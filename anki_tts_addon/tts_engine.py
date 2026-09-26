"""
TTS engine for the Anki TTS add-on.
Fallback: Edge TTS (online/cached) -> system TTS.
Edge TTS is bundled in vendor/ and imported directly.
"""

import asyncio
import concurrent.futures
import errno
import os
import ssl
import sys
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import logging
import platform
from pathlib import Path
from typing import Optional, Callable

from .audio_cache import AudioCache, AudioTooSmallError, speech_cache_key

log = logging.getLogger(__name__)

# Status callback — set by __init__.py to show tooltips to the user
_status_callback: Optional[Callable[[str], None]] = None
_edge_import_error: Optional[Exception] = None

# asyncio.TimeoutError and concurrent.futures.TimeoutError are only aliases of
# the builtin TimeoutError from Python 3.11; Anki still ships older Pythons.
_TIMEOUT_ERRORS = (
    TimeoutError,
    asyncio.TimeoutError,
    concurrent.futures.TimeoutError,
)

_NETWORK_ERRNOS = {
    errno.ENETDOWN,
    errno.ENETUNREACH,
    errno.EHOSTDOWN,
    errno.EHOSTUNREACH,
}


class _SpeechCancelled(Exception):
    pass


def set_status_callback(cb: Optional[Callable[[str], None]]):
    global _status_callback
    _status_callback = cb


def _notify(msg: str):
    log.info(msg)
    callback = _status_callback
    if callback:
        try:
            callback(msg)
        except Exception:
            log.debug("TTS status callback failed", exc_info=True)


def _exception_chain(exc: BaseException):
    seen = set()
    while exc is not None and id(exc) not in seen:
        seen.add(id(exc))
        yield exc
        exc = exc.__cause__ or exc.__context__


def _is_tls_error(exc: BaseException) -> bool:
    # Match on types, not text: every aiohttp connector error message embeds
    # "ssl:<context>", so a plain DNS failure would read as a TLS problem.
    return any(
        isinstance(e, ssl.SSLError)
        or type(e).__name__ in ("ClientSSLError", "ClientConnectorCertificateError")
        for e in _exception_chain(exc)
    )


def _is_network_error(exc: BaseException) -> bool:
    """True for connectivity failures (offline, DNS, refused), not TLS."""
    if _is_tls_error(exc):
        return False
    return any(
        isinstance(e, (socket.gaierror, ConnectionError))
        or (isinstance(e, OSError) and e.errno in _NETWORK_ERRNOS)
        for e in _exception_chain(exc)
    )


def _edge_error_reason(exc: Exception) -> str:
    """Return a short user-facing reason for Edge TTS failure."""
    if isinstance(exc, ModuleNotFoundError):
        missing = exc.name or "dependency"
        return f"missing module: {missing}"
    if isinstance(exc, _TIMEOUT_ERRORS):
        return "request timed out"

    if _is_tls_error(exc):
        return "TLS/certificate error"
    if _is_network_error(exc):
        return "network connection failed"

    msg = str(exc).strip()
    low = msg.lower()
    if "certificate verify failed" in low:
        return "TLS/certificate error"
    if (
        "cannot connect" in low
        or "name or service not known" in low
        or "nodename nor servname" in low
        or "temporary failure in name resolution" in low
        or "network is unreachable" in low
        or "connection reset" in low
        or "connection refused" in low
    ):
        return "network connection failed"
    if msg:
        return msg.splitlines()[0][:120]
    return exc.__class__.__name__


def _notify_edge_unavailable(exc: Exception):
    _notify(
        f"Edge TTS unavailable ({_edge_error_reason(exc)}) — using system voice"
    )


def _addon_dir() -> str:
    return os.path.dirname(os.path.abspath(__file__))


def _vendor_dir() -> str:
    d = os.path.join(_addon_dir(), "vendor")
    os.makedirs(d, exist_ok=True)
    return d


def _ensure_vendor_on_path():
    vd = _vendor_dir()
    if vd not in sys.path:
        sys.path.insert(0, vd)


# ---------------------------------------------------------------------------
# Edge TTS (online, best quality)
# ---------------------------------------------------------------------------

def _import_edge_tts():
    """Import bundled edge_tts from vendor/ (no runtime installs)."""
    global _edge_import_error
    _ensure_vendor_on_path()
    try:
        import edge_tts
        _edge_import_error = None
        return edge_tts
    except Exception as e:
        _edge_import_error = e
        log.warning("edge_tts import failed: %s", e)
        return None


class EdgeCircuitBreaker:
    """Stops hammering Edge after repeated failures, then retries later.

    Trips after THRESHOLD consecutive generation failures. Live speech skips
    Edge for LIVE_COOLDOWN_SECONDS; background prefetch pauses for an
    escalating cooldown. After a cooldown one more failure re-trips it, and
    any success closes it again.
    """

    THRESHOLD = 3
    LIVE_COOLDOWN_SECONDS = 60
    PREFETCH_COOLDOWNS_SECONDS = (60, 300, 900)

    def __init__(self, clock=time.monotonic):
        self._clock = clock
        self._lock = threading.Lock()
        self._failures = 0
        self._trips = 0
        self._tripped_at = None
        self._open_until = 0.0
        self._last_error = None

    def record_success(self) -> None:
        with self._lock:
            self._failures = 0
            self._trips = 0
            self._tripped_at = None
            self._open_until = 0.0

    def record_failure(self, error) -> bool:
        """Count a failure; return True when this failure trips the breaker."""
        with self._lock:
            now = self._clock()
            self._last_error = _edge_error_reason(error)
            self._failures += 1
            threshold = 1 if self._trips else self.THRESHOLD
            if self._failures < threshold or now < self._open_until:
                return False
            cooldowns = self.PREFETCH_COOLDOWNS_SECONDS
            cooldown = cooldowns[min(self._trips, len(cooldowns) - 1)]
            self._trips += 1
            self._failures = 0
            self._tripped_at = now
            self._open_until = now + cooldown
            return True

    def live_retry_in(self) -> float:
        with self._lock:
            if self._tripped_at is None:
                return 0.0
            until = min(
                self._open_until,
                self._tripped_at + self.LIVE_COOLDOWN_SECONDS,
            )
            return max(0.0, until - self._clock())

    def prefetch_retry_in(self) -> float:
        with self._lock:
            return max(0.0, self._open_until - self._clock())

    def snapshot(self) -> dict:
        with self._lock:
            retry_in = max(0.0, self._open_until - self._clock())
            return {
                "open": retry_in > 0,
                "retry_in": retry_in,
                "consecutive_failures": self._failures,
                "trips": self._trips,
                "last_error": self._last_error,
            }


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TTSEngine:
    """Manages TTS with fallback: Edge TTS -> system voice."""

    EDGE_VOICE = "en-GB-RyanNeural"
    EDGE_REQUEST_TIMEOUT_SECONDS = 30
    PROCESS_TIMEOUT_SECONDS = 120
    STOP_WAIT_SECONDS = 0.2
    EDGE_MISSING_PAUSE_SECONDS = 3600.0
    # Evict down to this fraction of the limit so a full cache is not
    # rescanned after every new clip.
    CLEANUP_TARGET_RATIO = 0.9

    def __init__(
        self,
        audio_cache: Optional[AudioCache] = None,
        cache_max_bytes: Optional[int] = None,
    ):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._lazy_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._generation_id = 0
        self._audio_cache = audio_cache
        self._cache_max_bytes = cache_max_bytes
        self._edge_tts = None
        self._edge_tts_checked = False
        self._breaker = EdgeCircuitBreaker()
        self._shutdown = False
        # All Edge requests run on one long-lived asyncio loop, so in-flight
        # requests can be cancelled and no thread is created per request.
        self._loop = None
        self._loop_thread = None
        self._loop_lock = threading.Lock()
        self._futures_lock = threading.Lock()
        self._prefetch_futures = set()
        self._live_futures = {}
        self._local = threading.local()
        # One speech worker consumes a single-slot mailbox: the latest
        # speak() request replaces any request that has not started yet.
        self._speech_cond = threading.Condition()
        self._speech_request = None
        self._speech_thread = None
        self._stats_lock = threading.Lock()
        self._live_hits = 0
        self._live_misses = 0

    def set_cache_max_bytes(self, cache_max_bytes: Optional[int]) -> None:
        self._cache_max_bytes = cache_max_bytes

    def set_audio_cache(self, audio_cache: Optional[AudioCache]) -> None:
        """Swap the cache wiring in place (used when a clean rebuild can't run)."""
        self._audio_cache = audio_cache

    def _get_edge_tts(self):
        """Lazily load bundled edge_tts on first call."""
        with self._lazy_lock:
            if not self._edge_tts_checked:
                self._edge_tts = _import_edge_tts()
                self._edge_tts_checked = True
                if self._edge_tts is None:
                    _notify_edge_unavailable(
                        _edge_import_error
                        or RuntimeError("edge_tts import failed")
                    )
            return self._edge_tts

    # -- status -------------------------------------------------------------

    def _edge_missing_reason(self) -> Optional[str]:
        """Why bundled edge_tts could not be imported (None when fine/unchecked)."""
        with self._lazy_lock:
            if not self._edge_tts_checked or self._edge_tts is not None:
                return None
        return "Edge TTS not installed: " + _edge_error_reason(
            _edge_import_error or RuntimeError("edge_tts import failed")
        )

    def edge_status(self) -> dict:
        status = self._breaker.snapshot()
        missing = self._edge_missing_reason()
        if missing is not None:
            # A missing module is not a transient outage: report it as down
            # (the breaker never sees it) so the monitor doesn't say "OK".
            status.update(
                open=True, retry_in=0.0, last_error=missing, missing=True
            )
        return status

    def live_stats(self) -> dict:
        with self._stats_lock:
            return {"hits": self._live_hits, "misses": self._live_misses}

    def status_snapshot(self) -> dict:
        with self._speech_cond:
            speaking = self._speech_thread is not None
        return {
            "edge": self.edge_status(),
            "live": self.live_stats(),
            "speaking": speaking,
        }

    def prefetch_pause_seconds(self) -> float:
        """Seconds background prefetch should wait before using Edge again."""
        if self._edge_missing_reason() is not None:
            # edge_tts can't appear without restarting Anki: hold the queue
            # instead of failing (and backing off) every remaining card.
            return self.EDGE_MISSING_PAUSE_SECONDS
        return self._breaker.prefetch_retry_in()

    def _count_live(self, hit: bool) -> None:
        with self._stats_lock:
            if hit:
                self._live_hits += 1
            else:
                self._live_misses += 1

    # -- live speech --------------------------------------------------------

    def speak(self, text: str, config: dict) -> None:
        """Stop any current speech, then speak text on the speech worker."""
        self.stop()
        generation_id = self._begin_generation()
        speed = (config or {}).get("speed", 1.5)
        self._cancel_live_requests_except(
            self._cache_key(text, speed) if text else None
        )
        with self._speech_cond:
            if self._shutdown:
                return
            self._speech_request = (text, dict(config or {}), generation_id)
            if self._speech_thread is None:
                self._speech_thread = threading.Thread(
                    target=self._speech_worker,
                    daemon=True,
                    name="anki-tts-speech",
                )
                self._speech_thread.start()
            self._speech_cond.notify_all()

    def _speech_worker(self) -> None:
        while True:
            with self._speech_cond:
                request = self._speech_request
                self._speech_request = None
                if request is None or self._shutdown:
                    self._speech_thread = None
                    self._speech_cond.notify_all()
                    return
            text, config, generation_id = request
            try:
                self._speak(text, config, generation_id)
            except _SpeechCancelled:
                pass
            except Exception:
                log.warning("TTS speech failed", exc_info=True)

    def wait_for_speech(self, timeout: Optional[float] = None) -> bool:
        """Wait for the live speech worker to finish."""
        deadline = None if timeout is None else time.monotonic() + timeout
        with self._speech_cond:
            while (
                self._speech_thread is not None
                and self._speech_thread is not threading.current_thread()
            ):
                if not self._speech_thread.is_alive():
                    self._speech_thread = None
                    break
                if deadline is None:
                    self._speech_cond.wait(0.1)
                    continue
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                self._speech_cond.wait(min(remaining, 0.1))
            return True

    def _begin_generation(self) -> int:
        with self._generation_lock:
            return self._generation_id

    def _cancel_generation(self) -> None:
        with self._generation_lock:
            self._generation_id += 1

    def _is_generation_current(self, generation_id: Optional[int]) -> bool:
        if generation_id is None:
            return True
        with self._generation_lock:
            return generation_id == self._generation_id

    def _raise_if_cancelled(self, generation_id: Optional[int]) -> None:
        if not self._is_generation_current(generation_id):
            raise _SpeechCancelled()

    def _is_generation_current_locked(
        self, generation_id: Optional[int]
    ) -> bool:
        return generation_id is None or generation_id == self._generation_id

    def _register_process_if_current(
        self, generation_id: Optional[int], cmd: list
    ):
        with self._generation_lock:
            if not self._is_generation_current_locked(generation_id):
                raise _SpeechCancelled()
            try:
                proc = subprocess.Popen(cmd)
            except OSError:
                return None
            with self._lock:
                self._process = proc
            return proc

    # -- prefetch -----------------------------------------------------------

    def prefetch(self, text: str, config: dict) -> Optional[bool]:
        """Generate Edge audio into the cache without playing it.

        Returns True when cached, False on failure, None when cancelled or
        while Edge is paused by the circuit breaker (not a card failure).
        """
        self._local.prefetch_error = None
        if not text or self._audio_cache is None:
            self._local.prefetch_error = "audio cache disabled"
            return False

        speed = config.get("speed", 1.5)
        if self._cached_path(text, speed) is not None:
            return True

        if self._breaker.prefetch_retry_in() > 0:
            return None

        edge_tts = self._get_edge_tts()
        if edge_tts is None:
            # Not this card's fault: leave it unfailed; the worker pauses on
            # prefetch_pause_seconds() and the monitor shows the reason.
            return None

        self._local.prefetch = True
        try:
            # Prefetch is intentionally decoupled from the live playback
            # generation: a card advance during review (speak()->stop()) must
            # not cancel or delete in-flight warm-up work.
            self._generate_edge_cached(
                text, speed, edge_tts, generation_id=None
            )
        except _SpeechCancelled:
            return None
        except AudioTooSmallError as e:
            log.warning("Edge TTS prefetch produced unusable audio: %s", e)
            self._local.prefetch_error = f"audio too small: {e}"
            return False
        except _TIMEOUT_ERRORS as e:
            self._record_edge_failure(e)
            log.warning(
                "Edge TTS prefetch timed out: %s", str(e) or type(e).__name__
            )
            self._local.prefetch_error = "request timed out"
            return False
        except Exception as e:
            # A transient Edge error must not disable the remaining cache queue.
            # Let the worker record the real error for this card and continue.
            self._record_edge_failure(e)
            reason = _edge_error_reason(e)
            if not _is_network_error(e):
                log.warning("Edge TTS prefetch failed: %s", reason)
                raise
            # Being offline is expected: one line, no traceback. The breaker
            # still pauses the queue until the network is back.
            log.warning("Edge TTS prefetch failed: %s (%s)", reason, e)
            self._local.prefetch_error = reason
            return False
        else:
            self._breaker.record_success()
            self._cleanup_cache()
            return True
        finally:
            self._local.prefetch = False

    def last_prefetch_error(self) -> Optional[str]:
        """Reason for this thread's last failed prefetch() (None if none)."""
        return getattr(self._local, "prefetch_error", None)

    def cancel_prefetch(self) -> None:
        """Cancel in-flight background Edge requests (used on stop/close)."""
        with self._futures_lock:
            futures = list(self._prefetch_futures)
        for future in futures:
            future.cancel()

    def shutdown(self, timeout: float = 0.2) -> bool:
        """Stop speech, cancel all Edge requests and stop the asyncio loop.

        Waits at most ``timeout`` seconds; returns True when the loop thread
        has exited. The engine refuses new speech afterwards.
        """
        with self._speech_cond:
            self._shutdown = True
            self._speech_request = None
            self._speech_cond.notify_all()
        self.stop()
        with self._futures_lock:
            futures = list(self._prefetch_futures)
            futures.extend(self._live_futures)
        for future in futures:
            future.cancel()
        with self._loop_lock:
            loop, thread = self._loop, self._loop_thread
            self._loop = None
            self._loop_thread = None
        if loop is not None:
            try:
                loop.call_soon_threadsafe(loop.stop)
            except RuntimeError:
                pass
        if thread is not None:
            thread.join(timeout=timeout)
            return not thread.is_alive()
        return True

    # -- speech pipeline ----------------------------------------------------

    def _speak(
        self, text: str, config: dict, generation_id: Optional[int] = None
    ) -> None:
        speed = config.get("speed", 1.5)
        fallback = config.get("fallback_to_system", True)

        self._raise_if_cancelled(generation_id)

        cached = self._cached_path(text, speed)
        self._count_live(hit=cached is not None)
        if cached is not None:
            try:
                self._play_cached_file(cached, generation_id)
                return
            except _SpeechCancelled:
                raise
            except Exception as e:
                log.warning("Cached TTS playback failed, regenerating: %s", e)

        # Tier 1: Edge TTS (online, best quality), unless the breaker is open
        # after repeated failures.
        edge_tts = None
        if self._breaker.live_retry_in() <= 0:
            edge_tts = self._get_edge_tts()
        if edge_tts is not None:
            generated = None
            uncached = None
            try:
                if self._audio_cache is None:
                    uncached = self._generate_edge_uncached(
                        text, speed, edge_tts, generation_id
                    )
                else:
                    generated = self._generate_edge_cached(
                        text, speed, edge_tts, generation_id
                    )
            except _SpeechCancelled:
                raise
            except AudioTooSmallError as e:
                # Content-level glitch, not Edge being down: fall back for
                # this card but keep using Edge for the rest of the session.
                self._raise_if_cancelled(generation_id)
                log.warning(
                    "Edge TTS produced unusable audio, using system "
                    "voice for this card: %s",
                    e,
                )
            except Exception as e:
                self._raise_if_cancelled(generation_id)
                log.warning(
                    "Edge TTS failed, using system voice for this card: %s",
                    _edge_error_reason(e),
                )
                self._record_edge_failure(e)
            else:
                self._breaker.record_success()
                if generated is not None:
                    self._cleanup_cache()
                # Playback errors are not Edge failures and must not make
                # the same text be spoken a second time by the fallback.
                self._play_generated(generated, uncached, generation_id)
                return

        # Tier 2: System TTS
        if fallback:
            self._speak_system(text, speed, generation_id)

    def _play_generated(self, cached, uncached, generation_id) -> None:
        try:
            self._raise_if_cancelled(generation_id)
            if uncached is not None:
                self._play_file(str(uncached), generation_id)
            elif cached is not None:
                self._play_cached_file(cached, generation_id)
        except _SpeechCancelled:
            raise
        except Exception as e:
            log.warning("TTS playback failed: %s", e)
        finally:
            if uncached is not None:
                try:
                    os.unlink(uncached)
                except OSError:
                    pass

    def _record_edge_failure(self, error: Exception) -> None:
        if self._breaker.record_failure(error):
            snapshot = self._breaker.snapshot()
            log.warning(
                "Edge TTS failing repeatedly (%s); pausing it for %.0f s",
                _edge_error_reason(error),
                snapshot["retry_in"],
            )
            _notify_edge_unavailable(error)

    def _cache_key(self, text: str, speed: float) -> str:
        return speech_cache_key(text, self.EDGE_VOICE, speed)

    def _cleanup_cache(self) -> None:
        """Evict old clips, but only when the tracked size is over the limit."""
        cache = self._audio_cache
        max_bytes = self._cache_max_bytes
        if cache is None or max_bytes is None:
            return
        try:
            cleanup_if_needed = getattr(cache, "cleanup_if_needed", None)
            if cleanup_if_needed is None:
                cache.cleanup(max_bytes)
            else:
                cleanup_if_needed(
                    max_bytes, int(max_bytes * self.CLEANUP_TARGET_RATIO)
                )
        except Exception as e:
            log.warning("Audio cache cleanup failed: %s", e)

    def _cached_path(self, text: str, speed: float) -> Optional[Path]:
        if self._audio_cache is None:
            return None
        return self._audio_cache.get(self._cache_key(text, speed))

    def _generate_edge_cached(
        self,
        text: str,
        speed: float,
        edge_tts,
        generation_id: Optional[int] = None,
    ) -> Optional[Path]:
        if self._audio_cache is None:
            return None

        key = self._cache_key(text, speed)
        cached = self._audio_cache.get(key)
        if cached is not None:
            return cached

        with self._audio_cache.generation_lock(key):
            cached = self._audio_cache.get(key)
            if cached is not None:
                return cached

            tmp = self._audio_cache.temp_path_for_key(key)
            try:
                self._raise_if_cancelled(generation_id)
                self._save_edge_audio(text, speed, edge_tts, tmp)
                # Always publish a finished clip, even if the card was
                # advanced meanwhile: it is valid and will be reused. Only
                # playback checks the generation.
                return self._audio_cache.store_from_temp(key, tmp)
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise

    def _generate_edge_uncached(
        self,
        text: str,
        speed: float,
        edge_tts,
        generation_id: Optional[int] = None,
    ) -> Path:
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp = Path(f.name)
        try:
            self._raise_if_cancelled(generation_id)
            self._save_edge_audio(text, speed, edge_tts, tmp)
            return tmp
        except Exception:
            try:
                tmp.unlink()
            except OSError:
                pass
            raise

    def _play_cached_file(
        self, path: Path, generation_id: Optional[int] = None
    ) -> None:
        # Copy to a private temp file so a concurrent cache cleanup/clear can't
        # delete the file mid-playback; stream the copy rather than loading the
        # whole clip into memory.
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp = Path(f.name)
            shutil.copyfile(path, tmp)
            self._play_file(str(tmp), generation_id)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    # -- Edge requests on the persistent loop -------------------------------

    def _ensure_loop(self):
        with self._loop_lock:
            if self._shutdown:
                raise _SpeechCancelled()
            if (
                self._loop is None
                or self._loop_thread is None
                or not self._loop_thread.is_alive()
            ):
                loop = asyncio.new_event_loop()
                thread = threading.Thread(
                    target=self._run_loop,
                    args=(loop,),
                    daemon=True,
                    name="anki-tts-edge-loop",
                )
                thread.start()
                self._loop = loop
                self._loop_thread = thread
            return self._loop

    @staticmethod
    def _run_loop(loop) -> None:
        asyncio.set_event_loop(loop)
        try:
            loop.run_forever()
        finally:
            try:
                pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(asyncio.wait(pending, timeout=1))
            except Exception:
                log.debug("Edge TTS loop shutdown failed", exc_info=True)
            finally:
                loop.close()

    def _cancel_live_requests_except(self, key: Optional[str]) -> None:
        """Cancel live Edge requests for other text (the card moved on)."""
        with self._futures_lock:
            stale = [
                future for future, future_key in self._live_futures.items()
                if future_key != key
            ]
        for future in stale:
            future.cancel()

    def _save_edge_audio(self, text: str, speed: float, edge_tts, path) -> None:
        """Generate Edge TTS audio at path on the engine's asyncio loop."""
        rate_pct = int((speed - 1.0) * 100)
        rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"
        timeout = self.EDGE_REQUEST_TIMEOUT_SECONDS
        tmp_path = os.fspath(path)

        async def _generate():
            try:
                comm = edge_tts.Communicate(text, self.EDGE_VOICE, rate=rate_str)
                await asyncio.wait_for(comm.save(tmp_path), timeout=timeout)
            except BaseException:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
                raise

        is_prefetch = getattr(self._local, "prefetch", False)
        loop = self._ensure_loop()
        future = asyncio.run_coroutine_threadsafe(_generate(), loop)
        with self._futures_lock:
            if is_prefetch:
                self._prefetch_futures.add(future)
            else:
                self._live_futures[future] = self._cache_key(text, speed)
        try:
            future.result(timeout=timeout + 5)
        except concurrent.futures.CancelledError:
            raise _SpeechCancelled()
        except _TIMEOUT_ERRORS:
            if not future.done():
                future.cancel()
            raise
        finally:
            with self._futures_lock:
                self._prefetch_futures.discard(future)
                self._live_futures.pop(future, None)

    # -- processes ----------------------------------------------------------

    def _speak_system(
        self, text: str, speed: float, generation_id: Optional[int] = None
    ) -> None:
        """Use the OS built-in TTS as a last resort."""
        system = platform.system()

        if system == "Darwin":
            rate = str(int(200 * speed))
            cmd = ["say", "-r", rate, text]
        elif system == "Linux":
            rate = str(int(175 * speed))
            cmd = ["espeak", "-s", rate, text]
        elif system == "Windows":
            sapi_rate = int((speed - 1.0) * 5)
            escaped = text.replace("'", "''")
            ps = (
                "Add-Type -AssemblyName System.Speech;"
                "$s = New-Object System.Speech.Synthesis.SpeechSynthesizer;"
                f"$s.Rate = {sapi_rate};"
                f"$s.Speak('{escaped}');"
            )
            cmd = ["powershell", "-Command", ps]
        else:
            return

        self._run_process(cmd, generation_id)

    def _play_file(
        self, path: str, generation_id: Optional[int] = None
    ) -> None:
        """Play an audio file using platform-appropriate command."""
        system = platform.system()

        if system == "Darwin":
            cmd = ["afplay", path]
        elif system == "Linux":
            cmd = ["mpv", "--no-terminal", "--", path]
        elif system == "Windows":
            cmd = [
                "ffplay", "-nodisp", "-autoexit", "-loglevel", "quiet", path,
            ]
        else:
            return

        self._run_process(cmd, generation_id)

    def _run_process(
        self, cmd: list, generation_id: Optional[int] = None
    ) -> None:
        """Spawn a subprocess, wait for it, and clean up safely."""
        proc = self._register_process_if_current(generation_id, cmd)
        if proc is None:
            return

        try:
            proc.wait(timeout=self.PROCESS_TIMEOUT_SECONDS)
        except subprocess.TimeoutExpired:
            log.warning("TTS audio process timed out; stopping it")
            try:
                proc.kill()
                proc.wait(timeout=1)
            except Exception:
                pass
        finally:
            with self._lock:
                if self._process is proc:
                    self._process = None

    def stop(self) -> None:
        """Stop any in-progress speech without blocking the caller for long."""
        self._cancel_generation()
        with self._lock:
            proc = self._process

        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=self.STOP_WAIT_SECONDS)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

            with self._lock:
                if self._process is proc:
                    self._process = None
