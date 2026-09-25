import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module


_MODULE = load_addon_module("audio_cache_state")
AudioCacheState = _MODULE.AudioCacheState


class Clock:
    def __init__(self, value):
        self.value = value

    def __call__(self):
        return self.value


class AudioCacheStateTests(unittest.TestCase):
    def test_missing_state_file_loads_empty_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = AudioCacheState(Path(tmpdir) / "state.json", now=Clock(100))

            self.assertEqual(
                state.summary(),
                {"pending": 0, "failed": 0},
            )

    def test_corrupt_state_file_loads_empty_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("{not-json", encoding="utf-8")

            state = AudioCacheState(path, now=Clock(100))

            self.assertEqual(
                state.summary(),
                {"pending": 0, "failed": 0},
            )

    def test_mark_pending_is_memory_only_and_does_not_write_file(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))

            self.assertTrue(state.mark_pending("abc", "hello world"))
            self.assertFalse(state.mark_pending("abc", "hello world"))

            entry = state.entry("abc")
            self.assertEqual(entry["status"], "pending")
            self.assertEqual(entry["preview"], "hello world")
            self.assertEqual(entry["updated_at"], 100)
            self.assertEqual(state.summary()["pending"], 1)
            self.assertFalse(state.is_dirty())
            state.flush()
            self.assertFalse(path.exists())

    def test_mark_calls_do_not_write_until_flush(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))

            state.mark_failed("abc", "network failed", "hello")

            self.assertFalse(path.exists())
            self.assertTrue(state.flush())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["entries"]["abc"]["status"], "failed")
            self.assertEqual(saved["entries"]["abc"]["text"], "hello")
            self.assertFalse(state.flush())

    def test_flush_writes_compact_json_with_only_failed_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            state.mark_pending("pending", "pending")
            state.mark_failed("failed", "boom", "failed")

            state.flush()

            raw = path.read_text(encoding="utf-8")
            self.assertNotIn("\n", raw)
            self.assertNotIn(": ", raw)
            self.assertEqual(set(json.loads(raw)["entries"]), {"failed"})

    def test_flush_if_due_debounces_writes(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            monotonic = Clock(0)
            state = AudioCacheState(path, now=Clock(100), monotonic=monotonic)

            state.mark_failed("a", "boom")
            self.assertTrue(state.flush_if_due(interval=5))
            state.mark_failed("b", "boom")
            monotonic.value = 4
            self.assertFalse(state.flush_if_due(interval=5))
            monotonic.value = 5
            self.assertTrue(state.flush_if_due(interval=5))
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["entries"]), {"a", "b"})

    def test_load_drops_pending_and_succeeded_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            "p": {"key": "p", "status": "pending"},
                            "s": {"key": "s", "status": "succeeded"},
                            "f": {"key": "f", "status": "failed", "attempts": 1},
                        },
                    }
                ),
                encoding="utf-8",
            )

            state = AudioCacheState(path, now=Clock(100))

            self.assertIsNone(state.entry("p"))
            self.assertIsNone(state.entry("s"))
            self.assertEqual(state.summary(), {"pending": 0, "failed": 1})
            self.assertTrue(state.flush())
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(set(saved["entries"]), {"f"})

    def test_load_removes_leftover_temp_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            stale = Path(tmpdir) / "state.json.1.2.abc.tmp"
            stale.write_text("partial", encoding="utf-8")
            other = Path(tmpdir) / "other.tmp"
            other.write_text("keep", encoding="utf-8")

            AudioCacheState(path, now=Clock(100))

            self.assertFalse(stale.exists())
            self.assertTrue(other.exists())

    def test_failed_write_removes_temp_file_and_stays_dirty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            state.mark_failed("abc", "boom")
            original_replace = _MODULE.os.replace

            def failing_replace(src, dst):
                raise OSError("disk full")

            try:
                _MODULE.os.replace = failing_replace
                with self.assertLogs(_MODULE.log, level="WARNING"):
                    self.assertFalse(state.flush())
            finally:
                _MODULE.os.replace = original_replace

            self.assertEqual(list(Path(tmpdir).glob("*.tmp")), [])
            self.assertTrue(state.is_dirty())
            self.assertTrue(state.flush())

    def test_mark_succeeded_removes_entry_and_failure_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            state.mark_failed("abc", "network failed")
            state.flush()

            self.assertTrue(state.mark_succeeded("abc", "hello"))
            self.assertFalse(state.mark_succeeded("abc", "hello"))

            self.assertIsNone(state.entry("abc"))
            self.assertTrue(state.can_retry("abc"))
            state.flush()
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["entries"], {})

    def test_mark_succeeded_on_unknown_key_is_a_no_op(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = AudioCacheState(Path(tmpdir) / "state.json", now=Clock(100))

            self.assertFalse(state.mark_succeeded("abc", "hello"))
            self.assertFalse(state.is_dirty())

    def test_cancel_pending_restores_failed_backoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = Clock(100)
            state = AudioCacheState(Path(tmpdir) / "state.json", now=clock)
            state.mark_failed("failed", "boom", "failed text")
            clock.value = 200
            state.mark_pending("failed", "failed text")
            state.mark_pending("new", "new text")

            state.cancel_pending("failed")
            state.cancel_pending("new")

            entry = state.entry("failed")
            self.assertEqual(entry["status"], "failed")
            self.assertEqual(entry["attempts"], 1)
            self.assertEqual(entry["next_retry_at"], 130)
            self.assertIsNone(state.entry("new"))

    def test_pending_retry_of_failed_entry_persists_as_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            state.mark_failed("abc", "boom", "text")
            state.mark_pending("abc", "text")

            state.flush()
            reloaded = AudioCacheState(path, now=Clock(100))

            self.assertEqual(reloaded.entry("abc")["status"], "failed")
            self.assertEqual(reloaded.entry("abc")["attempts"], 1)

    def test_retry_candidates_returns_expired_failed_entries_with_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            clock = Clock(100)
            state = AudioCacheState(Path(tmpdir) / "state.json", now=clock)
            state.mark_failed("a", "boom", "text a")
            state.mark_failed("b", "boom")

            self.assertEqual(state.retry_candidates(), [])
            clock.value = 130

            self.assertEqual(state.retry_candidates(), [("a", "text a")])

    def test_meta_round_trips_through_flush(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            state.set_meta("watermark", {"mod": 5})
            state.flush()

            reloaded = AudioCacheState(path, now=Clock(100))

            self.assertEqual(reloaded.get_meta("watermark"), {"mod": 5})

    def test_mark_failed_records_attempts_error_and_backoff(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            clock = Clock(100)
            state = AudioCacheState(path, now=clock)

            state.mark_failed("abc", "network failed")
            first = state.entry("abc")
            clock.value = 200
            state.mark_failed("abc", "still failed")
            second = state.entry("abc")

            self.assertEqual(first["status"], "failed")
            self.assertEqual(first["attempts"], 1)
            self.assertEqual(first["last_error"], "network failed")
            self.assertEqual(first["next_retry_at"], 130)
            self.assertEqual(second["attempts"], 2)
            self.assertEqual(second["last_error"], "still failed")
            self.assertEqual(second["next_retry_at"], 320)
            self.assertEqual(state.summary()["failed"], 1)

    def test_can_retry_respects_failed_backoff_window(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            clock = Clock(100)
            state = AudioCacheState(path, now=clock)

            self.assertTrue(state.can_retry("missing"))
            state.mark_failed("abc", "network failed")
            self.assertFalse(state.can_retry("abc"))
            clock.value = 130

            self.assertTrue(state.can_retry("abc"))

    def test_summary_counts_current_status_per_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = AudioCacheState(Path(tmpdir) / "state.json", now=Clock(100))

            state.mark_pending("pending", "pending")
            state.mark_succeeded("succeeded", "succeeded")
            state.mark_failed("failed", "failed")

            self.assertEqual(
                state.summary(),
                {"pending": 1, "failed": 1},
            )

    def test_clear_removes_all_entries_and_persists_empty_state(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            state.mark_pending("pending", "pending")
            state.mark_succeeded("succeeded", "succeeded")
            state.mark_failed("failed", "failed")

            state.clear()

            self.assertEqual(
                state.summary(),
                {"pending": 0, "failed": 0},
            )
            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["entries"], {})

    def test_corrupt_attempts_field_does_not_crash_mark_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            "abc": {
                                "key": "abc",
                                "status": "failed",
                                "attempts": "not-a-number",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = AudioCacheState(path, now=Clock(100))

            state.mark_failed("abc", "boom")

            entry = state.entry("abc")
            self.assertEqual(entry["attempts"], 1)
            self.assertEqual(entry["next_retry_at"], 130)

    def test_corrupt_next_retry_at_does_not_crash_can_retry(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(
                json.dumps(
                    {
                        "version": 1,
                        "entries": {
                            "abc": {
                                "key": "abc",
                                "status": "failed",
                                "attempts": 1,
                                "next_retry_at": "whenever",
                            }
                        },
                    }
                ),
                encoding="utf-8",
            )
            state = AudioCacheState(path, now=Clock(100))

            self.assertTrue(state.can_retry("abc"))

    def test_each_save_uses_unique_temp_file_to_avoid_replace_races(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            original_replace = _MODULE.os.replace
            replace_sources = []

            def record_replace(src, dst):
                replace_sources.append(Path(src).name)
                original_replace(src, dst)

            try:
                _MODULE.os.replace = record_replace
                state.mark_failed("first", "first")
                state.flush()
                state.mark_failed("second", "second")
                state.flush()
            finally:
                _MODULE.os.replace = original_replace

            self.assertEqual(len(replace_sources), 2)
            self.assertEqual(len(set(replace_sources)), 2)


class RetryFailuresTests(unittest.TestCase):
    def test_retry_failures_now_clears_backoff_but_keeps_failures_on_disk(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))
            state.mark_failed("k1", "request timed out", "hello")
            state.mark_failed("k2", "network", "world")
            self.assertFalse(state.can_retry("k1"))

            self.assertEqual(state.retry_failures_now(), 2)

            self.assertTrue(state.can_retry("k1"))
            self.assertEqual(
                sorted(state.retry_candidates()),
                [("k1", "hello"), ("k2", "world")],
            )
            state.flush()
            reloaded = AudioCacheState(path, now=Clock(100))
            self.assertEqual(reloaded.summary()["failed"], 2)

    def test_retry_failures_now_with_nothing_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            state = AudioCacheState(Path(tmpdir) / "s.json", now=Clock(100))
            self.assertEqual(state.retry_failures_now(), 0)
            self.assertFalse(state.is_dirty())

    def test_has_failures_without_text_detects_legacy_entries(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text(json.dumps({
                "version": 1,
                "entries": {"old": {"status": "failed", "attempts": 1,
                                    "last_error": "prefetch failed"}},
            }), encoding="utf-8")
            state = AudioCacheState(path, now=Clock(100))
            self.assertTrue(state.has_failures_without_text())
            fresh = AudioCacheState(Path(tmpdir) / "new.json", now=Clock(100))
            fresh.mark_failed("k", "e", "text")
            self.assertFalse(fresh.has_failures_without_text())


if __name__ == "__main__":
    unittest.main()
