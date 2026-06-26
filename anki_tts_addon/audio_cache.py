import hashlib
import json
import os
import threading
import uuid
from pathlib import Path


CACHE_FORMAT_VERSION = "edge-mp3-v1"
MIN_AUDIO_BYTES = 1024


class AudioTooSmallError(ValueError):
    """Raised when generated audio is too small to be a usable clip.

    This is a soft, content-level failure (a truncated/near-empty clip), not a
    sign that the TTS backend itself is unavailable.
    """


def speech_cache_key(text, voice, speed, version=CACHE_FORMAT_VERSION):
    payload = {
        "text": text,
        "voice": voice,
        "speed": round(float(speed), 3),
        "version": version,
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


class AudioCache:
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self._generation_locks = {}
        self._generation_locks_lock = threading.Lock()

    def path_for_key(self, key):
        return self.root / f"{key}.mp3"

    def temp_path_for_key(self, key):
        nonce = f"{os.getpid()}.{threading.get_ident()}.{uuid.uuid4().hex}"
        return self.root / f"{key}.{nonce}.tmp"

    def generation_lock(self, key):
        with self._generation_locks_lock:
            lock = self._generation_locks.get(key)
            if lock is None:
                lock = threading.Lock()
                self._generation_locks[key] = lock
            return lock

    def has(self, key):
        return self._is_valid_audio_file(self.path_for_key(key))

    def get(self, key):
        path = self.path_for_key(key)
        if not self._is_valid_audio_file(path):
            return None

        try:
            os.utime(path, None)
            return path
        except FileNotFoundError:
            return None

    def store_from_temp(self, key, tmp_path):
        tmp_path = Path(tmp_path)
        try:
            size = tmp_path.stat().st_size
        except FileNotFoundError:
            self._remove_path(tmp_path)
            raise ValueError("Audio temp file is missing")

        if size < MIN_AUDIO_BYTES:
            self._remove_path(tmp_path)
            raise AudioTooSmallError(
                f"Audio temp file is too small: {size} bytes"
            )

        final_path = self.path_for_key(key)
        os.replace(tmp_path, final_path)
        return final_path

    def remove(self, key):
        self._remove_path(self.path_for_key(key))

    def iter_audio_files(self):
        for path in self.root.glob("*.mp3"):
            if path.is_file():
                yield path

    def cleanup_temp_files(self):
        removed = 0
        for path in self.root.glob("*.tmp"):
            if not path.is_file():
                continue
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                removed += 1
        return removed

    def cleanup(self, max_bytes):
        entries = []
        total_bytes = 0
        for path in self.iter_audio_files():
            try:
                stat = path.stat()
            except FileNotFoundError:
                continue
            entries.append((stat.st_mtime, path, stat.st_size))
            total_bytes += stat.st_size

        removed = 0
        for _, path, size in sorted(entries):
            if total_bytes <= max_bytes:
                break
            try:
                path.unlink()
            except FileNotFoundError:
                pass
            else:
                total_bytes -= size
                removed += 1

        return removed

    @staticmethod
    def _remove_path(path):
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    @staticmethod
    def _is_valid_audio_file(path):
        try:
            return path.is_file() and path.stat().st_size >= MIN_AUDIO_BYTES
        except FileNotFoundError:
            return False
