import sys
import tempfile
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module


_AUDIO_CACHE_MODULE = load_addon_module("audio_cache")
_TTS_ENGINE_MODULE = load_addon_module("tts_engine")

AudioCache = _AUDIO_CACHE_MODULE.AudioCache
speech_cache_key = _AUDIO_CACHE_MODULE.speech_cache_key
TTSEngine = _TTS_ENGINE_MODULE.TTSEngine

FAKE_EDGE_BYTES = b"edge-mp3" * 200
CACHED_BYTES = b"cached-mp3" * 120


class FakeEdgeTTS:
    saved_texts = []

    class Communicate:
        def __init__(self, text, voice, rate):
            self.text = text
            self.voice = voice
            self.rate = rate

        async def save(self, path):
            FakeEdgeTTS.saved_texts.append(self.text)
            Path(path).write_bytes(FAKE_EDGE_BYTES)


class TinyEdgeTTS:
    class Communicate:
        def __init__(self, text, voice, rate):
            pass

        async def save(self, path):
            Path(path).write_bytes(b"too-small")


class CleanupRecordingCache(AudioCache):
    def __init__(self, root):
        super().__init__(root)
        self.cleanup_calls = []

    def cleanup(self, max_bytes):
        self.cleanup_calls.append(max_bytes)
        return super().cleanup(max_bytes)


class BlockingStoreCache(AudioCache):
    def __init__(self, root):
        super().__init__(root)
        self.store_entered = threading.Event()
        self.release_store = threading.Event()

    def store_from_temp(self, key, tmp_path):
        self.store_entered.set()
        self.release_store.wait(timeout=2)
        return super().store_from_temp(key, tmp_path)


class TTSEngineCacheTests(unittest.TestCase):
    def setUp(self):
        FakeEdgeTTS.saved_texts = []

    def test_cache_hit_plays_existing_file_without_edge_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            cache.path_for_key(key).write_bytes(CACHED_BYTES)
            played_bytes = []

            def fail_edge_load():
                raise AssertionError("Edge TTS should not be loaded on cache hit")

            def capture_play(path, generation_id=None):
                played_bytes.append(Path(path).read_bytes())

            engine._get_edge_tts = fail_edge_load
            engine._play_file = capture_play

            engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(played_bytes, [CACHED_BYTES])

    def test_disappearing_cache_hit_falls_through_to_edge_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            cache.path_for_key(key).write_bytes(CACHED_BYTES)
            original_play_cached_file = engine._play_cached_file
            play_cached_calls = []
            played_bytes = []

            def delete_before_first_cached_copy(path, generation_id=None):
                play_cached_calls.append(path)
                if len(play_cached_calls) == 1:
                    path.unlink()
                original_play_cached_file(path, generation_id)

            def capture_play(path, generation_id=None):
                played_bytes.append(Path(path).read_bytes())

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._play_cached_file = delete_before_first_cached_copy
            engine._play_file = capture_play

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING") as logs:
                engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(len(play_cached_calls), 2)
            self.assertEqual(cache.path_for_key(key).read_bytes(), FAKE_EDGE_BYTES)
            self.assertEqual(played_bytes, [FAKE_EDGE_BYTES])
            self.assertEqual(FakeEdgeTTS.saved_texts, ["hello"])
            self.assertIn("Cached TTS playback failed", logs.output[0])

    def test_cache_miss_generates_edge_audio_into_cache_then_plays_it(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            played_bytes = []

            def capture_play(path, generation_id=None):
                played_bytes.append(Path(path).read_bytes())

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._play_file = capture_play

            engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(cache.path_for_key(key).read_bytes(), FAKE_EDGE_BYTES)
            self.assertEqual(played_bytes, [FAKE_EDGE_BYTES])
            self.assertEqual(FakeEdgeTTS.saved_texts, ["hello"])

    def test_cache_miss_cleans_cache_to_configured_limit_after_playing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CleanupRecordingCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache, cache_max_bytes=4096)
            played_bytes = []

            def capture_play(path, generation_id=None):
                played_bytes.append(Path(path).read_bytes())

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._play_file = capture_play

            engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(played_bytes, [FAKE_EDGE_BYTES])
            self.assertEqual(cache.cleanup_calls, [4096])

    def test_play_cached_file_removes_temp_playback_file_after_playing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cached = Path(tmpdir) / "cached.mp3"
            cached.write_bytes(CACHED_BYTES)
            engine = TTSEngine()
            played_paths = []

            def capture_play(path, generation_id=None):
                played_path = Path(path)
                self.assertTrue(played_path.exists())
                self.assertEqual(played_path.read_bytes(), CACHED_BYTES)
                played_paths.append(played_path)

            engine._play_file = capture_play

            engine._play_cached_file(cached)

            self.assertEqual(len(played_paths), 1)
            self.assertFalse(played_paths[0].exists())

    def test_cache_enabled_edge_store_failure_falls_back_to_system_without_piper(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            system_calls = []

            def fail_piper():
                raise AssertionError("Piper fallback should not run")

            def capture_system(text, speed, generation_id=None):
                system_calls.append((text, speed, generation_id))

            engine._get_edge_tts = lambda: TinyEdgeTTS
            engine._get_piper_voice = fail_piper
            engine._speak_system = capture_system

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING") as logs:
                engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(len(system_calls), 1)
            self.assertEqual(system_calls[0][0:2], ("hello", 1.5))
            self.assertIn("Edge TTS failed", logs.output[0])

    def test_generate_edge_cached_cleanup_oserror_preserves_original_error(self):
        class OSErrorTempPath:
            def unlink(self):
                raise OSError("cleanup failed")

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            original = RuntimeError("generation failed")

            cache.temp_path_for_key = lambda key: OSErrorTempPath()

            def fail_save(text, speed, edge_tts, path):
                raise original

            engine._save_edge_audio = fail_save

            with self.assertRaises(RuntimeError) as raised:
                engine._generate_edge_cached("hello", 1.5, FakeEdgeTTS)

            self.assertIs(raised.exception, original)

    def test_prefetch_generates_but_does_not_play(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)

            def fail_play(path):
                raise AssertionError("prefetch should not play audio")

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._play_file = fail_play

            result = engine.prefetch("hello", {"speed": 1.5})

            self.assertTrue(result)
            self.assertTrue(cache.has(key))
            self.assertEqual(FakeEdgeTTS.saved_texts, ["hello"])

    def test_prefetch_returns_true_on_cache_hit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            cache.path_for_key(key).write_bytes(CACHED_BYTES)

            def fail_edge_load():
                raise AssertionError("Edge TTS should not be loaded on cache hit")

            engine._get_edge_tts = fail_edge_load

            result = engine.prefetch("hello", {"speed": 1.5})

            self.assertTrue(result)

    def test_prefetch_returns_false_when_edge_generation_fails(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            engine._get_edge_tts = lambda: TinyEdgeTTS

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                result = engine.prefetch("hello", {"speed": 1.5})

            self.assertFalse(result)

    def test_prefetch_cleans_cache_to_configured_limit_after_generation(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CleanupRecordingCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache, cache_max_bytes=4096)

            engine._get_edge_tts = lambda: FakeEdgeTTS

            engine.prefetch("hello", {"speed": 1.5})

            self.assertEqual(FakeEdgeTTS.saved_texts, ["hello"])
            self.assertEqual(cache.cleanup_calls, [4096])

    def test_stop_cancels_inflight_prefetch_before_cache_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            entered = threading.Event()
            release = threading.Event()
            results = []

            def blocking_save(text, speed, edge_tts, path):
                entered.set()
                release.wait(timeout=2)
                Path(path).write_bytes(FAKE_EDGE_BYTES)

            def run_prefetch():
                results.append(engine.prefetch("hello", {"speed": 1.5}))

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._save_edge_audio = blocking_save
            prefetch_thread = threading.Thread(target=run_prefetch)
            prefetch_thread.start()
            self.assertTrue(entered.wait(timeout=1))
            engine.stop()
            release.set()
            prefetch_thread.join(timeout=1)

            self.assertFalse(prefetch_thread.is_alive())
            self.assertFalse(cache.path_for_key(key).exists())
            self.assertEqual(results, [None])

    def test_prefetch_does_not_cancel_active_live_speech(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            live_entered = threading.Event()
            release_live = threading.Event()
            played = []

            def live_save(text, speed, edge_tts, path):
                live_entered.set()
                release_live.wait(timeout=2)
                Path(path).write_bytes(FAKE_EDGE_BYTES)

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._save_edge_audio = live_save
            engine._play_file = (
                lambda path, generation_id=None: played.append(
                    Path(path).read_bytes()
                )
            )

            engine.speak("live", {"speed": 1.5, "fallback_to_system": True})
            self.assertTrue(live_entered.wait(timeout=1))

            engine._save_edge_audio = (
                lambda text, speed, edge_tts, path:
                Path(path).write_bytes(FAKE_EDGE_BYTES)
            )
            engine.prefetch("prefetch", {"speed": 1.5})
            release_live.set()

            self.assertTrue(engine.wait_for_speech(timeout=1))
            self.assertEqual(played, [FAKE_EDGE_BYTES])

    def test_edge_tts_lazy_load_is_shared_across_threads(self):
        engine = TTSEngine()
        calls = []
        results = []
        errors = []
        first_call_entered = threading.Event()
        second_call_entered = threading.Event()
        release_import = threading.Event()
        original_import = _TTS_ENGINE_MODULE._import_edge_tts

        def slow_import():
            calls.append(threading.get_ident())
            if len(calls) == 1:
                first_call_entered.set()
            else:
                second_call_entered.set()
            release_import.wait(timeout=2)
            return FakeEdgeTTS

        def load_edge():
            try:
                results.append(engine._get_edge_tts())
            except Exception as e:
                errors.append(e)

        try:
            _TTS_ENGINE_MODULE._import_edge_tts = slow_import
            first_thread = threading.Thread(target=load_edge)
            second_thread = threading.Thread(target=load_edge)

            first_thread.start()
            self.assertTrue(first_call_entered.wait(timeout=1))
            second_thread.start()
            self.assertFalse(second_call_entered.wait(timeout=0.1))
            release_import.set()
            first_thread.join(timeout=1)
            second_thread.join(timeout=1)

            self.assertFalse(first_thread.is_alive())
            self.assertFalse(second_thread.is_alive())
            if errors:
                raise errors[0]
            self.assertEqual(results, [FakeEdgeTTS, FakeEdgeTTS])
            self.assertEqual(len(calls), 1)
        finally:
            release_import.set()
            _TTS_ENGINE_MODULE._import_edge_tts = original_import

    def test_wait_for_speech_reports_active_speech_timeout(self):
        engine = TTSEngine()
        entered = threading.Event()
        release = threading.Event()

        def blocking_speak(text, config, generation_id=None):
            entered.set()
            release.wait(timeout=2)

        engine._speak = blocking_speak
        engine.speak("hello", {"speed": 1.5})
        self.assertTrue(entered.wait(timeout=1))

        try:
            self.assertFalse(engine.wait_for_speech(timeout=0.05))
            release.set()
            self.assertTrue(engine.wait_for_speech(timeout=1))
        finally:
            release.set()

    def test_stop_cancels_inflight_edge_generation_before_cache_store_or_play(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            entered = threading.Event()
            release = threading.Event()
            played = []
            piper_calls = []

            def blocking_save(text, speed, edge_tts, path):
                entered.set()
                release.wait(timeout=2)
                Path(path).write_bytes(FAKE_EDGE_BYTES)

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._get_piper_voice = lambda: object()
            engine._save_edge_audio = blocking_save
            engine._play_file = (
                lambda path, generation_id=None: played.append(path)
            )
            engine._speak_piper = (
                lambda text, speed, voice, generation_id=None:
                piper_calls.append((text, speed, voice))
            )

            engine.speak("hello", {"speed": 1.5, "fallback_to_system": True})
            self.assertTrue(entered.wait(timeout=1))
            engine.stop()
            release.set()

            self.assertTrue(engine.wait_for_speech(timeout=1))
            self.assertFalse(cache.path_for_key(key).exists())
            self.assertEqual(played, [])
            self.assertEqual(piper_calls, [])

    def test_stop_after_edge_generation_before_publish_skips_cache_store(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)

            def save_then_stop(text, speed, edge_tts, path):
                Path(path).write_bytes(FAKE_EDGE_BYTES)
                engine.stop()

            engine._save_edge_audio = save_then_stop

            with self.assertRaises(_TTS_ENGINE_MODULE._SpeechCancelled):
                engine._generate_edge_cached(
                    "hello",
                    1.5,
                    FakeEdgeTTS,
                    engine._begin_generation(),
                )

            self.assertFalse(cache.path_for_key(key).exists())

    def test_stop_during_cache_publish_removes_published_file_before_returning(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = BlockingStoreCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)

            engine._save_edge_audio = (
                lambda text, speed, edge_tts, path:
                Path(path).write_bytes(FAKE_EDGE_BYTES)
            )

            generation_id = engine._begin_generation()

            def generate():
                with self.assertRaises(_TTS_ENGINE_MODULE._SpeechCancelled):
                    engine._generate_edge_cached(
                        "hello", 1.5, FakeEdgeTTS, generation_id
                    )

            generate_thread = threading.Thread(target=generate)
            generate_thread.start()
            self.assertTrue(cache.store_entered.wait(timeout=1))

            stop_done = threading.Event()
            stop_thread = threading.Thread(
                target=lambda: (engine.stop(), stop_done.set())
            )
            stop_thread.start()
            self.assertFalse(stop_done.wait(timeout=0.05))
            cache.release_store.set()
            stop_thread.join(timeout=1)
            generate_thread.join(timeout=1)

            self.assertTrue(stop_done.is_set())
            self.assertFalse(generate_thread.is_alive())
            self.assertFalse(cache.path_for_key(key).exists())

    def test_stop_during_playback_registration_terminates_process(self):
        entered_popen = threading.Event()
        release_popen = threading.Event()
        stop_done = threading.Event()
        engine = TTSEngine()
        generation_id = engine._begin_generation()
        original_popen = _TTS_ENGINE_MODULE.subprocess.Popen
        processes = []

        class FakeProcess:
            def __init__(self, cmd):
                self.cmd = cmd
                self.terminated = False
                self.killed = False
                self._done = threading.Event()
                processes.append(self)
                entered_popen.set()
                release_popen.wait(timeout=2)

            def wait(self, timeout=None):
                self._done.wait(timeout=timeout)

            def poll(self):
                return 0 if self._done.is_set() else None

            def terminate(self):
                self.terminated = True
                self._done.set()

            def kill(self):
                self.killed = True
                self._done.set()

        def run_playback():
            engine._run_process(["play", "file"], generation_id)

        try:
            _TTS_ENGINE_MODULE.subprocess.Popen = FakeProcess
            playback_thread = threading.Thread(target=run_playback)
            playback_thread.start()
            self.assertTrue(entered_popen.wait(timeout=1))

            stop_thread = threading.Thread(
                target=lambda: (engine.stop(), stop_done.set())
            )
            stop_thread.start()
            self.assertFalse(stop_done.wait(timeout=0.05))

            release_popen.set()
            stop_thread.join(timeout=1)
            playback_thread.join(timeout=1)

            self.assertTrue(stop_done.is_set())
            self.assertFalse(playback_thread.is_alive())
            self.assertEqual(len(processes), 1)
            self.assertTrue(processes[0].terminated)
            self.assertFalse(processes[0].killed)
        finally:
            release_popen.set()
            _TTS_ENGINE_MODULE.subprocess.Popen = original_popen

    def test_canceled_edge_failure_does_not_set_failure_latch_or_fallback(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            entered = threading.Event()
            release = threading.Event()
            piper_calls = []

            def fail_after_stop(text, speed, edge_tts, path):
                entered.set()
                release.wait(timeout=2)
                raise RuntimeError("network failed after stop")

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._get_piper_voice = lambda: object()
            engine._save_edge_audio = fail_after_stop
            engine._speak_piper = (
                lambda text, speed, voice, generation_id=None:
                piper_calls.append((text, speed, voice))
            )

            engine.speak("hello", {"speed": 1.5, "fallback_to_system": True})
            self.assertTrue(entered.wait(timeout=1))
            engine.stop()
            release.set()

            self.assertTrue(engine.wait_for_speech(timeout=1))
            self.assertFalse(engine._edge_tts_failed)
            self.assertEqual(piper_calls, [])


if __name__ == "__main__":
    unittest.main()
