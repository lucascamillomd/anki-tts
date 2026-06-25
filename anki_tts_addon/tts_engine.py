"""
TTS engine for the Anki TTS add-on.
Fallback: Edge TTS (online/cached) -> system TTS.
Edge TTS is bundled in vendor/ and imported directly.
"""

import os
import sys
import subprocess
import tempfile
import threading
import logging
import platform
from pathlib import Path
from typing import Optional, Callable

from .audio_cache import AudioCache, speech_cache_key

log = logging.getLogger(__name__)

# Status callback — set by __init__.py to show tooltips to the user
_status_callback: Optional[Callable[[str], None]] = None
_edge_import_error: Optional[Exception] = None


class _SpeechCancelled(Exception):
    pass


def set_status_callback(cb: Callable[[str], None]):
    global _status_callback
    _status_callback = cb


def _notify(msg: str):
    log.info(msg)
    if _status_callback:
        _status_callback(msg)


def _edge_error_reason(exc: Exception) -> str:
    """Return a short user-facing reason for Edge TTS failure."""
    if isinstance(exc, ModuleNotFoundError):
        missing = exc.name or "dependency"
        return f"missing module: {missing}"

    msg = str(exc).strip()
    low = msg.lower()
    if "certificate verify failed" in low or "ssl" in low:
        return "TLS/certificate error"
    if (
        "cannot connect" in low
        or "name or service not known" in low
        or "temporary failure in name resolution" in low
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


# ---------------------------------------------------------------------------
# Engine
# ---------------------------------------------------------------------------

class TTSEngine:
    """Manages TTS with fallback: Edge TTS -> system voice."""

    EDGE_VOICE = "en-GB-RyanNeural"

    def __init__(
        self,
        audio_cache: Optional[AudioCache] = None,
        cache_max_bytes: Optional[int] = None,
    ):
        self._process: Optional[subprocess.Popen] = None
        self._lock = threading.Lock()
        self._lazy_lock = threading.Lock()
        self._generation_lock = threading.Lock()
        self._publish_lock = threading.Lock()
        self._generation_id = 0
        self._speech_threads_lock = threading.Lock()
        self._speech_threads = set()
        self._audio_cache = audio_cache
        self._cache_max_bytes = cache_max_bytes
        self._edge_tts = None
        self._edge_tts_checked = False
        self._edge_tts_failed = False  # True after first speech failure

    def set_cache_max_bytes(self, cache_max_bytes: Optional[int]) -> None:
        self._cache_max_bytes = cache_max_bytes

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

    def speak(self, text: str, config: dict) -> None:
        """Stop any current speech, then speak text in a background thread."""
        self.stop()
        generation_id = self._begin_generation()

        def _run_speech():
            try:
                self._speak(text, config, generation_id)
            except _SpeechCancelled:
                pass
            finally:
                with self._speech_threads_lock:
                    self._speech_threads.discard(threading.current_thread())

        thread = threading.Thread(
            target=_run_speech, daemon=True
        )
        with self._speech_threads_lock:
            self._speech_threads.add(thread)
        thread.start()

    def wait_for_speech(self, timeout: Optional[float] = None) -> bool:
        """Wait for live speech generation threads to finish."""
        import time

        deadline = None if timeout is None else time.monotonic() + timeout
        current = threading.current_thread()

        while True:
            with self._speech_threads_lock:
                threads = [
                    thread for thread in self._speech_threads
                    if thread is not current
                ]

            if not threads:
                return True

            for thread in threads:
                if deadline is None:
                    join_timeout = None
                else:
                    join_timeout = max(0, deadline - time.monotonic())
                thread.join(timeout=join_timeout)

            with self._speech_threads_lock:
                self._speech_threads = {
                    thread for thread in self._speech_threads
                    if thread.is_alive()
                }
                if not self._speech_threads:
                    return True

            if deadline is not None and time.monotonic() >= deadline:
                return False

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

    def _run_if_current(self, generation_id: Optional[int], callback):
        with self._generation_lock:
            if generation_id is not None and generation_id != self._generation_id:
                raise _SpeechCancelled()
            return callback()

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

    def _store_cache_if_current(
        self, key: str, tmp, generation_id: Optional[int]
    ) -> Path:
        with self._publish_lock:
            self._raise_if_cancelled(generation_id)
            final = self._audio_cache.store_from_temp(key, tmp)
            try:
                self._raise_if_cancelled(generation_id)
            except _SpeechCancelled:
                self._audio_cache.remove(key)
                raise
            return final

    def prefetch(self, text: str, config: dict) -> Optional[bool]:
        """Generate Edge audio into the cache without playing it."""
        if not text or self._audio_cache is None:
            return False

        speed = config.get("speed", 1.5)
        if self._cached_path(text, speed) is not None:
            return True

        generation_id = self._begin_generation()
        edge_tts = self._get_edge_tts()
        if edge_tts is None:
            return False

        try:
            self._generate_edge_cached(text, speed, edge_tts, generation_id)
        except _SpeechCancelled:
            return None
        except Exception as e:
            log.warning("Edge TTS prefetch failed: %s", e)
            return False
        else:
            self._cleanup_cache()
            return True

    def _speak(
        self, text: str, config: dict, generation_id: Optional[int] = None
    ) -> None:
        speed = config.get("speed", 1.5)
        fallback = config.get("fallback_to_system", True)

        self._raise_if_cancelled(generation_id)

        cached = self._cached_path(text, speed)
        if cached is not None:
            try:
                self._play_cached_file(cached, generation_id)
                return
            except _SpeechCancelled:
                raise
            except Exception as e:
                log.warning("Cached TTS playback failed, regenerating: %s", e)

        # Tier 1: Edge TTS (online, best quality)
        # Skip if it failed previously — don't retry every card
        if not self._edge_tts_failed:
            edge_tts = self._get_edge_tts()
            if edge_tts is not None:
                try:
                    if self._audio_cache is None:
                        self._speak_edge(
                            text, speed, edge_tts, generation_id
                        )
                    else:
                        cached = self._generate_edge_cached(
                            text, speed, edge_tts, generation_id
                        )
                        try:
                            self._play_cached_file(cached, generation_id)
                        finally:
                            self._cleanup_cache()
                    return
                except _SpeechCancelled:
                    raise
                except Exception as e:
                    self._raise_if_cancelled(generation_id)
                    log.warning(
                        "Edge TTS failed, switching to system TTS: %s", e
                    )
                    self._edge_tts_failed = True
                    _notify_edge_unavailable(e)

        # Tier 2: System TTS
        if fallback:
            self._speak_system(text, speed, generation_id)

    def _cache_key(self, text: str, speed: float) -> str:
        return speech_cache_key(text, self.EDGE_VOICE, speed)

    def _cleanup_cache(self) -> None:
        if self._audio_cache is None or self._cache_max_bytes is None:
            return
        try:
            self._audio_cache.cleanup(self._cache_max_bytes)
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
                return self._store_cache_if_current(
                    key, tmp, generation_id
                )
            except Exception:
                try:
                    tmp.unlink()
                except OSError:
                    pass
                raise

    def _play_cached_file(
        self, path: Path, generation_id: Optional[int] = None
    ) -> None:
        tmp = None
        try:
            with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
                tmp = Path(f.name)
                f.write(path.read_bytes())
            self._play_file(str(tmp), generation_id)
        finally:
            if tmp is not None:
                try:
                    tmp.unlink()
                except OSError:
                    pass

    def _save_edge_audio(self, text: str, speed: float, edge_tts, path) -> None:
        """Generate Edge TTS audio at path."""
        import asyncio

        rate_pct = int((speed - 1.0) * 100)
        rate_str = f"+{rate_pct}%" if rate_pct >= 0 else f"{rate_pct}%"

        async def _generate(tmp_path: str):
            comm = edge_tts.Communicate(text, self.EDGE_VOICE, rate=rate_str)
            await comm.save(tmp_path)

        asyncio.run(_generate(os.fspath(path)))

    def _speak_edge(
        self,
        text: str,
        speed: float,
        edge_tts,
        generation_id: Optional[int] = None,
    ) -> None:
        """Generate speech with Edge TTS and play the resulting audio."""
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            tmp = f.name

        try:
            self._raise_if_cancelled(generation_id)
            self._save_edge_audio(text, speed, edge_tts, tmp)
            self._play_file(tmp, generation_id)
        finally:
            try:
                os.unlink(tmp)
            except OSError:
                pass

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

        proc.wait(timeout=120)

        with self._lock:
            if self._process is proc:
                self._process = None

    def stop(self) -> None:
        """Stop any in-progress speech."""
        self._cancel_generation()
        with self._publish_lock:
            pass
        with self._lock:
            proc = self._process

        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass

            with self._lock:
                self._process = None
