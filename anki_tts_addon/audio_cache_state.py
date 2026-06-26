import json
import os
import threading
import time
import uuid
from pathlib import Path


RETRY_DELAYS_SECONDS = (30, 120, 600)
STATE_VERSION = 1


def _as_int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_optional_int(value):
    if value is None:
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


class AudioCacheState:
    def __init__(self, path, now=None):
        self.path = Path(path)
        self._now = now or time.time
        self._lock = threading.RLock()
        self._entries = self._load()

    def entry(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            return dict(entry)

    def mark_pending(self, key, preview=""):
        with self._lock:
            entry = self._base_entry(key, preview)
            entry["status"] = "pending"
            self._entries[key] = entry
            self._save()

    def mark_succeeded(self, key, preview=""):
        with self._lock:
            entry = self._base_entry(key, preview)
            entry["status"] = "succeeded"
            entry["attempts"] = 0
            entry["last_error"] = None
            entry["next_retry_at"] = None
            self._entries[key] = entry
            self._save()

    def mark_failed(self, key, error, preview=""):
        with self._lock:
            previous = self._entries.get(key, {})
            attempts = int(previous.get("attempts") or 0) + 1
            delay = RETRY_DELAYS_SECONDS[
                min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)
            ]
            entry = self._base_entry(
                key, preview or previous.get("preview", "")
            )
            entry["status"] = "failed"
            entry["attempts"] = attempts
            entry["last_error"] = str(error)[:240]
            entry["next_retry_at"] = self._timestamp() + delay
            self._entries[key] = entry
            self._save()

    def can_retry(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.get("status") != "failed":
                return True
            next_retry_at = entry.get("next_retry_at")
            return next_retry_at is None or self._timestamp() >= next_retry_at

    def summary(self):
        with self._lock:
            counts = {"pending": 0, "succeeded": 0, "failed": 0}
            for entry in self._entries.values():
                status = entry.get("status")
                if status in counts:
                    counts[status] += 1
            return counts

    def clear(self):
        with self._lock:
            self._entries = {}
            self._save()

    def _base_entry(self, key, preview):
        previous = self._entries.get(key, {})
        return {
            "key": key,
            "preview": self._preview(preview or previous.get("preview", "")),
            "status": previous.get("status", "pending"),
            "attempts": int(previous.get("attempts") or 0),
            "last_error": previous.get("last_error"),
            "next_retry_at": previous.get("next_retry_at"),
            "updated_at": self._timestamp(),
        }

    def _timestamp(self):
        return int(self._now())

    @staticmethod
    def _preview(text):
        return " ".join(str(text).split())[:120]

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError):
            return {}
        if not isinstance(data, dict):
            return {}
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            return {}
        return {
            str(key): self._normalize_entry(entry)
            for key, entry in entries.items()
            if isinstance(entry, dict)
        }

    @staticmethod
    def _normalize_entry(entry):
        """Coerce numeric fields so a corrupt/foreign file can't crash later."""
        normalized = dict(entry)
        normalized["attempts"] = _as_int(entry.get("attempts"), 0)
        normalized["next_retry_at"] = _as_optional_int(entry.get("next_retry_at"))
        return normalized

    def _save(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}."
            f"{uuid.uuid4().hex}.tmp"
        )
        payload = {
            "version": STATE_VERSION,
            "entries": self._entries,
        }
        tmp.write_text(
            json.dumps(
                payload,
                ensure_ascii=False,
                sort_keys=True,
                indent=2,
            ),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)
