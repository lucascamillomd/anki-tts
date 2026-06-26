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
                {"pending": 0, "succeeded": 0, "failed": 0},
            )

    def test_corrupt_state_file_loads_empty_summary(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            path.write_text("{not-json", encoding="utf-8")

            state = AudioCacheState(path, now=Clock(100))

            self.assertEqual(
                state.summary(),
                {"pending": 0, "succeeded": 0, "failed": 0},
            )

    def test_mark_pending_persists_key_preview_and_timestamp(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            state = AudioCacheState(path, now=Clock(100))

            state.mark_pending("abc", "hello world")

            saved = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(saved["entries"]["abc"]["status"], "pending")
            self.assertEqual(saved["entries"]["abc"]["preview"], "hello world")
            self.assertEqual(saved["entries"]["abc"]["updated_at"], 100)
            self.assertEqual(state.summary()["pending"], 1)

    def test_mark_succeeded_clears_failure_metadata(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            path = Path(tmpdir) / "state.json"
            clock = Clock(100)
            state = AudioCacheState(path, now=clock)
            state.mark_failed("abc", "network failed")
            clock.value = 200

            state.mark_succeeded("abc", "hello")

            entry = state.entry("abc")
            self.assertEqual(entry["status"], "succeeded")
            self.assertEqual(entry["attempts"], 0)
            self.assertIsNone(entry["last_error"])
            self.assertIsNone(entry["next_retry_at"])
            self.assertEqual(entry["updated_at"], 200)

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
                {"pending": 1, "succeeded": 1, "failed": 1},
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
                {"pending": 0, "succeeded": 0, "failed": 0},
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
                state.mark_pending("first", "first")
                state.mark_pending("second", "second")
            finally:
                _MODULE.os.replace = original_replace

            self.assertEqual(len(replace_sources), 2)
            self.assertEqual(len(set(replace_sources)), 2)


if __name__ == "__main__":
    unittest.main()
