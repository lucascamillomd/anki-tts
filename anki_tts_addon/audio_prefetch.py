import logging
import queue
import threading


log = logging.getLogger(__name__)


class AudioPrefetcher:
    _SENTINEL = object()

    def __init__(self, engine):
        self.engine = engine
        self._queue = queue.Queue()
        self._queued_keys = set()
        self._lock = threading.Lock()
        self._handoff_lock = threading.Lock()
        self._thread = None
        self._accepting = True
        self._stop_requested = False
        self._active_count = 0
        self._idle_callback = None
        self._result_callback = None
        self._idle_notified = True

    def enqueue(self, text, config):
        if not text:
            return False

        item_config = dict(config or {})
        key = self._key(text, item_config)
        with self._lock:
            if self._finish_stopped_thread_locked():
                self._accepting = True
            if self._stop_requested or not self._accepting:
                return False
            if key in self._queued_keys:
                return False

            self._queued_keys.add(key)
            self._idle_notified = False
            self._queue.put((text, item_config))
            return True

    def set_idle_callback(self, callback):
        with self._lock:
            self._idle_callback = callback

    def set_result_callback(self, callback):
        with self._lock:
            self._result_callback = callback

    def status(self):
        with self._lock:
            active = self._active_count
            pending = max(0, len(self._queued_keys) - active)
            running = (
                self._thread is not None
                and self._thread.is_alive()
            ) or active > 0 or pending > 0
            return {
                "running": running,
                "active": active,
                "pending": pending,
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
            self._thread = threading.Thread(target=self._run, daemon=True)
            self._thread.start()
            return True

    def stop(self, timeout=None):
        with self._lock:
            self._stop_requested = True
            thread = self._thread
            if thread is None or not thread.is_alive():
                self._thread = None
                self._accepting = False
                self._stop_requested = False
                self._discard_pending_locked()
                return True

            self._discard_pending_locked()
            self._queue.put(self._SENTINEL)

        if not self._wait_for_handoff(timeout):
            return False

        with self._handoff_lock:
            with self._lock:
                self._accepting = False

        thread.join(timeout=timeout)

        with self._lock:
            if self._thread is thread and not thread.is_alive():
                self._thread = None
                self._accepting = False
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
            item = self._queue.get()
            try:
                if item is self._SENTINEL:
                    return
                text, config = item
                with self._lock:
                    if not self._accepting:
                        self._queued_keys.discard(self._key(text, config))
                        continue

                self._process_if_accepting(text, config)
            finally:
                self._queue.task_done()

    def _process_if_accepting(self, text, config):
        key = self._key(text, config)
        with self._handoff_lock:
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
        try:
            result = self.engine.prefetch(text, config)
            if result is None:
                ok = None
            else:
                ok = result is not False
            if ok is False:
                error = "prefetch failed"
        except Exception as exc:
            error = str(exc) or exc.__class__.__name__
            log.warning("Audio prefetch failed", exc_info=True)
        finally:
            with self._lock:
                self._queued_keys.discard(key)
                callback = self._result_callback

        if callback is not None:
            callback(text, config, ok, error)

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

    def _wait_for_handoff(self, timeout):
        if timeout is None:
            self._handoff_lock.acquire()
            self._handoff_lock.release()
            return True

        acquired = self._handoff_lock.acquire(timeout=timeout)
        if not acquired:
            return False
        self._handoff_lock.release()
        return True

    def _finish_stopped_thread_locked(self):
        if (
            self._stop_requested
            and self._thread is not None
            and not self._thread.is_alive()
        ):
            self._thread = None
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
                and self._active_count == 0
                and not self._queued_keys
            ):
                self._idle_notified = True
                callback = self._idle_callback

        if callback is not None:
            callback()
