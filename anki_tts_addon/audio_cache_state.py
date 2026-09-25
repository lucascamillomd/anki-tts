import json
import logging
import os
import threading
import time
import uuid
from pathlib import Path


log = logging.getLogger(__name__)

RETRY_DELAYS_SECONDS = (30, 120, 600)
STATE_VERSION = 2
FLUSH_INTERVAL_SECONDS = 5.0
MAX_TEXT_CHARS = 1000


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
    """Retry bookkeeping for background audio generation.

    Success is proven by the cache file itself, so it is never stored.
    Pending entries live in memory only. Only failed entries (with their
    backoff and text, so they can be retried without re-rendering the card)
    and a few metadata values are persisted.

    ``mark_*`` only touch memory and set a dirty flag. Callers persist with
    ``flush()`` / ``flush_if_due()`` from a background thread and once more
    when the profile closes.
    """

    def __init__(self, path, now=None, monotonic=None):
        self.path = Path(path)
        self._now = now or time.time
        self._monotonic = monotonic or time.monotonic
        self._lock = threading.RLock()
        self._save_lock = threading.Lock()
        self._dirty = False
        self._last_flush = None
        self._meta = {}
        self._entries = self._load()
        self._remove_stale_temp_files()

    def entry(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None:
                return None
            return dict(entry)

    def mark_pending(self, key, preview=""):
        """Mark key as queued. Returns False when it already was pending."""
        with self._lock:
            previous = self._entries.get(key)
            if previous is not None and previous.get("status") == "pending":
                return False
            entry = self._base_entry(key, preview)
            entry["status"] = "pending"
            self._entries[key] = entry
            # Pending is memory-only; the persisted form of a retried failed
            # entry is unchanged, so this never dirties the state file.
            return True

    def cancel_pending(self, key):
        """Undo mark_pending (enqueue refused or the item was discarded)."""
        with self._lock:
            self._cancel_pending_locked(key)

    def clear_pending(self):
        with self._lock:
            for key in [
                key for key, entry in self._entries.items()
                if entry.get("status") == "pending"
            ]:
                self._cancel_pending_locked(key)

    def mark_succeeded(self, key, preview=""):
        with self._lock:
            previous = self._entries.pop(key, None)
            if previous is None:
                return False
            if _as_int(previous.get("attempts"), 0) > 0:
                self._dirty = True
            return True

    def mark_failed(self, key, error, preview=""):
        with self._lock:
            previous = self._entries.get(key, {})
            attempts = int(previous.get("attempts") or 0) + 1
            delay = RETRY_DELAYS_SECONDS[
                min(attempts - 1, len(RETRY_DELAYS_SECONDS) - 1)
            ]
            entry = self._base_entry(key, preview)
            entry["status"] = "failed"
            entry["attempts"] = attempts
            entry["last_error"] = str(error)[:240]
            entry["next_retry_at"] = self._timestamp() + delay
            self._entries[key] = entry
            self._dirty = True

    def can_retry(self, key):
        with self._lock:
            entry = self._entries.get(key)
            if entry is None or entry.get("status") != "failed":
                return True
            next_retry_at = entry.get("next_retry_at")
            return next_retry_at is None or self._timestamp() >= next_retry_at

    def retry_failures_now(self):
        """Make every failed entry retryable immediately (resets backoff).

        The cards are re-queued by the next backlog scan. Returns the count.
        """
        with self._lock:
            count = 0
            for entry in self._entries.values():
                if entry.get("status") != "failed":
                    continue
                # Keep attempts: entries with attempts are what gets persisted.
                entry["next_retry_at"] = None
                count += 1
            if count:
                self._dirty = True
            return count

    def prune_failures(self, keep_keys):
        """Drop failed entries whose key is not in ``keep_keys``; return count."""
        keep = set(keep_keys)
        with self._lock:
            stale = [
                key for key, entry in self._entries.items()
                if entry.get("status") == "failed" and key not in keep
            ]
            for key in stale:
                del self._entries[key]
            if stale:
                self._dirty = True
            return len(stale)

    def has_failures_without_text(self):
        """True when some failed entry can only be retried by a card rescan."""
        with self._lock:
            return any(
                entry.get("status") == "failed" and not entry.get("text")
                for entry in self._entries.values()
            )

    def summary(self):
        with self._lock:
            counts = {"pending": 0, "failed": 0}
            for entry in self._entries.values():
                status = entry.get("status")
                if status in counts:
                    counts[status] += 1
            return counts

    def get_meta(self, name, default=None):
        with self._lock:
            return self._meta.get(name, default)

    def set_meta(self, name, value):
        with self._lock:
            if self._meta.get(name) == value:
                return
            self._meta[name] = value
            self._dirty = True

    def clear(self):
        with self._lock:
            self._entries = {}
            self._meta = {}
            self._dirty = True
        self.flush()

    def is_dirty(self):
        with self._lock:
            return self._dirty

    def flush(self):
        """Write the state file if anything persisted changed.

        Never raises: a failed write is logged and retried on the next flush.
        """
        with self._save_lock:
            with self._lock:
                if not self._dirty:
                    return False
                payload = self._payload_locked()
                self._dirty = False
                self._last_flush = self._monotonic()
            try:
                self._write(payload)
            except OSError as e:
                log.warning("Could not save TTS audio cache state: %s", e)
                with self._lock:
                    self._dirty = True
                return False
            return True

    def flush_if_due(self, interval=FLUSH_INTERVAL_SECONDS):
        with self._lock:
            if not self._dirty:
                return False
            if (
                self._last_flush is not None
                and self._monotonic() - self._last_flush < interval
            ):
                return False
        return self.flush()

    def _cancel_pending_locked(self, key):
        entry = self._entries.get(key)
        if entry is None or entry.get("status") != "pending":
            return
        if _as_int(entry.get("attempts"), 0) > 0:
            entry["status"] = "failed"
        else:
            del self._entries[key]

    def _base_entry(self, key, preview):
        previous = self._entries.get(key, {})
        text = str(preview or previous.get("text") or previous.get("preview") or "")
        return {
            "key": key,
            "preview": self._preview(text),
            "text": text[:MAX_TEXT_CHARS],
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

    def _payload_locked(self):
        entries = {}
        for key, entry in self._entries.items():
            if _as_int(entry.get("attempts"), 0) <= 0:
                continue
            # A retried failure that is queued again is still a failure on
            # disk: if Anki quits before it runs, its backoff survives.
            persisted = dict(entry)
            persisted["status"] = "failed"
            entries[key] = persisted
        return {
            "version": STATE_VERSION,
            "entries": entries,
            "meta": dict(self._meta),
        }

    def _load(self):
        try:
            data = json.loads(self.path.read_text(encoding="utf-8"))
        except (FileNotFoundError, json.JSONDecodeError, OSError, ValueError):
            return {}
        if not isinstance(data, dict):
            return {}
        meta = data.get("meta", {})
        if isinstance(meta, dict):
            self._meta = dict(meta)
        entries = data.get("entries", {})
        if not isinstance(entries, dict):
            return {}
        # Keep only retryable failures. Legacy entries without card text
        # (older versions logged a generic "prefetch failed") can't be retried
        # from here and their backoff would make scans skip the card, so they
        # are dropped and the card is simply re-found by the next scan.
        # "Edge TTS not installed" was once recorded per card; it is an
        # install problem, not a card failure, so those entries are dropped.
        loaded = {
            str(key): self._normalize_entry(entry)
            for key, entry in entries.items()
            if isinstance(entry, dict)
            and entry.get("status") == "failed"
            and entry.get("text")
            and not str(entry.get("last_error") or "").startswith(
                "Edge TTS not installed"
            )
        }
        if len(loaded) != len(entries):
            # Drop legacy/pending/succeeded entries from disk on next flush.
            self._dirty = True
        return loaded

    @staticmethod
    def _normalize_entry(entry):
        """Coerce numeric fields so a corrupt/foreign file can't crash later."""
        normalized = dict(entry)
        normalized["attempts"] = _as_int(entry.get("attempts"), 0)
        normalized["next_retry_at"] = _as_optional_int(entry.get("next_retry_at"))
        return normalized

    def _remove_stale_temp_files(self):
        """Remove temp files left behind when a quit interrupted a write."""
        try:
            candidates = list(self.path.parent.glob(f"{self.path.name}.*.tmp"))
        except OSError:
            return
        for path in candidates:
            try:
                path.unlink()
            except OSError:
                pass

    def _write(self, payload):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.parent / (
            f"{self.path.name}.{os.getpid()}.{threading.get_ident()}."
            f"{uuid.uuid4().hex}.tmp"
        )
        try:
            tmp.write_text(
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    separators=(",", ":"),
                ),
                encoding="utf-8",
            )
            os.replace(tmp, self.path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
