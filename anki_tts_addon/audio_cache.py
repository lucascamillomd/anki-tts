import hashlib
import json
import os
import threading
import time
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
        # Total bytes of cached mp3s, tracked incrementally so the live path
        # never has to scan the directory. None until first computed.
        self._size_lock = threading.Lock()
        self._total_bytes = None

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
        try:
            replaced_size = final_path.stat().st_size
        except OSError:
            replaced_size = 0
        os.replace(tmp_path, final_path)
        self._adjust_total_bytes(size - replaced_size)
        return final_path

    def remove(self, key):
        path = self.path_for_key(key)
        try:
            size = path.stat().st_size
        except OSError:
            return
        self._remove_path(path)
        self._adjust_total_bytes(-size)

    def cached_keys(self):
        """Set of keys with a cached mp3, from a single directory scan."""
        keys = set()
        for entry in self._scan_audio_entries():
            keys.add(entry.name[:-len(".mp3")])
        return keys

    def total_bytes(self):
        with self._size_lock:
            if self._total_bytes is not None:
                return self._total_bytes
        total = 0
        for entry in self._scan_audio_entries():
            try:
                total += entry.stat().st_size
            except OSError:
                continue
        with self._size_lock:
            self._total_bytes = total
            return total

    def known_total_bytes(self):
        """Tracked total without touching the disk; None until computed."""
        with self._size_lock:
            return self._total_bytes

    def remove_all_audio_files(self):
        """Unlink every cached mp3 (slow on big caches: run off the UI thread)."""
        removed = 0
        for entry in self._scan_audio_entries():
            try:
                os.unlink(entry.path)
            except OSError:
                continue
            removed += 1
        with self._size_lock:
            self._total_bytes = None
        return removed

    def invalidate_size(self):
        with self._size_lock:
            self._total_bytes = None

    def cleanup_if_needed(self, max_bytes, target_bytes=None):
        """Evict old files only when the tracked total exceeds max_bytes."""
        if self.total_bytes() <= max_bytes:
            return 0
        return self.cleanup(
            max_bytes if target_bytes is None else min(target_bytes, max_bytes)
        )

    def iter_audio_files(self):
        for path in self.root.glob("*.mp3"):
            if path.is_file():
                yield path

    def cleanup_temp_files(self, min_age_seconds=0):
        """Remove leftover *.tmp files.

        ``min_age_seconds`` skips recent files, so a cleanup running in the
        background never deletes a clip that is being generated right now.
        """
        removed = 0
        cutoff = time.time() - min_age_seconds
        try:
            entries = [
                entry for entry in os.scandir(self.root)
                if entry.name.endswith(".tmp")
            ]
        except OSError:
            return 0
        for entry in entries:
            try:
                if not entry.is_file():
                    continue
                if min_age_seconds and entry.stat().st_mtime > cutoff:
                    continue
                os.unlink(entry.path)
            except OSError:
                continue
            removed += 1
        return removed

    def cleanup(self, max_bytes):
        entries = []
        total_bytes = 0
        for entry in self._scan_audio_entries():
            try:
                stat = entry.stat()
            except OSError:
                continue
            entries.append((stat.st_mtime, entry.path, stat.st_size))
            total_bytes += stat.st_size

        removed = 0
        for _, path, size in sorted(entries):
            if total_bytes <= max_bytes:
                break
            try:
                os.unlink(path)
            except FileNotFoundError:
                pass
            else:
                total_bytes -= size
                removed += 1

        with self._size_lock:
            self._total_bytes = total_bytes
        return removed

    def _adjust_total_bytes(self, delta):
        with self._size_lock:
            if self._total_bytes is not None:
                self._total_bytes = max(0, self._total_bytes + delta)

    def _scan_audio_entries(self):
        try:
            with os.scandir(self.root) as it:
                entries = [
                    entry for entry in it
                    if entry.name.endswith(".mp3") and entry.is_file()
                ]
        except FileNotFoundError:
            return []
        return entries

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
