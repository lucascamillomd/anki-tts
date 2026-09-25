import sys
import asyncio
import tempfile
import threading
import time
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


class FailingEdgeTTS:
    class Communicate:
        def __init__(self, text, voice, rate):
            pass

        async def save(self, path):
            raise RuntimeError("cannot connect to Edge service")


class HangingEdgeTTS:
    class Communicate:
        def __init__(self, text, voice, rate):
            pass

        async def save(self, path):
            await asyncio.Event().wait()


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

    def test_cache_miss_cleans_cache_only_when_over_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CleanupRecordingCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache, cache_max_bytes=4096)
            played_bytes = []

            def capture_play(path, generation_id=None):
                played_bytes.append(Path(path).read_bytes())

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._play_file = capture_play

            engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})
            # One clip is under the limit: no directory scan on the live path.
            self.assertEqual(cache.cleanup_calls, [])

            engine._speak("again", {"speed": 1.5, "fallback_to_system": True})
            engine._speak("third", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(len(played_bytes), 3)
            self.assertEqual(
                cache.cleanup_calls,
                [int(4096 * engine.CLEANUP_TARGET_RATIO)],
            )

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

    def test_too_small_edge_audio_falls_back_to_system_without_disabling_edge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            system_calls = []

            def capture_system(text, speed, generation_id=None):
                system_calls.append((text, speed, generation_id))

            engine._get_edge_tts = lambda: TinyEdgeTTS
            engine._speak_system = capture_system

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(len(system_calls), 1)
            self.assertEqual(system_calls[0][0:2], ("hello", 1.5))
            # A single too-small clip must NOT disable Edge for the session.
            self.assertFalse(engine.edge_status()["open"])

    def test_single_edge_failure_falls_back_without_disabling_edge(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            system_calls = []

            def capture_system(text, speed, generation_id=None):
                system_calls.append((text, speed, generation_id))

            engine._get_edge_tts = lambda: FailingEdgeTTS
            engine._speak_system = capture_system

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING") as logs:
                engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(len(system_calls), 1)
            self.assertFalse(engine.edge_status()["open"])
            self.assertIn("Edge TTS failed", logs.output[0])

    def test_repeated_edge_failures_trip_breaker_then_recover_after_cooldown(self):
        class Clock:
            value = 1000.0

            def __call__(self):
                return self.value

        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            clock = Clock()
            engine._breaker = _TTS_ENGINE_MODULE.EdgeCircuitBreaker(clock=clock)
            loads = []
            played = []

            def load_failing():
                loads.append("failing")
                return FailingEdgeTTS

            engine._get_edge_tts = load_failing
            engine._speak_system = lambda text, speed, generation_id=None: None
            engine._play_file = (
                lambda path, generation_id=None: played.append(path)
            )
            config = {"speed": 1.5, "fallback_to_system": True}

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                for text in ("a", "b", "c"):
                    engine._speak(text, config)

            self.assertTrue(engine.edge_status()["open"])
            self.assertGreater(engine.prefetch_pause_seconds(), 0)
            engine._speak("d", config)
            self.assertEqual(len(loads), 3)

            clock.value += engine._breaker.LIVE_COOLDOWN_SECONDS
            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._speak("e", config)

            self.assertEqual(len(played), 1)
            self.assertFalse(engine.edge_status()["open"])
            self.assertEqual(engine.prefetch_pause_seconds(), 0)

    def test_prefetch_returns_none_while_breaker_is_open(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            engine._get_edge_tts = lambda: FailingEdgeTTS

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                for text in ("a", "b", "c"):
                    with self.assertRaises(RuntimeError):
                        engine.prefetch(text, {"speed": 1.5})

            engine._get_edge_tts = lambda: FakeEdgeTTS
            self.assertIsNone(engine.prefetch("d", {"speed": 1.5}))
            self.assertEqual(FakeEdgeTTS.saved_texts, [])

    def test_playback_error_after_generation_does_not_speak_twice(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            system_calls = []

            def failing_play(path, generation_id=None):
                raise OSError("afplay missing")

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._play_file = failing_play
            engine._speak_system = (
                lambda text, speed, generation_id=None:
                system_calls.append(text)
            )

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                engine._speak("hello", {"speed": 1.5, "fallback_to_system": True})

            self.assertEqual(system_calls, [])
            self.assertFalse(engine.edge_status()["open"])

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

    def test_prefetch_timeout_does_not_stop_later_cards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            engine.EDGE_REQUEST_TIMEOUT_SECONDS = 0.01
            engine._get_edge_tts = lambda: HangingEdgeTTS

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                self.assertFalse(engine.prefetch("stuck", {"speed": 1.5}))

            self.assertEqual(engine.last_prefetch_error(), "request timed out")
            self.assertFalse(engine.edge_status()["open"])
            engine._get_edge_tts = lambda: FakeEdgeTTS
            self.assertTrue(engine.prefetch("next", {"speed": 1.5}))
            self.assertIsNone(engine.last_prefetch_error())

    def test_prefetch_cleanup_runs_only_when_cache_exceeds_limit(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = CleanupRecordingCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache, cache_max_bytes=4096)
            engine._get_edge_tts = lambda: FakeEdgeTTS

            engine.prefetch("a", {"speed": 1.5})
            engine.prefetch("b", {"speed": 1.5})
            # Still under the limit: no directory-scanning cleanup.
            self.assertEqual(cache.cleanup_calls, [])

            engine.prefetch("c", {"speed": 1.5})

            self.assertEqual(
                cache.cleanup_calls,
                [int(4096 * engine.CLEANUP_TARGET_RATIO)],
            )
            self.assertEqual(FakeEdgeTTS.saved_texts, ["a", "b", "c"])

    def test_live_stop_does_not_cancel_inflight_prefetch(self):
        # A card advance during review calls speak()->stop(), which bumps the
        # live generation. Background prefetch must survive that and keep its
        # generated file so warm-up during review is not wasted.
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
            self.assertTrue(cache.path_for_key(key).exists())
            self.assertEqual(cache.path_for_key(key).read_bytes(), FAKE_EDGE_BYTES)
            self.assertEqual(results, [True])

    def test_prefetch_failure_does_not_disable_later_cards(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            engine._get_edge_tts = lambda: FailingEdgeTTS

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                with self.assertRaises(RuntimeError):
                    engine.prefetch("first", {"speed": 1.5})

            self.assertFalse(engine.edge_status()["open"])
            engine._get_edge_tts = lambda: FakeEdgeTTS
            self.assertTrue(engine.prefetch("next", {"speed": 1.5}))

    def test_prefetch_too_small_audio_does_not_latch_edge_failed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            engine._get_edge_tts = lambda: TinyEdgeTTS

            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                result = engine.prefetch("hello", {"speed": 1.5})

            self.assertFalse(result)
            self.assertTrue(
                engine.last_prefetch_error().startswith("audio too small")
            )
            self.assertFalse(engine.edge_status()["open"])

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

    def test_stop_during_edge_generation_skips_playback_but_keeps_clip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)
            entered = threading.Event()
            release = threading.Event()
            played = []
            system_calls = []

            def blocking_save(text, speed, edge_tts, path):
                entered.set()
                release.wait(timeout=2)
                Path(path).write_bytes(FAKE_EDGE_BYTES)

            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._save_edge_audio = blocking_save
            engine._play_file = (
                lambda path, generation_id=None: played.append(path)
            )
            engine._speak_system = (
                lambda text, speed, generation_id=None:
                system_calls.append(text)
            )

            engine.speak("hello", {"speed": 1.5, "fallback_to_system": True})
            self.assertTrue(entered.wait(timeout=1))
            engine.stop()
            release.set()

            self.assertTrue(engine.wait_for_speech(timeout=1))
            # The downloaded clip is kept for next time, but not played.
            self.assertEqual(cache.path_for_key(key).read_bytes(), FAKE_EDGE_BYTES)
            self.assertEqual(played, [])
            self.assertEqual(system_calls, [])

    def test_stop_after_edge_generation_still_publishes_clip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)

            def save_then_stop(text, speed, edge_tts, path):
                Path(path).write_bytes(FAKE_EDGE_BYTES)
                engine.stop()

            engine._save_edge_audio = save_then_stop

            path = engine._generate_edge_cached(
                "hello",
                1.5,
                FakeEdgeTTS,
                engine._begin_generation(),
            )

            self.assertEqual(path, cache.path_for_key(key))
            self.assertEqual(path.read_bytes(), FAKE_EDGE_BYTES)

    def test_stop_during_cache_publish_does_not_block_or_remove_clip(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = BlockingStoreCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hello", engine.EDGE_VOICE, 1.5)

            engine._save_edge_audio = (
                lambda text, speed, edge_tts, path:
                Path(path).write_bytes(FAKE_EDGE_BYTES)
            )

            generation_id = engine._begin_generation()
            generate_thread = threading.Thread(
                target=lambda: engine._generate_edge_cached(
                    "hello", 1.5, FakeEdgeTTS, generation_id
                )
            )
            generate_thread.start()
            self.assertTrue(cache.store_entered.wait(timeout=1))

            started = time.monotonic()
            engine.stop()
            self.assertLess(time.monotonic() - started, 0.5)
            cache.release_store.set()
            generate_thread.join(timeout=1)

            self.assertFalse(generate_thread.is_alive())
            self.assertTrue(cache.path_for_key(key).exists())

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
            engine._speak_system = (
                lambda text, speed, generation_id=None:
                piper_calls.append(text)
            )
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
            self.assertFalse(engine.edge_status()["open"])
            self.assertEqual(piper_calls, [])


    def test_edge_requests_share_one_persistent_loop_thread(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            self.addCleanup(engine.shutdown)
            engine._get_edge_tts = lambda: FakeEdgeTTS

            self.assertTrue(engine.prefetch("a", {"speed": 1.5}))
            first_thread = engine._loop_thread
            self.assertTrue(engine.prefetch("b", {"speed": 1.5}))

            self.assertIs(engine._loop_thread, first_thread)
            self.assertTrue(first_thread.is_alive())

    def test_cancel_prefetch_aborts_inflight_edge_request(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            self.addCleanup(engine.shutdown)
            engine._get_edge_tts = lambda: HangingEdgeTTS
            results = []
            thread = threading.Thread(
                target=lambda: results.append(
                    engine.prefetch("stuck", {"speed": 1.5})
                )
            )
            thread.start()
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not engine._prefetch_futures:
                time.sleep(0.01)

            engine.cancel_prefetch()
            thread.join(timeout=1)

            self.assertFalse(thread.is_alive())
            self.assertEqual(results, [None])
            self.assertFalse(engine.edge_status()["open"])
            self.assertEqual(engine.edge_status()["consecutive_failures"], 0)

    def test_shutdown_cancels_requests_and_stops_loop_quickly(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            engine._get_edge_tts = lambda: HangingEdgeTTS
            engine._speak_system = lambda text, speed, generation_id=None: None

            engine.speak("hanging", {"speed": 1.5, "fallback_to_system": True})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not engine._live_futures:
                time.sleep(0.01)
            loop_thread = engine._loop_thread

            started = time.monotonic()
            self.assertTrue(engine.shutdown(timeout=1))

            self.assertLess(time.monotonic() - started, 1)
            self.assertFalse(loop_thread.is_alive())
            self.assertTrue(engine.wait_for_speech(timeout=1))
            engine.speak("ignored", {"speed": 1.5})
            self.assertTrue(engine.wait_for_speech(timeout=0.1))

    def test_new_card_cancels_live_request_for_other_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            self.addCleanup(engine.shutdown)
            played = []
            engine._play_file = (
                lambda path, generation_id=None: played.append(
                    Path(path).read_bytes()
                )
            )

            class FirstHangsEdgeTTS:
                class Communicate:
                    def __init__(self, text, voice, rate):
                        self.text = text

                    async def save(self, path):
                        if self.text == "first":
                            await asyncio.Event().wait()
                        Path(path).write_bytes(FAKE_EDGE_BYTES)

            engine._get_edge_tts = lambda: FirstHangsEdgeTTS
            engine.speak("first", {"speed": 1.5})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not engine._live_futures:
                time.sleep(0.01)

            engine.speak("second", {"speed": 1.5})

            self.assertTrue(engine.wait_for_speech(timeout=2))
            self.assertEqual(played, [FAKE_EDGE_BYTES])
            self.assertFalse(engine.edge_status()["open"])

    def test_speech_uses_single_worker_with_latest_request_mailbox(self):
        engine = TTSEngine()
        entered = threading.Event()
        release = threading.Event()
        spoken = []
        threads = set()

        def blocking_speak(text, config, generation_id=None):
            threads.add(threading.current_thread())
            spoken.append(text)
            if text == "first":
                entered.set()
                release.wait(timeout=2)

        engine._speak = blocking_speak
        engine.speak("first", {"speed": 1.5})
        self.assertTrue(entered.wait(timeout=1))
        engine.speak("second", {"speed": 1.5})
        engine.speak("third", {"speed": 1.5})
        release.set()

        self.assertTrue(engine.wait_for_speech(timeout=1))
        self.assertEqual(spoken, ["first", "third"])
        self.assertEqual(len(threads), 1)

    def test_live_cache_hits_and_misses_are_counted(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            cache = AudioCache(Path(tmpdir))
            engine = TTSEngine(audio_cache=cache)
            key = speech_cache_key("hit", engine.EDGE_VOICE, 1.5)
            cache.path_for_key(key).write_bytes(CACHED_BYTES)
            engine._get_edge_tts = lambda: FakeEdgeTTS
            engine._play_file = lambda path, generation_id=None: None

            engine._speak("hit", {"speed": 1.5})
            engine._speak("miss", {"speed": 1.5})

            self.assertEqual(engine.live_stats(), {"hits": 1, "misses": 1})

    def test_run_process_kills_player_on_timeout_and_clears_process(self):
        engine = TTSEngine()
        engine.PROCESS_TIMEOUT_SECONDS = 0.01
        original_popen = _TTS_ENGINE_MODULE.subprocess.Popen
        processes = []

        class HangingProcess:
            def __init__(self, cmd):
                self.killed = False
                processes.append(self)

            def wait(self, timeout=None):
                if not self.killed:
                    raise _TTS_ENGINE_MODULE.subprocess.TimeoutExpired(
                        "afplay", timeout
                    )
                return 0

            def poll(self):
                return 0 if self.killed else None

            def kill(self):
                self.killed = True

        try:
            _TTS_ENGINE_MODULE.subprocess.Popen = HangingProcess
            with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
                engine._run_process(["afplay", "x"])
        finally:
            _TTS_ENGINE_MODULE.subprocess.Popen = original_popen

        self.assertTrue(processes[0].killed)
        self.assertIsNone(engine._process)

    def test_speech_worker_survives_unexpected_error(self):
        engine = TTSEngine()
        spoken = []

        def flaky_speak(text, config, generation_id=None):
            if text == "boom":
                raise RuntimeError("unexpected")
            spoken.append(text)

        engine._speak = flaky_speak
        with self.assertLogs(_TTS_ENGINE_MODULE.log, level="WARNING"):
            engine.speak("boom", {"speed": 1.5})
            self.assertTrue(engine.wait_for_speech(timeout=1))
        engine.speak("ok", {"speed": 1.5})

        self.assertTrue(engine.wait_for_speech(timeout=1))
        self.assertEqual(spoken, ["ok"])

if __name__ == "__main__":
    unittest.main()
