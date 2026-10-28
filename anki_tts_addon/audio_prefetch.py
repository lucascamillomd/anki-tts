import logging
import queue
import threading
import time


log = logging.getLogger(__name__)


class AudioPrefetcher:
    _SENTINEL = object()
    PAUSE_POLL_SECONDS = 0.5
    USER_PAUSE_POLL_SECONDS = 0.25
    # Each request is ~1 s of network wait, not CPU, so a few concurrent
    # requests multiply throughput. Kept small: Microsoft may throttle.
    WORKERS = 1
    MAX_WORKERS = 4

    def __init__(self, engine):
        self.engine = engine
        self._queue = queue.Queue()
        self._queued_keys = set()
        self._lock = threading.Lock()
        self._stop_event = threading.Event()
        self._thread = None
        self._extra_threads = []
        self._workers = self.WORKERS
        self._accepting = True
        self._stop_requested = False
        self._active_count = 0
        self._idle_callback = None
        self._result_callback = None
        self._idle_notified = True
        # Threads holding the queue while Edge is unavailable.
        self._edge_waiters = 0
        # User pause (monitor button). Set while running; cleared while paused.
        self._user_paused = False
        self._run_gate = threading.Event()
        self._run_gate.set()
        # Thread ident -> (item preview, monotonic start) for active items.
        self._in_flight = {}
        self._succeeded = 0
        self._failed = 0
        self._canceled = 0
        self._last_error = None

    def enqueue(self, text, config):
        if not text:
            return False

        item_config = dict(config or {})
        key = self._key(text, item_config)
        with self._lock:
            # A stopped worker is only revived by start(), never by enqueue:
            # otherwise items pile up with no thread to process them.
            self._finish_stopped_thread_locked()
            if self._stop_requested or not self._accepting:
                return False
            if key in self._queued_keys:
                return False

            self._queued_keys.add(key)
            self._idle_notified = False
            self._queue.put((text, item_config))
            return True

    def pause(self):
        """Hold the queue after the current item; stop() still exits promptly."""
        with self._lock:
            self._user_paused = True
            self._run_gate.clear()

    def resume(self):
        with self._lock:
            self._user_paused = False
            self._run_gate.set()

    def set_workers(self, count):
        """Number of concurrent requests; takes effect at the next start()."""
        with self._lock:
            self._workers = max(1, min(self.MAX_WORKERS, int(count)))

    def is_paused(self):
        with self._lock:
            return self._user_paused

    def set_idle_callback(self, callback):
        with self._lock:
            self._idle_callback = callback

    def set_result_callback(self, callback):
        with self._lock:
            self._result_callback = callback

    def status(self):
        """Thread-safe snapshot of the worker for status/monitor UIs."""
        with self._lock:
            active = self._active_count
            pending = max(0, len(self._queued_keys) - active)
            alive = any(t.is_alive() for t in self._threads_locked())
            running = alive or active > 0 or pending > 0
            # The oldest active item, so a slow request stays visible.
            current, started = min(
                self._in_flight.values(),
                key=lambda item: item[1],
                default=(None, None),
            )
            edge_waiting = self._edge_waiters > 0
            return {
                "running": running,
                "alive": alive,
                "active": active,
                "pending": pending,
                "paused": edge_waiting or self._user_paused,
                "user_paused": self._user_paused,
                "edge_waiting": edge_waiting,
                "stopping": self._stop_requested,
                "current": current,
                "current_started_at": started,
                "succeeded": self._succeeded,
                "failed": self._failed,
                "canceled": self._canceled,
                "last_error": self._last_error,
            }

    def start(self):
        with self._lock:
            self._finish_stopped_thread_locked()
            if self._stop_requested:
                return False
            if self._thread is not None and self._thread.is_alive():
                return self._accepting and not self._stop_requested

            self._accepting = True
            self._stop_requested = False
            self._stop_event.clear()
            if self._user_paused:
                self._run_gate.clear()
            else:
                self._run_gate.set()
            self._thread = threading.Thread(
                target=self._run, daemon=True, name="anki-tts-prefetch"
            )
            self._thread.start()
            self._extra_threads = []
            for index in range(1, self._workers):
                thread = threading.Thread(
                    target=self._run,
                    daemon=True,
                    name=f"anki-tts-prefetch-{index}",
                )
                thread.start()
                self._extra_threads.append(thread)
            return True

    def stop(self, timeout=None):
        with self._lock:
            self._stop_requested = True
            self._accepting = False
            self._stop_event.set()
            # Wake a user-paused worker so it can never block quit.
            self._run_gate.set()
            primary = self._thread
            threads = [t for t in self._threads_locked() if t.is_alive()]
            if not threads:
                self._thread = None
                self._extra_threads = []
                self._stop_requested = False
                self._discard_pending_locked()
                return True

            self._discard_pending_locked()
            for _ in threads:
                self._queue.put(self._SENTINEL)

        # Abort the in-flight network request so the worker exits promptly
        # instead of blocking the caller (often the Qt main thread).
        cancel = getattr(self.engine, "cancel_prefetch", None)
        if cancel is not None:
            try:
                cancel()
            except Exception:
                log.warning("Could not cancel in-flight prefetch", exc_info=True)

        deadline = None if timeout is None else time.monotonic() + timeout
        for thread in threads:
            if deadline is None:
                thread.join()
            else:
                thread.join(timeout=max(0.0, deadline - time.monotonic()))

        with self._lock:
            if self._thread is primary and not any(
                t.is_alive() for t in threads
            ):
                self._thread = None
                self._extra_threads = []
                self._stop_requested = False
                return True

            return False

    def drain_for_tests(self):
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                return

            try:
                if item is self._SENTINEL:
                    continue
                text, config = item
                self._process(text, config)
                self._notify_idle_if_needed()
            finally:
                self._queue.task_done()

    def _run(self):
        while True:
            try:
                item = self._queue.get()
                try:
                    if item is self._SENTINEL:
                        return
                    text, config = item
                    self._wait_while_user_paused()
                    self._wait_while_paused()
                    with self._lock:
                        if self._stop_requested or not self._accepting:
                            self._queued_keys.discard(self._key(text, config))
                            continue

                    self._process_if_accepting(text, config)
                finally:
                    self._queue.task_done()
            except Exception:
                # No single item may end the worker thread.
                log.exception("Audio prefetch worker error")

    def _wait_while_user_paused(self):
        while not self._stop_event.is_set():
            with self._lock:
                if not self._user_paused:
                    return
            self._run_gate.wait(self.USER_PAUSE_POLL_SECONDS)

    def _wait_while_paused(self):
        """Hold the queue while the engine reports Edge as unavailable."""
        pause_seconds = getattr(self.engine, "prefetch_pause_seconds", None)
        if pause_seconds is None:
            return
        logged = False
        try:
            while not self._stop_event.is_set():
                try:
                    delay = float(pause_seconds() or 0)
                except Exception:
                    delay = 0
                if delay <= 0:
                    break
                if not logged:
                    logged = True
                    with self._lock:
                        self._edge_waiters += 1
                        first = self._edge_waiters == 1
                    if first:  # one line per pause, not one per worker
                        log.warning(
                            "Edge TTS unavailable; pausing audio prefetch "
                            "for up to %.0f s",
                            delay,
                        )
                self._stop_event.wait(min(delay, self.PAUSE_POLL_SECONDS))
        finally:
            if logged:
                with self._lock:
                    self._edge_waiters -= 1

    def _process_if_accepting(self, text, config):
        key = self._key(text, config)
        with self._lock:
            if self._stop_requested or not self._accepting:
                self._queued_keys.discard(key)
                return
            self._active_count += 1

        try:
            self._process(text, config)
        finally:
            with self._lock:
                self._active_count -= 1
        self._notify_idle_if_needed()

    def _process(self, text, config):
        key = self._key(text, config)
        ok = False
        error = None
        ident = threading.get_ident()
        with self._lock:
            self._in_flight[ident] = (
                " ".join(str(text).split())[:120],
                time.monotonic(),
            )
        try:
            result = self.engine.prefetch(text, config)
            if result is None:
                ok = None
            else:
                ok = result is not False
            if ok is False:
                error = self._engine_failure_reason() or "prefetch failed"
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            log.warning("Audio prefetch failed", exc_info=True)
        finally:
            with self._lock:
                self._queued_keys.discard(key)
                self._in_flight.pop(ident, None)
                if ok is None:
                    self._canceled += 1
                elif ok:
                    self._succeeded += 1
                else:
                    self._failed += 1
                    self._last_error = error
                callback = self._result_callback

        if callback is not None:
            try:
                callback(text, config, ok, error)
            except Exception:
                log.exception("Audio prefetch result callback failed")

    def _engine_failure_reason(self):
        reason = getattr(self.engine, "last_prefetch_error", None)
        if not callable(reason):
            return None
        try:
            value = reason()
        except Exception:
            return None
        return value if isinstance(value, str) and value else None

    @staticmethod
    def _key(text, config):
        return (text, config.get("speed", 1.5))

    def _discard_pending_locked(self):
        while True:
            try:
                item = self._queue.get_nowait()
            except queue.Empty:
                break

            try:
                if item is not self._SENTINEL:
                    text, config = item
                    self._queued_keys.discard(self._key(text, config))
            finally:
                self._queue.task_done()

    def _threads_locked(self):
        if self._thread is None:
            return []
        return [self._thread, *self._extra_threads]

    def _finish_stopped_thread_locked(self):
        if (
            self._stop_requested
            and self._thread is not None
            and not any(t.is_alive() for t in self._threads_locked())
        ):
            self._thread = None
            self._extra_threads = []
            self._accepting = False
            self._stop_requested = False
            self._discard_pending_locked()
            return True
        return False

    def _notify_idle_if_needed(self):
        callback = None
        with self._lock:
            if (
                not self._idle_notified
                and not self._stop_requested
                and self._active_count == 0
                and not self._queued_keys
            ):
                self._idle_notified = True
                callback = self._idle_callback

        if callback is not None:
            try:
                callback()
            except Exception:
                log.exception("Audio prefetch idle callback failed")
