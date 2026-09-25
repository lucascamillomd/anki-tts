import os
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module

_MODULE = load_addon_module("audio_cache")
AudioCache = _MODULE.AudioCache
MIN_AUDIO_BYTES = _MODULE.MIN_AUDIO_BYTES
speech_cache_key = _MODULE.speech_cache_key


def _valid_audio_bytes(fill=b"m"):
    return fill * MIN_AUDIO_BYTES


class AudioCacheTests(unittest.TestCase):
    def test_known_total_bytes_never_scans(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            (Path(tmpdir) / "a.mp3").write_bytes(_valid_audio_bytes())
            original = os.scandir

            def fail(*_a, **_k):
                raise AssertionError("scandir called")

            _MODULE.os.scandir = fail
            try:
                self.assertIsNone(cache.known_total_bytes())
            finally:
                _MODULE.os.scandir = original
            self.assertEqual(cache.total_bytes(), MIN_AUDIO_BYTES)
            self.assertEqual(cache.known_total_bytes(), MIN_AUDIO_BYTES)

    def test_remove_all_audio_files_keeps_other_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            for name in ("a.mp3", "b.mp3"):
                (root / name).write_bytes(_valid_audio_bytes())
            (root / "keep.txt").write_text("x")
            cache.total_bytes()

            self.assertEqual(cache.remove_all_audio_files(), 2)

            self.assertEqual(sorted(p.name for p in root.iterdir()), ["keep.txt"])
            self.assertIsNone(cache.known_total_bytes())
            self.assertEqual(cache.total_bytes(), 0)

    def test_key_changes_when_text_voice_or_speed_changes(self):
        key = speech_cache_key("hello", "en-GB-RyanNeural", 1.25)

        self.assertEqual(key, speech_cache_key("hello", "en-GB-RyanNeural", 1.25))
        self.assertNotEqual(key, speech_cache_key("hello!", "en-GB-RyanNeural", 1.25))
        self.assertNotEqual(key, speech_cache_key("hello", "en-US-GuyNeural", 1.25))
        self.assertNotEqual(key, speech_cache_key("hello", "en-GB-RyanNeural", 1.0))
        self.assertRegex(key, r"^[0-9a-f]{64}$")

    def test_store_from_temp_moves_mp3_atomically(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            tmp = cache.temp_path_for_key(key)
            tmp.write_bytes(_valid_audio_bytes())

            final = cache.store_from_temp(key, tmp)

            self.assertEqual(final, cache.path_for_key(key))
            self.assertTrue(final.exists())
            self.assertEqual(final.read_bytes(), _valid_audio_bytes())
            self.assertFalse(tmp.exists())

    def test_temp_path_for_key_stays_in_cache_dir_and_is_unique(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)

            first = cache.temp_path_for_key(key)
            second = cache.temp_path_for_key(key)

            self.assertEqual(first.parent, cache.root)
            self.assertEqual(second.parent, cache.root)
            self.assertNotEqual(first, second)
            self.assertTrue(first.name.startswith(f"{key}."))
            self.assertEqual(first.suffix, ".tmp")

    def test_store_from_temp_rejects_empty_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            tmp = cache.temp_path_for_key(key)
            tmp.write_bytes(b"")

            with self.assertRaises(ValueError):
                cache.store_from_temp(key, tmp)

            self.assertFalse(cache.path_for_key(key).exists())
            self.assertFalse(tmp.exists())

    def test_store_from_temp_rejects_missing_temp_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            tmp = cache.temp_path_for_key(key)

            with self.assertRaises(ValueError):
                cache.store_from_temp(key, tmp)

            self.assertFalse(cache.path_for_key(key).exists())

    def test_store_from_temp_rejects_tiny_audio(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            tmp = cache.temp_path_for_key(key)
            tmp.write_bytes(b"x" * (MIN_AUDIO_BYTES - 1))

            with self.assertRaises(ValueError):
                cache.store_from_temp(key, tmp)

            self.assertFalse(cache.path_for_key(key).exists())
            self.assertFalse(tmp.exists())

    def test_has_and_get_require_valid_size_and_get_touches_mtime(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            path = cache.path_for_key(key)

            path.write_bytes(b"x" * (MIN_AUDIO_BYTES - 1))
            self.assertFalse(cache.has(key))
            self.assertIsNone(cache.get(key))

            path.write_bytes(_valid_audio_bytes())
            old_time = 1_600_000_000
            os.utime(path, (old_time, old_time))

            self.assertTrue(cache.has(key))
            self.assertEqual(cache.get(key), path)
            self.assertGreater(path.stat().st_mtime, old_time)

    def test_remove_ignores_missing_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            path = cache.path_for_key(key)
            path.write_bytes(_valid_audio_bytes())

            cache.remove(key)
            cache.remove(key)

            self.assertFalse(path.exists())

    def test_iter_audio_files_yields_only_mp3_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            (root / "first.mp3").write_bytes(_valid_audio_bytes(b"a"))
            (root / "second.mp3").write_bytes(_valid_audio_bytes(b"b"))
            (root / "ignored.tmp").write_bytes(_valid_audio_bytes(b"c"))
            (root / "directory.mp3").mkdir()

            self.assertEqual(
                sorted(path.name for path in cache.iter_audio_files()),
                ["first.mp3", "second.mp3"],
            )

    def test_cleanup_temp_files_removes_only_tmp_files(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            first_tmp = root / "first.tmp"
            second_tmp = root / "second.tmp"
            audio_file = root / "keep.mp3"
            directory = root / "directory.tmp"
            first_tmp.write_bytes(b"partial")
            second_tmp.write_bytes(b"partial")
            audio_file.write_bytes(_valid_audio_bytes())
            directory.mkdir()

            removed = cache.cleanup_temp_files()

            self.assertEqual(removed, 2)
            self.assertFalse(first_tmp.exists())
            self.assertFalse(second_tmp.exists())
            self.assertTrue(audio_file.exists())
            self.assertTrue(directory.exists())

    def test_cleanup_temp_files_keeps_recent_files_when_min_age_given(self):
        import os
        import time

        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            old_tmp = root / "old.tmp"
            fresh_tmp = root / "fresh.tmp"
            old_tmp.write_bytes(b"partial")
            fresh_tmp.write_bytes(b"in progress")
            old = time.time() - 3600
            os.utime(old_tmp, (old, old))

            removed = cache.cleanup_temp_files(min_age_seconds=300)

            self.assertEqual(removed, 1)
            self.assertFalse(old_tmp.exists())
            self.assertTrue(fresh_tmp.exists())

    def test_cleanup_removes_oldest_mp3_files_until_under_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            old = root / "old.mp3"
            middle = root / "middle.mp3"
            newest = root / "newest.mp3"
            ignored = root / "ignored.tmp"
            for path in (old, middle, newest, ignored):
                path.write_bytes(b"x" * MIN_AUDIO_BYTES)
            os.utime(old, (100, 100))
            os.utime(middle, (200, 200))
            os.utime(newest, (300, 300))

            removed = cache.cleanup(MIN_AUDIO_BYTES)

            self.assertEqual(removed, 2)
            self.assertFalse(old.exists())
            self.assertFalse(middle.exists())
            self.assertTrue(newest.exists())
            self.assertTrue(ignored.exists())

    def test_cached_keys_lists_mp3_stems_from_one_scan(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            (root / "a.mp3").write_bytes(_valid_audio_bytes())
            (root / "b.mp3").write_bytes(_valid_audio_bytes())
            (root / "c.tmp").write_bytes(_valid_audio_bytes())
            (root / "d.mp3").mkdir()

            self.assertEqual(cache.cached_keys(), {"a", "b"})

    def test_total_bytes_is_tracked_incrementally_on_store_and_remove(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            (root / "existing.mp3").write_bytes(b"x" * MIN_AUDIO_BYTES)
            self.assertEqual(cache.total_bytes(), MIN_AUDIO_BYTES)

            tmp = cache.temp_path_for_key("new")
            tmp.write_bytes(b"y" * (MIN_AUDIO_BYTES * 2))
            cache.store_from_temp("new", tmp)
            original_scandir = os.scandir

            def fail_scandir(*_args, **_kwargs):
                raise AssertionError("total_bytes must not rescan")

            try:
                os.scandir = fail_scandir
                self.assertEqual(cache.total_bytes(), MIN_AUDIO_BYTES * 3)
                cache.remove("existing")
                self.assertEqual(cache.total_bytes(), MIN_AUDIO_BYTES * 2)
            finally:
                os.scandir = original_scandir

    def test_cleanup_if_needed_skips_scan_under_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            (root / "a.mp3").write_bytes(b"x" * MIN_AUDIO_BYTES)
            cache.total_bytes()
            calls = []
            cache.cleanup = lambda max_bytes: calls.append(max_bytes)

            cache.cleanup_if_needed(MIN_AUDIO_BYTES * 2)
            self.assertEqual(calls, [])

            cache.cleanup_if_needed(MIN_AUDIO_BYTES - 1, target_bytes=10)
            self.assertEqual(calls, [10])

    def test_cleanup_updates_tracked_total(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            cache = AudioCache(root)
            for name in ("a", "b", "c"):
                (root / f"{name}.mp3").write_bytes(b"x" * MIN_AUDIO_BYTES)

            cache.cleanup(MIN_AUDIO_BYTES)

            self.assertEqual(cache.total_bytes(), MIN_AUDIO_BYTES)
            self.assertEqual(len(cache.cached_keys()), 1)

    def test_generation_lock_is_reused_per_key(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            key = speech_cache_key("hello", "en-GB-RyanNeural", 1.5)
            other_key = speech_cache_key("hello again", "en-GB-RyanNeural", 1.5)

            self.assertIs(cache.generation_lock(key), cache.generation_lock(key))
            self.assertIsNot(cache.generation_lock(key), cache.generation_lock(other_key))


class ModuleLoaderTests(unittest.TestCase):
    def test_load_addon_module_does_not_clobber_existing_real_parent_package(self):
        original_package = sys.modules.get("anki_tts_addon")
        original_module = sys.modules.get("anki_tts_addon.audio_cache")
        real_package = types.ModuleType("anki_tts_addon")
        real_package.__file__ = "/real/anki_tts_addon/__init__.py"

        try:
            sys.modules["anki_tts_addon"] = real_package

            module = load_addon_module("audio_cache")

            self.assertIs(sys.modules["anki_tts_addon"], real_package)
            self.assertIs(sys.modules["anki_tts_addon.audio_cache"], module)
        finally:
            if original_package is None:
                sys.modules.pop("anki_tts_addon", None)
            else:
                sys.modules["anki_tts_addon"] = original_package

            if original_module is None:
                sys.modules.pop("anki_tts_addon.audio_cache", None)
            else:
                sys.modules["anki_tts_addon.audio_cache"] = original_module
