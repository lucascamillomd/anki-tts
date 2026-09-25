import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


from fake_aqt import (  # noqa: E402
    _AQT_MODULES,
    _DEFAULT_CONFIG,
    _MISSING,
    _FakeAddonManager,
    _ImmediateFuture,
    _install_fake_aqt,
)


def _purge_addon_modules():
    for name in list(sys.modules):
        if name == "anki_tts_addon" or name.startswith("anki_tts_addon."):
            del sys.modules[name]


class _FakeEngine:
    def __init__(self, wait_result=True):
        self.stop_count = 0
        self.spoken = []
        self.wait_result = wait_result
        self.wait_timeouts = []
        self.set_audio_cache_calls = []

    def set_audio_cache(self, cache):
        self.set_audio_cache_calls.append(cache)

    def stop(self):
        self.stop_count += 1

    def speak(self, text, config):
        self.stop()
        self.spoken.append((text, config))

    def wait_for_speech(self, timeout=None):
        self.wait_timeouts.append(timeout)
        return self.wait_result


class _FakePrefetcher:
    def __init__(
        self,
        stop_result=True,
        start_result=True,
        enqueue_result=True,
        status_result=None,
    ):
        self.started = False
        self.stopped = False
        self.stop_result = stop_result
        self.start_result = start_result
        self.enqueue_result = enqueue_result
        self.status_result = status_result or {
            "running": False,
            "active": 0,
            "pending": 0,
        }
        self.stop_timeouts = []
        self.enqueued = []
        self.idle_callback = None
        self.result_callback = None

    def start(self):
        self.started = True
        return self.start_result

    def stop(self, timeout=None):
        self.stopped = True
        self.stop_timeouts.append(timeout)
        return self.stop_result

    def enqueue(self, text, config):
        self.enqueued.append((text, dict(config)))
        return self.enqueue_result

    def set_idle_callback(self, callback):
        self.idle_callback = callback

    def set_result_callback(self, callback):
        self.result_callback = callback

    def status(self):
        return dict(self.status_result)


class _FakeCheckBox:
    def __init__(self, checked):
        self.checked = checked

    def isChecked(self):
        return self.checked


class _FakeSpinBox:
    def __init__(self, value):
        self._value = value

    def value(self):
        return self._value


class _FakeDb:
    def __init__(self, collection):
        self.collection = collection
        self.list_queries = []
        self.usn = 0

    def list(self, sql, *args):
        self.list_queries.append((sql, args))
        return list(self.collection.changed_ids)

    def scalar(self, _sql, *_args):
        return self.usn


class _FakeCollection:
    path = "/fake/collection.anki2"

    def __init__(self, cards):
        self.cards = dict(cards)
        self.find_queries = []
        self.get_card_ids = []
        self.changed_ids = []
        self.db = _FakeDb(self)

    def find_cards(self, query):
        self.find_queries.append(query)
        return list(self.cards)

    def get_card(self, card_id):
        self.get_card_ids.append(card_id)
        return self.cards[card_id]


class _FakeDueCard:
    ord = 0

    def __init__(self, card_id, rendered_question, question_html="raw question"):
        self.id = card_id
        self.rendered_question = rendered_question
        self.question_html = question_html
        self.render_reload_values = []

    def render_output(self, reload=False):
        self.render_reload_values.append(reload)
        return types.SimpleNamespace(question_text=self.rendered_question)

    def question(self):
        return self.question_html


class _EmptyQuestionCard:
    id = 404
    ord = 0

    def render_output(self, reload=False):
        return types.SimpleNamespace(question_text="")

    def question(self):
        return ""


class _FakeCard:
    id = 123
    ord = 0

    def question(self):
        return (
            "Ruxolitinib is used to treat chronic myeloproliferative "
            "disorders that have {{c3::JAK2}} mutations, including "
            "{{c1::myelofibrosis}} and {{c2::polycythemia vera}}"
        )

    def answer(self):
        return (
            self.question()
            + '<hr id="answer">'
            + "Ruxolitinib treats myelofibrosis and polycythemia vera"
        )


class ReviewerHookTests(unittest.TestCase):
    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name, _MISSING) for name in _AQT_MODULES
        }
        _purge_addon_modules()
        self.hooks = _install_fake_aqt()
        self.addon = importlib.import_module("anki_tts_addon")
        # Never touch the real add-on user_files from tests.
        self._user_files = tempfile.TemporaryDirectory()
        self.addCleanup(self._user_files.cleanup)
        user_files = Path(self._user_files.name)
        self.addon._addon_user_files_dir = lambda: user_files

    def tearDown(self):
        _purge_addon_modules()
        import logging

        for handler in list(logging.getLogger("anki_tts_addon").handlers):
            if getattr(handler, "_anki_tts_file_handler", False):
                logging.getLogger("anki_tts_addon").removeHandler(handler)
                handler.close()
        for name in _AQT_MODULES:
            sys.modules.pop(name, None)
            saved = self._saved_modules[name]
            if saved is not _MISSING:
                sys.modules[name] = saved

    def set_config(self, **overrides):
        self.addon.mw.addonManager.config.update(overrides)

    def _run_next_timer(self):
        _delay_ms, callback = self.hooks.timers.pop(0)
        callback()

    def _run_all_timers(self):
        while self.hooks.timers:
            self._run_next_timer()

    def _delayed_timers(self):
        """QTimer.singleShot timers (delay > 0), not queued background tasks."""
        return [timer for timer in self.hooks.timers if timer[0] > 0]

    def test_profile_open_registers_rendered_html_cache_hook(self):
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        prefetcher = _FakePrefetcher()
        self.addon._prefetcher = prefetcher

        self.addon._on_profile_did_open()

        self.assertTrue(prefetcher.started)
        self.assertIn(self.addon.cache_review_html, self.hooks.card_will_show)
        self.assertIn(
            self.addon.on_reviewer_did_show_question,
            self.hooks.reviewer_did_show_question,
        )

    def test_profile_open_registers_audio_cache_menu_actions(self):
        self.addon._menu_added = False
        self.addon._hooks_registered = True
        self.addon._prefetcher = _FakePrefetcher()

        self.addon._on_profile_did_open()

        self.assertEqual(len(self.hooks.menus), 1)
        actions = self.hooks.menus[0].actions
        action_texts = [action.text for action in actions]
        self.assertEqual(
            action_texts,
            [
                "Toggle TTS",
                "Settings...",
                "Warm All Audio Cache",
                "Audio Cache Status",
                "Show Monitor",
                "Clear Audio Cache",
            ],
        )
        warm_action = actions[action_texts.index("Warm All Audio Cache")]
        status_action = actions[action_texts.index("Audio Cache Status")]
        monitor_action = actions[action_texts.index("Show Monitor")]
        clear_action = actions[action_texts.index("Clear Audio Cache")]
        self.assertEqual(
            monitor_action.triggered.callbacks,
            [self.addon.show_monitor],
        )
        self.assertEqual(
            warm_action.triggered.callbacks,
            [self.addon.warm_all_audio_cache],
        )
        self.assertEqual(
            status_action.triggered.callbacks,
            [self.addon.show_audio_cache_status],
        )
        self.assertEqual(
            clear_action.triggered.callbacks,
            [self.addon.confirm_clear_audio_cache],
        )

    def test_profile_open_registers_sync_hook_when_available(self):
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.addon._prefetcher = _FakePrefetcher()

        self.addon._on_profile_did_open()

        self.assertIn(self.addon.on_sync_did_finish, self.hooks.sync_did_finish)
        self.assertIn(self.addon.on_sync_will_start, self.hooks.sync_will_start)

        self.addon._on_profile_will_close()

        self.assertEqual(self.hooks.sync_did_finish, [])
        self.assertEqual(self.hooks.sync_will_start, [])

    def test_profile_open_cleans_stale_audio_temp_files_before_warming(self):
        events = []

        class RecordingCache:
            def cleanup_temp_files(self, min_age_seconds=0):
                events.append(("cleanup", min_age_seconds))
                return 2

        class RecordingCollection(_FakeCollection):
            def find_cards(self, query):
                events.append("warm")
                return super().find_cards(query)

        self.addon._menu_added = True
        self.addon._hooks_registered = True
        self.addon._audio_cache = RecordingCache()
        self.addon.mw.col = RecordingCollection({})
        self.addon._prefetcher = _FakePrefetcher()

        self.addon._on_profile_did_open()

        # The directory scan runs off the main thread, and only removes
        # leftovers old enough not to belong to a clip being generated now.
        self.assertEqual(events, [])
        self.assertEqual(
            self.hooks.background_calls[0]["kwargs"], {"uses_collection": False}
        )

        with self.assertLogs(self.addon.log, level="WARNING"):
            self._run_all_timers()

        self.assertEqual(
            events,
            [("cleanup", self.addon.TEMP_FILE_MIN_AGE_SECONDS), "warm"],
        )

    def test_profile_open_does_not_start_prefetcher_when_tts_disabled(self):
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.set_config(enabled=False)
        prefetcher = _FakePrefetcher()
        self.addon._prefetcher = prefetcher

        self.addon._on_profile_did_open()

        self.assertFalse(prefetcher.started)

    def test_profile_open_does_not_start_prefetcher_when_cache_disabled(self):
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.set_config(cache_enabled=False)
        prefetcher = _FakePrefetcher()
        self.addon._prefetcher = prefetcher

        self.addon._on_profile_did_open()

        self.assertFalse(prefetcher.started)

    def test_profile_open_does_not_start_prefetcher_when_prefetch_disabled(self):
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.set_config(prefetch_enabled=False)
        prefetcher = _FakePrefetcher()
        self.addon._prefetcher = prefetcher

        self.addon._on_profile_did_open()

        self.assertFalse(prefetcher.started)

    def test_profile_close_stops_prefetcher_and_engine(self):
        prefetcher = _FakePrefetcher()
        engine = _FakeEngine()
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine

        self.addon._on_profile_will_close()

        self.assertTrue(prefetcher.stopped)
        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.CLOSE_STOP_TIMEOUT_SECONDS],
        )
        self.assertIsNone(self.addon._prefetcher)
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.CLOSE_STOP_TIMEOUT_SECONDS],
        )
        self.assertIsNone(self.addon._engine)

    def test_profile_close_cancels_and_shuts_down_engine_with_short_waits(self):
        events = []

        class ShutdownEngine(_FakeEngine):
            def cancel_prefetch(self):
                events.append("cancel_prefetch")

            def shutdown(self, timeout=None):
                events.append(("shutdown", timeout))
                return True

        class RecordingPrefetcher(_FakePrefetcher):
            def stop(self, timeout=None):
                events.append(("prefetcher.stop", timeout))
                return super().stop(timeout)

        self.addon._engine = ShutdownEngine()
        self.addon._prefetcher = RecordingPrefetcher()

        self.addon._on_profile_will_close()

        timeout = self.addon.CLOSE_STOP_TIMEOUT_SECONDS
        self.assertLessEqual(timeout, 0.3)
        self.assertEqual(
            events,
            [
                "cancel_prefetch",
                ("prefetcher.stop", timeout),
                ("shutdown", timeout),
            ],
        )

    def test_profile_close_flushes_state_and_drops_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            state = self._new_cache_state(path)
            state.mark_failed("failed", "boom", "text")
            state.mark_pending("pending", "pending text")
            self.addon._audio_cache_state = state

            self.addon._on_profile_will_close()

            reloaded = self._new_cache_state(path)
            self.assertEqual(reloaded.summary(), {"pending": 0, "failed": 1})
            self.assertIsNone(self.addon._audio_cache_state)

    def test_profile_close_detaches_status_callback(self):
        tts_engine_module = importlib.import_module("anki_tts_addon.tts_engine")
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.addon._prefetcher = _FakePrefetcher()
        self.addon._on_profile_did_open()
        self.assertIsNotNone(tts_engine_module._status_callback)
        shown = []
        self.addon.mw.taskman.run_on_main = lambda fn: shown.append(fn)

        self.addon._on_profile_will_close()
        self.addon._show_status("late message from a worker")

        self.assertIsNone(tts_engine_module._status_callback)
        self.assertEqual(shown, [])

    def test_profile_close_detaches_worker_when_prefetcher_stop_times_out(self):
        prefetcher = _FakePrefetcher(stop_result=False)
        engine = _FakeEngine()
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine
        self.addon._audio_cache = object()
        self.addon._engine_cache_enabled = True
        prefetcher.idle_callback = self.addon._on_audio_prefetch_idle
        prefetcher.result_callback = self.addon._on_audio_prefetch_result

        self.addon._on_profile_will_close()

        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.CLOSE_STOP_TIMEOUT_SECONDS],
        )
        # The stuck worker can no longer call back into the add-on, and the
        # next profile gets a fresh one instead of the half-stopped worker.
        self.assertIsNone(prefetcher.idle_callback)
        self.assertIsNone(prefetcher.result_callback)
        self.assertIsNone(self.addon._prefetcher)
        self.assertEqual(engine.stop_count, 1)
        self.assertIsNone(self.addon._engine)
        self.assertIsNone(self.addon._audio_cache)
        self.assertIsNone(self.addon._engine_cache_enabled)

    def test_profile_close_does_not_wait_long_for_live_speech(self):
        prefetcher = _FakePrefetcher()
        engine = _FakeEngine(wait_result=False)
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine
        self.addon._audio_cache = object()
        self.addon._engine_cache_enabled = True

        self.addon._on_profile_will_close()

        self.assertIsNone(self.addon._prefetcher)
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.CLOSE_STOP_TIMEOUT_SECONDS],
        )
        self.assertIsNone(self.addon._engine)

    def test_profile_close_invalidates_pending_manual_warm_timer(self):
        old_collection = _FakeCollection({1: _FakeDueCard(1, "old")})
        new_collection = _FakeCollection({2: _FakeDueCard(2, "new")})
        self.addon.mw.col = old_collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()
        self.addon._on_profile_will_close()
        self.addon.mw.col = new_collection
        self._run_all_timers()

        self.assertEqual(old_collection.find_queries, [])
        self.assertEqual(new_collection.find_queries, [])
        self.assertEqual(prefetcher.enqueued, [])

    def test_audio_cache_status_never_scans_the_collection(self):
        collection = _FakeCollection({1: _FakeDueCard(1, "card")})
        self.addon.mw.col = collection

        self.addon.show_audio_cache_status()

        self.assertEqual(collection.find_queries, [])
        self.assertEqual(collection.get_card_ids, [])
        self.assertEqual(self.hooks.timers, [])
        self.assertEqual(
            self.hooks.tooltips,
            [
                "Audio cache: no full scan yet (use Warm All Audio Cache); "
                "worker idle"
            ],
        )

    def test_profile_close_invalidates_scheduled_auto_warm_timer(self):
        old_collection = _FakeCollection({1: _FakeDueCard(1, "old")})
        new_collection = _FakeCollection({2: _FakeDueCard(2, "new")})
        self.addon.mw.col = old_collection
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon._on_profile_did_open()
        self.addon._on_profile_will_close()
        self.addon.mw.col = new_collection
        self._run_all_timers()

        self.assertEqual(old_collection.find_queries, [])
        self.assertEqual(new_collection.find_queries, [])
        self.assertEqual(prefetcher.enqueued, [])

    def test_profile_reopen_schedules_auto_warm_after_stale_timer(self):
        old_collection = _FakeCollection({1: _FakeDueCard(1, "old")})
        new_collection = _FakeCollection({2: _FakeDueCard(2, "new")})
        self.addon.mw.col = old_collection
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon._on_profile_did_open()
        self.addon._on_profile_will_close()
        self.addon.mw.col = new_collection
        self.addon._on_profile_did_open()

        self.assertEqual(len(self._delayed_timers()), 2)

        self._run_next_timer()

        self.assertEqual(old_collection.find_queries, [])
        self.assertEqual(new_collection.find_queries, [])
        self.assertEqual(prefetcher.enqueued, [])

        self._run_all_timers()

        self.assertEqual(old_collection.find_queries, [])
        self.assertEqual(new_collection.find_queries, [""])
        self.assertEqual(prefetcher.enqueued, [("new", _DEFAULT_CONFIG)])

    def test_stop_prefetcher_preserves_worker_when_timed_stop_fails(self):
        prefetcher = _FakePrefetcher(stop_result=False)
        self.addon._prefetcher = prefetcher

        stopped = self.addon._stop_prefetcher(timeout=0.05)

        self.assertFalse(stopped)
        self.assertEqual(prefetcher.stop_timeouts, [0.05])
        self.assertIs(self.addon._prefetcher, prefetcher)

    def test_cache_disabled_constructs_engine_without_audio_cache(self):
        self.set_config(cache_enabled=False)

        engine = self.addon.engine()

        self.assertIsNone(engine._audio_cache)

    def test_cache_max_mb_is_passed_to_engine_as_bytes(self):
        self.set_config(cache_max_mb=12)

        engine = self.addon.engine()

        self.assertEqual(engine._cache_max_bytes, 12 * 1024 * 1024)

    def test_engine_rebuilds_when_cache_enabled_changes(self):
        cache_enabled_engine = self.addon.engine()
        self.assertIsNotNone(cache_enabled_engine._audio_cache)
        self.set_config(cache_enabled=False)

        cache_disabled_engine = self.addon.engine()

        self.assertIsNot(cache_disabled_engine, cache_enabled_engine)
        self.assertIsNone(cache_disabled_engine._audio_cache)
        self.set_config(cache_enabled=True)

        cache_reenabled_engine = self.addon.engine()

        self.assertIsNot(cache_reenabled_engine, cache_disabled_engine)
        self.assertIsNotNone(cache_reenabled_engine._audio_cache)

    def test_engine_reconfigures_in_place_when_prefetcher_stop_times_out(self):
        engine = _FakeEngine()
        prefetcher = _FakePrefetcher(stop_result=False)
        self.addon._engine = engine
        self.addon._engine_cache_enabled = True
        self.addon._prefetcher = prefetcher
        self.set_config(cache_enabled=False)

        result = self.addon.engine()

        self.assertIs(result, engine)
        self.assertIs(self.addon._engine, engine)
        # The cache-enabled flag must be reconciled to the new setting, not
        # left stale while the engine keeps caching the user disabled.
        self.assertFalse(self.addon._engine_cache_enabled)
        self.assertEqual(engine.set_audio_cache_calls, [None])
        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )
        self.assertEqual(engine.stop_count, 1)

    def test_engine_cancels_generation_before_stopping_prefetcher_on_cache_toggle(self):
        events = []

        class RecordingEngine(_FakeEngine):
            def stop(self):
                events.append("engine.stop")
                super().stop()

        class RecordingPrefetcher(_FakePrefetcher):
            def stop(self, timeout=None):
                events.append("prefetcher.stop")
                return super().stop(timeout)

        engine = RecordingEngine()
        prefetcher = RecordingPrefetcher()
        self.addon._engine = engine
        self.addon._engine_cache_enabled = True
        self.addon._prefetcher = prefetcher
        self.set_config(cache_enabled=False)

        result = self.addon.engine()

        self.assertIsNot(result, engine)
        self.assertEqual(events[:2], ["engine.stop", "prefetcher.stop"])
        self.assertEqual(engine.stop_count, 1)

    def test_engine_reconfigures_in_place_when_live_speech_stop_times_out(self):
        engine = _FakeEngine(wait_result=False)
        self.addon._engine = engine
        self.addon._engine_cache_enabled = True
        self.addon._prefetcher = None
        self.set_config(cache_enabled=False)

        result = self.addon.engine()

        self.assertIs(result, engine)
        self.assertIs(self.addon._engine, engine)
        self.assertFalse(self.addon._engine_cache_enabled)
        self.assertEqual(engine.set_audio_cache_calls, [None])
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )

    def test_warm_question_text_prefers_captured_reviewer_html(self):
        # When a card has already been shown this session, warm-up must key its
        # audio off the same reviewer HTML the live path uses, so the warmed
        # clip is actually found on playback instead of under a divergent key.
        card = _FakeDueCard(7, rendered_question="render output text")
        self.addon.cache_review_html(
            "captured reviewer text", card, self.addon.REVIEW_QUESTION_CONTEXT
        )

        text = self.addon._warm_question_text(card)

        self.assertEqual(text, "captured reviewer text")

    def test_warm_question_text_falls_back_to_render_output(self):
        card = _FakeDueCard(8, rendered_question="render output text")

        text = self.addon._warm_question_text(card)

        self.assertEqual(text, "render output text")

    def test_question_tts_uses_cached_rendered_question_html(self):
        card = _FakeCard()
        engine = _FakeEngine()
        self.addon._engine = engine
        self.addon._prefetcher = _FakePrefetcher()
        rendered_question = (
            "Ruxolitinib is used to treat chronic myeloproliferative "
            "disorders that have JAK2 mutations, including myelofibrosis "
            'and <span class="cloze">[...]</span>'
        )
        self.addon.cache_review_html(
            rendered_question, card, self.addon.REVIEW_QUESTION_CONTEXT
        )

        self.addon.on_reviewer_did_show_question(card)

        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.spoken,
            [
                (
                    "Ruxolitinib is used to treat chronic "
                    "myeloproliferative disorders that have JAK2 "
                    "mutations, including myelofibrosis and bla bla bla",
                    {
                        "enabled": True,
                        "speak_question": True,
                        "speak_answer": True,
                        "fallback_to_system": True,
                        "speed": 1.5,
                        "cache_enabled": True,
                        "prefetch_enabled": True,
                        "cache_max_mb": 2048,
                    },
                )
            ],
        )

    def test_question_hook_stops_previous_audio_when_question_speech_disabled(self):
        card = _FakeCard()
        engine = _FakeEngine()
        self.addon._engine = engine
        self.set_config(speak_question=False, speak_answer=True)

        self.addon.on_reviewer_did_show_question(card)

        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(engine.spoken, [])

    def test_question_hook_stops_previous_audio_for_empty_question_text(self):
        engine = _FakeEngine()
        self.addon._engine = engine

        self.addon.on_reviewer_did_show_question(_EmptyQuestionCard())

        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(engine.spoken, [])

    def test_question_tts_uses_shared_rendered_text_helper(self):
        card = _FakeCard()
        engine = _FakeEngine()
        self.addon._engine = engine
        self.addon._prefetcher = _FakePrefetcher()
        rendered_question = (
            'Ruxolitinib has JAK2, myelofibrosis, '
            '<span class="cloze">[...]</span>'
        )
        self.addon.cache_review_html(
            rendered_question, card, self.addon.REVIEW_QUESTION_CONTEXT
        )
        original_helper = self.addon.speakable_question_text
        helper_calls = []

        def spy_speakable_question_text(card_arg, rendered_html):
            helper_calls.append((card_arg, rendered_html))
            return original_helper(card_arg, rendered_html)

        self.addon.speakable_question_text = spy_speakable_question_text

        self.addon.on_reviewer_did_show_question(card)

        self.assertEqual(helper_calls, [(card, rendered_question)])
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(len(engine.spoken), 1)
        self.assertEqual(
            engine.spoken[0][0],
            "Ruxolitinib has JAK2, myelofibrosis, bla bla bla",
        )

    def test_answer_tts_uses_cached_rendered_answer_html_and_strips_question(self):
        card = _FakeCard()
        engine = _FakeEngine()
        self.addon._engine = engine
        self.addon._prefetcher = _FakePrefetcher()
        rendered_answer = (
            "Question side mentions JAK2 and should be skipped"
            '<hr id="answer">'
            "Answer side shows myelofibrosis and polycythemia vera"
        )
        self.addon.cache_review_html(
            rendered_answer, card, self.addon.REVIEW_ANSWER_CONTEXT
        )

        self.addon.on_reviewer_did_show_answer(card)

        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(len(engine.spoken), 1)
        self.assertEqual(
            engine.spoken[0][0],
            "Answer side shows myelofibrosis and polycythemia vera",
        )
        self.assertNotIn("Question side", engine.spoken[0][0])

    def test_warm_all_audio_cache_queues_only_speakable_question_text(self):
        speakable_card = _FakeDueCard(
            101,
            (
                "Ruxolitinib has JAK2, myelofibrosis, "
                '<span class="cloze">[...]</span>'
            ),
            question_html="raw text should not be queued",
        )
        empty_card = _FakeDueCard(202, "", question_html="")
        collection = _FakeCollection(
            {
                speakable_card.id: speakable_card,
                empty_card.id: empty_card,
            }
        )
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher
        original_helper = self.addon.speakable_question_text
        helper_card_ids = []

        def spy_speakable_question_text(card, rendered_html=None):
            helper_card_ids.append(card.id)
            return original_helper(card, rendered_html)

        self.addon.speakable_question_text = spy_speakable_question_text

        self.addon.warm_all_audio_cache()
        self._run_all_timers()

        self.assertTrue(prefetcher.started)
        self.assertEqual(collection.find_queries, [""])
        self.assertEqual(collection.get_card_ids, [101, 202])
        self.assertEqual(helper_card_ids, [101, 202])
        self.assertEqual(speakable_card.render_reload_values, [True])
        self.assertEqual(empty_card.render_reload_values, [True])
        self.assertEqual(
            prefetcher.enqueued,
            [
                (
                    "Ruxolitinib has JAK2, myelofibrosis, bla bla bla",
                    _DEFAULT_CONFIG,
                )
            ],
        )
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queued 1 cards for TTS audio caching",
        )

    def test_warm_all_audio_cache_respects_disabled_audio_cache_settings(self):
        for key in ("enabled", "cache_enabled", "prefetch_enabled"):
            with self.subTest(key=key):
                self.addon.mw.addonManager.config = dict(_DEFAULT_CONFIG)
                self.hooks.tooltips.clear()
                prefetcher = _FakePrefetcher()
                self.addon._prefetcher = prefetcher
                self.addon.mw.col = _FakeCollection(
                    {1: _FakeDueCard(1, "speakable")}
                )
                self.set_config(**{key: False})

                self.addon.warm_all_audio_cache()

                self.assertFalse(prefetcher.started)
                self.assertEqual(prefetcher.enqueued, [])
                self.assertEqual(self.addon.mw.col.find_queries, [])
                self.assertEqual(
                    self.hooks.tooltips[-1],
                    "TTS audio caching is disabled",
                )

    def test_warm_all_audio_cache_skips_when_prefetcher_is_still_stopping(self):
        prefetcher = _FakePrefetcher(start_result=False)
        self.addon._prefetcher = prefetcher
        self.addon.mw.col = _FakeCollection({1: _FakeDueCard(1, "speakable")})

        self.addon.warm_all_audio_cache()

        self.assertTrue(prefetcher.started)
        self.assertEqual(prefetcher.enqueued, [])
        self.assertEqual(self.addon.mw.col.find_queries, [])
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Skipped queuing TTS audio because audio generation is still stopping",
        )

    def test_warm_all_audio_cache_counts_only_accepted_enqueue_items(self):
        collection = _FakeCollection({1: _FakeDueCard(1, "speakable")})
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher(enqueue_result=False)
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()
        self._run_all_timers()

        self.assertEqual(len(prefetcher.enqueued), 1)
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queued 0 cards for TTS audio caching",
        )

    def test_warm_all_audio_cache_defers_collection_scan_until_timer_fires(self):
        collection = _FakeCollection({1: _FakeDueCard(1, "speakable")})
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()

        self.assertTrue(prefetcher.started)
        self.assertEqual(collection.find_queries, [])
        self.assertEqual(collection.get_card_ids, [])
        self.assertEqual(prefetcher.enqueued, [])
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queueing TTS audio cache...",
        )
        self.assertEqual(len(self.hooks.timers), 1)

        self._run_all_timers()

        self.assertEqual(collection.find_queries, [""])
        self.assertEqual(collection.get_card_ids, [1])
        self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queued 1 cards for TTS audio caching",
        )

    def test_warm_scan_runs_as_one_background_op_not_ui_timer_batches(self):
        collection = _FakeCollection(
            {card_id: _FakeDueCard(card_id, f"card {card_id}") for card_id in range(1, 51)}
        )
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()

        self.assertEqual(len(self.hooks.timers), 1)
        self._run_next_timer()

        self.assertEqual(self.hooks.timers, [])
        self.assertEqual(len(collection.get_card_ids), 50)
        self.assertEqual(len(prefetcher.enqueued), 50)

    def test_query_op_is_used_without_modal_progress(self):
        ops = []

        class FakeQueryOp:
            def __init__(self, parent, op, success):
                self.op = op
                self.success = success
                self.failure_callback = None
                self.progress = None
                ops.append(self)

            def failure(self, callback):
                self.failure_callback = callback
                return self

            def with_progress(self, label=None):
                self.progress = label
                return self

            def run_in_background(self):
                self.success(self.op(self_addon.mw.col))

        self_addon = self.addon
        operations = types.ModuleType("aqt.operations")
        operations.QueryOp = FakeQueryOp
        sys.modules["aqt.operations"] = operations
        self.addCleanup(sys.modules.pop, "aqt.operations", None)
        collection = _FakeCollection({1: _FakeDueCard(1, "speakable")})
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()
        self.addon.on_sync_did_finish()
        self._run_all_timers()

        self.assertEqual(len(ops), 2)
        # A modal progress dialog would block reviewing for the whole scan.
        self.assertIsNone(ops[0].progress)
        self.assertIsNone(ops[1].progress)
        self.assertIsNotNone(ops[1].failure_callback)
        self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])

    def _write_cached_audio(self, cache, text, speed=1.5):
        audio_cache_module = importlib.import_module("anki_tts_addon.audio_cache")
        key = audio_cache_module.speech_cache_key(
            text, self.addon.TTSEngine.EDGE_VOICE, speed
        )
        cache.path_for_key(key).write_bytes(
            b"x" * audio_cache_module.MIN_AUDIO_BYTES
        )
        return key

    def _cache_key(self, text, speed=1.5):
        audio_cache_module = importlib.import_module("anki_tts_addon.audio_cache")
        return audio_cache_module.speech_cache_key(
            text, self.addon.TTSEngine.EDGE_VOICE, speed
        )

    def _new_cache_state(self, path, now=None):
        state_module = importlib.import_module("anki_tts_addon.audio_cache_state")
        return state_module.AudioCacheState(path, now=now)

    def test_warm_all_audio_cache_marks_missing_keys_pending(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            state = self._new_cache_state(root / "state.json")
            self.addon._audio_cache_state = state
            self.addon.mw.col = _FakeCollection(
                {1: _FakeDueCard(1, "speakable")}
            )
            prefetcher = _FakePrefetcher()
            self.addon._engine = _FakeEngine()
            self.addon._prefetcher = None
            self.addon.AudioPrefetcher = lambda _engine: prefetcher

            self.addon.warm_all_audio_cache()
            self._run_all_timers()

            entry = state.entry(self._cache_key("speakable"))
            self.assertEqual(entry["status"], "pending")
            self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])

    def test_warm_all_audio_cache_skips_cached_keys_without_state_writes(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = self.addon.AudioCache(root / "cache")
            state = self._new_cache_state(root / "state.json")
            key = self._write_cached_audio(cache, "speakable")
            self.addon._audio_cache = cache
            self.addon._audio_cache_state = state
            self.addon.mw.col = _FakeCollection(
                {1: _FakeDueCard(1, "speakable")}
            )
            prefetcher = _FakePrefetcher()
            self.addon._engine = _FakeEngine()
            self.addon._prefetcher = None
            self.addon.AudioPrefetcher = lambda _engine: prefetcher

            self.addon.warm_all_audio_cache()
            self._run_all_timers()

            self.assertIsNone(state.entry(key))
            self.assertEqual(prefetcher.enqueued, [])
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Queued 0 cards for TTS audio caching",
            )

    def test_refused_enqueue_rolls_back_pending_mark(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            state = self._new_cache_state(root / "state.json")
            self.addon._audio_cache_state = state
            self.addon.mw.col = _FakeCollection({1: _FakeDueCard(1, "speakable")})
            prefetcher = _FakePrefetcher(enqueue_result=False)
            self.addon._engine = _FakeEngine()
            self.addon._prefetcher = None
            self.addon.AudioPrefetcher = lambda _engine: prefetcher

            self.addon.warm_all_audio_cache()
            self._run_all_timers()

            self.assertIsNone(state.entry(self._cache_key("speakable")))

    def test_fast_worker_result_is_not_overwritten_by_pending(self):
        class FailsImmediatelyPrefetcher(_FakePrefetcher):
            def enqueue(self, text, config):
                accepted = super().enqueue(text, config)
                self.result_callback(text, config, False, "edge unavailable")
                return accepted

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            state = self._new_cache_state(root / "state.json")
            self.addon._audio_cache_state = state
            self.addon.mw.col = _FakeCollection({1: _FakeDueCard(1, "speakable")})
            prefetcher = FailsImmediatelyPrefetcher()
            self.addon._engine = _FakeEngine()
            self.addon._prefetcher = None
            self.addon.AudioPrefetcher = lambda _engine: prefetcher

            self.addon.warm_all_audio_cache()
            self._run_all_timers()

            entry = state.entry(self._cache_key("speakable"))
            self.assertEqual(entry["status"], "failed")
            self.assertIsNotNone(entry["next_retry_at"])

    def test_warm_all_audio_cache_skips_failed_key_until_retry_time(self):
        class Clock:
            def __init__(self, value):
                self.value = value

            def __call__(self):
                return self.value

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            clock = Clock(100)
            state = self._new_cache_state(root / "state.json", now=clock)
            state.mark_failed(self._cache_key("speakable"), "network failed")
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = state
            self.addon.mw.col = _FakeCollection(
                {1: _FakeDueCard(1, "speakable")}
            )
            prefetcher = _FakePrefetcher()
            self.addon._engine = _FakeEngine()
            self.addon._prefetcher = None
            self.addon.AudioPrefetcher = lambda _engine: prefetcher

            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            clock.value = 130
            self.addon.warm_all_audio_cache()
            self._run_all_timers()

            self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])

    def test_prefetch_result_callback_marks_success_and_failure_in_state(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._new_cache_state(root / "state.json")
            state.mark_pending(self._cache_key("succeeded"), "succeeded")
            self.addon._audio_cache_state = state

            self.addon._on_audio_prefetch_result(
                "succeeded", {"speed": 1.5}, True, None
            )
            self.addon._on_audio_prefetch_result(
                "failed", {"speed": 1.5}, False, "network failed"
            )

            self.assertIsNone(state.entry(self._cache_key("succeeded")))
            failure = state.entry(self._cache_key("failed"))
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["last_error"], "network failed")
            self.assertEqual(failure["text"], "failed")

    def test_prefetch_result_callback_clears_pending_for_canceled_prefetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._new_cache_state(root / "state.json")
            key = self._cache_key("canceled")
            state.mark_pending(key, "canceled")
            self.addon._audio_cache_state = state

            self.addon._on_audio_prefetch_result(
                "canceled", {"speed": 1.5}, None, None
            )

            self.assertIsNone(state.entry(key))

    def test_prefetch_result_callback_ignored_after_shutdown(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._new_cache_state(Path(temp_dir) / "state.json")
            self.addon._audio_cache_state = state
            self.addon._on_profile_will_close()
            self.addon._audio_cache_state = state

            self.addon._on_audio_prefetch_result(
                "late", {"speed": 1.5}, False, "boom"
            )

            self.assertIsNone(state.entry(self._cache_key("late")))

    def _warm_with_cache(self, root, cards, prefetcher=None):
        cache = self.addon.AudioCache(root / "cache")
        state = self._new_cache_state(root / "state.json")
        self.addon._audio_cache = cache
        self.addon._audio_cache_state = state
        collection = _FakeCollection(cards)
        self.addon.mw.col = collection
        prefetcher = prefetcher or _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher
        return cache, state, collection, prefetcher

    def test_audio_cache_status_includes_pending_and_failed_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache, state, _collection, prefetcher = self._warm_with_cache(
                root,
                {
                    1: _FakeDueCard(1, "cached"),
                    2: _FakeDueCard(2, "failed"),
                    3: _FakeDueCard(3, "missing"),
                },
            )
            self._write_cached_audio(cache, "cached")
            state.mark_failed(self._cache_key("failed"), "network failed")

            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            prefetcher.status_result = {"running": True, "active": 0, "pending": 1}
            self.hooks.tooltips.clear()
            self.addon.show_audio_cache_status()

            self.assertEqual(
                self.hooks.tooltips,
                [
                    "Audio cache incomplete: 1/3 cards cached; 2 missing; "
                    "1 pending; 1 failed; worker running"
                ],
            )

    def test_audio_cache_status_reports_complete_cache_for_speakable_cards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache, _state, _collection, _prefetcher = self._warm_with_cache(
                Path(temp_dir),
                {
                    1: _FakeDueCard(1, "speakable"),
                    2: _FakeDueCard(2, "", question_html=""),
                },
            )
            self._write_cached_audio(cache, "speakable")

            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            self.addon.show_audio_cache_status()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache complete: 1/1 cards cached; worker idle",
        )

    def test_audio_cache_status_counts_worker_successes_live(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _cache, _state, _collection, prefetcher = self._warm_with_cache(
                Path(temp_dir),
                {1: _FakeDueCard(1, "one"), 2: _FakeDueCard(2, "two")},
            )

            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            self.addon.show_audio_cache_status()
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Audio cache incomplete: 0/2 cards cached; 2 missing; worker idle",
            )

            prefetcher.result_callback("one", _DEFAULT_CONFIG, True, None)
            self.addon.show_audio_cache_status()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache incomplete: 1/2 cards cached; 1 missing; worker idle",
        )

    def test_audio_cache_status_uses_persisted_counts_after_restart(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._new_cache_state(Path(temp_dir) / "state.json")
            state.set_meta("scan_stats", {"total": 10, "missing": 4, "at": 1})
            self.addon._audio_cache_state = state
            self.addon.mw.col = _FakeCollection({})

            self.addon.show_audio_cache_status()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache incomplete: 6/10 cards cached; 4 missing; "
            "as of last full scan; worker idle",
        )

    def test_status_snapshot_exposes_worker_scan_and_engine_state(self):
        class EngineWithStatus(_FakeEngine):
            def status_snapshot(self):
                return {"edge": {"open": True, "retry_in": 42}, "live": {"hits": 3}}

        self.addon._engine = EngineWithStatus()
        self.addon._prefetcher = _FakePrefetcher(
            status_result={"running": True, "active": 1, "pending": 2, "paused": True}
        )
        self.addon.mw.col = _FakeCollection({})

        snapshot = self.addon.audio_cache_status_snapshot()

        self.assertEqual(snapshot["worker"]["pending"], 2)
        self.assertTrue(snapshot["worker"]["paused"])
        self.assertEqual(snapshot["engine"]["live"], {"hits": 3})
        self.assertIsNone(snapshot["scan"]["running"])
        self.addon.show_audio_cache_status()
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache: no full scan yet (use Warm All Audio Cache); "
            "3 pending; Edge paused, retry in 42s; worker paused",
        )

    def test_warm_all_audio_cache_reports_final_status_when_prefetcher_becomes_idle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache, _state, _collection, prefetcher = self._warm_with_cache(
                Path(temp_dir), {1: _FakeDueCard(1, "speakable")}
            )

            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Queued 1 cards for TTS audio caching",
            )
            self._write_cached_audio(cache, "speakable")
            prefetcher.result_callback("speakable", _DEFAULT_CONFIG, True, None)
            prefetcher.idle_callback()
            self._run_all_timers()

        self.assertEqual(
            self.hooks.tooltips[-2],
            "Queued 1 cards for TTS audio caching",
        )
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache complete: 1/1 cards cached; worker idle",
        )

    def test_idle_while_worker_still_busy_does_not_notify(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _cache, _state, _collection, prefetcher = self._warm_with_cache(
                Path(temp_dir), {1: _FakeDueCard(1, "speakable")}
            )
            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            prefetcher.status_result = {"running": True, "active": 1, "pending": 0}

            prefetcher.idle_callback()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queued 1 cards for TTS audio caching",
        )

    def test_auto_warm_never_shows_status_on_idle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            _cache, _state, _collection, prefetcher = self._warm_with_cache(
                Path(temp_dir), {1: _FakeDueCard(1, "speakable")}
            )
            self.addon.on_sync_did_finish()
            self._run_all_timers()
            prefetcher.result_callback("speakable", _DEFAULT_CONFIG, True, None)
            prefetcher.idle_callback()
            self._run_all_timers()

        self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])
        self.assertEqual(self.hooks.tooltips, [])

    def test_warm_all_audio_cache_shows_queued_before_fast_idle_status(self):
        class FastIdlePrefetcher(_FakePrefetcher):
            def __init__(self, cache_writer):
                super().__init__()
                self.cache_writer = cache_writer

            def enqueue(self, text, config):
                accepted = super().enqueue(text, config)
                if accepted:
                    self.cache_writer(text)
                    self.result_callback(text, config, True, None)
                    self.idle_callback()
                return accepted

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            cache = self.addon.AudioCache(root / "cache")
            prefetcher = FastIdlePrefetcher(
                lambda text: self._write_cached_audio(cache, text)
            )
            self._warm_with_cache(
                root, {1: _FakeDueCard(1, "speakable")}, prefetcher
            )
            self.addon._audio_cache = cache

            self.addon.warm_all_audio_cache()
            self._run_all_timers()

        self.assertEqual(
            self.hooks.tooltips[-2],
            "Queued 1 cards for TTS audio caching",
        )
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache complete: 1/1 cards cached; worker idle",
        )

    def test_profile_open_defers_auto_warm_until_timer_fires(self):
        collection = _FakeCollection({1: _FakeDueCard(1, "speakable")})
        self.addon.mw.col = collection
        self.addon._menu_added = True
        self.addon._hooks_registered = True
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon._on_profile_did_open()

        self.assertEqual(collection.find_queries, [])
        self.assertEqual(prefetcher.enqueued, [])
        self.assertEqual(
            [delay for delay, _ in self._delayed_timers()],
            [self.addon.AUTO_WARM_START_DELAY_MS],
        )

        self._run_all_timers()

        self.assertEqual(collection.find_queries, [""])
        self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])
        self.assertEqual(self.hooks.tooltips, [])

    def test_sync_finish_defers_auto_warm_until_timer_fires(self):
        collection = _FakeCollection({1: _FakeDueCard(1, "speakable")})
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.on_sync_did_finish()

        self.assertEqual(collection.find_queries, [])
        self.assertEqual(prefetcher.enqueued, [])
        self.assertEqual(len(self.hooks.timers), 1)

        self._run_all_timers()

        self.assertEqual(collection.find_queries, [""])
        self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])
        self.assertEqual(self.hooks.tooltips, [])

    def _set_up_auto_warm(self, cards):
        collection = _FakeCollection(cards)
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher
        return collection, prefetcher

    def test_scan_runs_in_small_collection_ops_so_anki_ops_can_interleave(self):
        self.addon.SCAN_CHUNK_CARDS = 10
        collection, prefetcher = self._set_up_auto_warm(
            {card_id: _FakeDueCard(card_id, f"card {card_id}") for card_id in range(1, 26)}
        )

        self.addon.warm_all_audio_cache()
        self._run_all_timers()

        scan_ops = [
            call for call in self.hooks.background_calls
            if "uses_collection" not in call["kwargs"]
        ]
        self.assertEqual(len(scan_ops), 3)
        self.assertEqual(len(collection.get_card_ids), 25)
        self.assertEqual(len(prefetcher.enqueued), 25)
        self.assertIsNone(self.addon._warm_job)
        self.assertEqual(
            self.hooks.tooltips[-1], "Queued 25 cards for TTS audio caching"
        )

    def test_sync_start_between_scan_chunks_stops_the_scan(self):
        self.addon.SCAN_CHUNK_CARDS = 10
        collection, prefetcher = self._set_up_auto_warm(
            {card_id: _FakeDueCard(card_id, f"card {card_id}") for card_id in range(1, 26)}
        )

        self.addon.warm_all_audio_cache()
        self._run_next_timer()  # first chunk
        self.addon.on_sync_will_start()
        self._run_all_timers()

        self.assertEqual(len(collection.get_card_ids), 10)
        self.assertEqual(prefetcher.enqueued, [])
        self.assertIsNone(self.addon._warm_job)
        # Deferred, not dropped: it runs again once the sync is over.
        self.assertIsNotNone(self.addon._warm_rescan)
        self.assertEqual(self.addon._warm_stats.snapshot()["recent_errors"], ())

    def test_scan_op_ignores_a_different_collection(self):
        collection, prefetcher = self._set_up_auto_warm(
            {1: _FakeDueCard(1, "speakable")}
        )
        self.addon.warm_all_audio_cache()
        self.addon.mw.col = _FakeCollection({2: _FakeDueCard(2, "other")})
        self._run_all_timers()

        self.assertEqual(collection.get_card_ids, [])
        self.assertEqual(prefetcher.enqueued, [])
        self.assertIsNone(self.addon._warm_job)

    def test_incremental_query_ignores_unsynced_usn_and_checks_note_types(self):
        collection = _FakeCollection({})
        self.addon._select_card_ids(collection, {"mod": 100, "usn": 7})

        sql, args = collection.db.list_queries[-1]
        self.assertNotIn("-1", sql)
        self.assertIn("notetypes", sql)
        self.assertEqual(args, (100, 100, 7, 7, 100, 7))

    def test_incremental_query_falls_back_without_notetypes_table(self):
        class OldSchemaDb(_FakeDb):
            def list(self, sql, *args):
                if "notetypes" in sql:
                    raise RuntimeError("no such table: notetypes")
                return super().list(sql, *args)

        collection = _FakeCollection({})
        collection.changed_ids = [5]
        collection.db = OldSchemaDb(collection)

        self.assertEqual(
            self.addon._select_card_ids(collection, {"mod": 1, "usn": 2}), [5]
        )

    def test_question_hook_does_not_rerender_when_reviewer_html_is_cached(self):
        class CountingCard(_FakeCard):
            renders = 0

            def question(self):
                CountingCard.renders += 1
                return super().question()

        card = CountingCard()
        engine = _FakeEngine()
        self.addon._engine = engine
        self.addon.cache_review_html(
            "Shown question", card, self.addon.REVIEW_QUESTION_CONTEXT
        )

        self.addon.on_reviewer_did_show_question(card)

        self.assertEqual(CountingCard.renders, 0)
        self.assertEqual(engine.spoken[0][0], "Shown question")

    def test_review_html_cache_is_bounded(self):
        module = importlib.import_module("anki_tts_addon.review_html_cache")
        for card_id in range(module.MAX_ENTRIES + 10):
            card = types.SimpleNamespace(id=card_id)
            module.cache_review_html("q", card, module.REVIEW_QUESTION_CONTEXT)

        self.assertEqual(len(module._review_html_by_card), module.MAX_ENTRIES)
        newest = types.SimpleNamespace(id=module.MAX_ENTRIES + 9)
        oldest = types.SimpleNamespace(id=0)
        ctx = module.REVIEW_QUESTION_CONTEXT
        self.assertEqual(module.get_review_html(newest, ctx, None), "q")
        self.assertIsNone(module.get_review_html(oldest, ctx, None))

    def test_reviewer_end_does_not_create_an_engine(self):
        self.addon._engine = None

        self.addon.on_reviewer_will_end()

        self.assertIsNone(self.addon._engine)

    def test_profile_close_finishes_cleanup_when_a_step_fails(self):
        class BrokenStateEngine(_FakeEngine):
            def stop(self):
                raise RuntimeError("speech process vanished")

        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = _FakePrefetcher()
        self.addon._on_profile_did_open()
        prefetcher = self.addon._prefetcher
        self.addon._engine = BrokenStateEngine()

        with self.assertLogs(self.addon.log, level="WARNING"):
            self.addon._on_profile_will_close()

        self.assertTrue(prefetcher.stopped)
        self.assertIsNone(self.addon._engine)
        self.assertFalse(self.addon._hooks_registered)
        self.assertEqual(self.hooks.reviewer_did_show_question, [])
        self.assertEqual(self.hooks.sync_did_finish, [])

    def test_state_flush_after_idle_does_not_use_collection_executor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            state = self._new_cache_state(Path(temp_dir) / "state.json")
            state.set_meta("x", 1)
            self.addon._audio_cache_state = state

            self.addon._flush_state_in_background()

            self.assertEqual(
                self.hooks.background_calls[-1]["kwargs"],
                {"uses_collection": False},
            )

    def test_idle_handler_errors_never_escape_to_anki(self):
        self.addon._pending_watermark = ("key", {"mod": 1})

        def broken_state():
            raise OSError("disk full")

        self.addon.audio_cache_state = broken_state
        with self.assertLogs(self.addon.log, level="WARNING"):
            self.addon._on_prefetch_idle_main()

    def test_sync_uses_incremental_scan_after_watermark_is_committed(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = self._new_cache_state(root / "state.json")
            collection, prefetcher = self._set_up_auto_warm(
                {1: _FakeDueCard(1, "one"), 2: _FakeDueCard(2, "two")}
            )

            self.addon.on_sync_did_finish()
            self._run_all_timers()
            self.assertEqual(collection.find_queries, [""])
            prefetcher.result_callback("one", _DEFAULT_CONFIG, True, None)
            prefetcher.result_callback("two", _DEFAULT_CONFIG, True, None)
            prefetcher.idle_callback()
            self._run_all_timers()

            collection.changed_ids = [2]
            collection.get_card_ids.clear()
            self.addon.on_sync_did_finish()
            self._run_all_timers()

            self.assertEqual(collection.find_queries, [""])
            self.assertEqual(len(collection.db.list_queries), 1)
            self.assertEqual(collection.get_card_ids, [2])

    def test_watermark_not_committed_while_queued_audio_is_unfinished(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = self._new_cache_state(root / "state.json")
            collection, prefetcher = self._set_up_auto_warm(
                {1: _FakeDueCard(1, "one")}
            )

            self.addon.on_sync_did_finish()
            self._run_all_timers()
            self.addon.on_sync_did_finish()
            self._run_all_timers()

            # Quit before the worker drained: next pass must scan everything.
            self.assertEqual(collection.find_queries, ["", ""])
            self.assertEqual(collection.db.list_queries, [])

    def test_speed_change_forces_full_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = self._new_cache_state(root / "state.json")
            collection, prefetcher = self._set_up_auto_warm({})

            self.addon.on_sync_did_finish()
            self._run_all_timers()
            self.set_config(speed=1.25)
            self.addon.on_sync_did_finish()
            self._run_all_timers()

            self.assertEqual(collection.find_queries, ["", ""])

    def test_sync_will_start_cancels_scheduled_and_running_warm(self):
        collection, prefetcher = self._set_up_auto_warm(
            {1: _FakeDueCard(1, "speakable")}
        )
        self.addon.on_sync_did_finish()
        self.addon.on_sync_will_start()
        self._run_all_timers()
        self.assertEqual(collection.find_queries, [])

        self.addon.warm_all_audio_cache()
        self.assertEqual(
            self.hooks.tooltips[-1],
            "TTS audio scan will run after the current scan or sync finishes",
        )
        self._run_all_timers()
        self.assertEqual(collection.find_queries, [])

        self.addon.on_sync_did_finish()
        self._run_all_timers()

        # The manual request deferred by the sync runs as a full, manual scan.
        self.assertEqual(collection.find_queries, [""])
        self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queued 1 cards for TTS audio caching",
        )

    def test_sync_start_aborts_scan_already_in_background(self):
        collection, prefetcher = self._set_up_auto_warm(
            {1: _FakeDueCard(1, "speakable")}
        )
        self.addon.on_sync_did_finish()
        self._run_next_timer()  # scheduled timer: submits the background op
        self.addon.on_sync_will_start()
        self._run_all_timers()

        self.assertEqual(collection.get_card_ids, [])
        self.assertEqual(prefetcher.enqueued, [])
        self.assertIsNone(self.addon._warm_job)

    def test_profile_job_is_not_current_while_syncing_or_collection_closed(self):
        collection = _FakeCollection({})
        self.addon.mw.col = collection
        generation = self.addon._profile_generation

        self.assertTrue(self.addon._profile_job_is_current(generation, collection))
        self.addon.on_sync_will_start()
        self.assertFalse(self.addon._profile_job_is_current(generation, collection))
        self.addon.on_sync_did_finish()
        collection.db = None
        self.assertFalse(self.addon._profile_job_is_current(generation, collection))

    def test_overlapping_triggers_run_one_scan_then_one_rescan(self):
        collection, prefetcher = self._set_up_auto_warm(
            {1: _FakeDueCard(1, "speakable")}
        )
        self.addon.on_sync_did_finish()
        self._run_next_timer()  # job running, background op pending
        self.addon.warm_all_audio_cache()
        self.addon.warm_all_audio_cache()

        self.assertEqual(len(self.hooks.timers), 1)
        self._run_all_timers()

        self.assertEqual(collection.find_queries, ["", ""])
        self.assertIsNone(self.addon._warm_job)
        self.assertIsNone(self.addon._warm_rescan)

    def test_stale_scheduled_timer_does_not_clear_new_schedule(self):
        collection, _prefetcher = self._set_up_auto_warm({})
        self.addon._schedule_auto_warm_all_audio_cache(10)
        stale = self.hooks.timers.pop(0)[1]
        self.addon._cancel_scheduled_auto_warm()
        self.addon._schedule_auto_warm_all_audio_cache(10)

        stale()
        self.addon._schedule_auto_warm_all_audio_cache(10)

        self.assertEqual(len(self.hooks.timers), 1)
        self.assertTrue(self.addon._warm_scheduled)

    def test_deleted_card_during_scan_is_skipped_without_error(self):
        class DeletingCollection(_FakeCollection):
            def get_card(self, card_id):
                if card_id == 2:
                    raise KeyError("card deleted")
                return super().get_card(card_id)

        collection = DeletingCollection(
            {
                1: _FakeDueCard(1, "first"),
                2: _FakeDueCard(2, "deleted"),
                3: _FakeDueCard(3, "third"),
            }
        )
        self.addon.mw.col = collection
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()
        self._run_all_timers()

        self.assertEqual(
            prefetcher.enqueued,
            [("first", _DEFAULT_CONFIG), ("third", _DEFAULT_CONFIG)],
        )
        self.assertIsNone(self.addon._warm_job)
        self.assertEqual(
            self.addon.audio_cache_status_snapshot()["scan"]["last"]["errors"], 1
        )

    def test_scan_failure_finishes_job_and_allows_next_scan(self):
        class BrokenCollection(_FakeCollection):
            def find_cards(self, query):
                super().find_cards(query)
                raise RuntimeError("collection closed")

        collection = BrokenCollection({})
        self.addon.mw.col = collection
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = _FakePrefetcher()

        with self.assertLogs(self.addon.log, level="WARNING"):
            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            self.addon.warm_all_audio_cache()
            self._run_all_timers()

        self.assertEqual(collection.find_queries, ["", ""])
        self.assertIsNone(self.addon._warm_job)
        self.assertEqual(
            self.hooks.tooltips[-1],
            "TTS audio scan failed: collection closed",
        )

    def test_warm_stops_queueing_when_tts_disabled_during_scan(self):
        collection, prefetcher = self._set_up_auto_warm(
            {1: _FakeDueCard(1, "speakable")}
        )
        self.addon.warm_all_audio_cache()
        self.set_config(enabled=False)
        self.addon._sync_prefetcher_with_config(self.addon.get_config())
        self._run_all_timers()

        self.assertEqual(prefetcher.enqueued, [])
        self.assertIsNone(self.addon._warm_job)

    def test_failed_items_with_text_are_retried_without_rescanning_card(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            state = self._new_cache_state(root / "state.json", now=lambda: 0)
            state.mark_failed(self._cache_key("old failure"), "boom", "old failure")
            self.addon._audio_cache_state = state
            state._now = lambda: 10_000
            collection, prefetcher = self._set_up_auto_warm({})

            self.addon.on_sync_did_finish()
            self._run_all_timers()

            self.assertEqual(prefetcher.enqueued, [("old failure", _DEFAULT_CONFIG)])

    def test_profile_open_registers_quit_hook_once(self):
        connected = []
        self.addon.mw.app = types.SimpleNamespace(
            aboutToQuit=types.SimpleNamespace(connect=connected.append)
        )
        self.addon._menu_added = True
        self.addon._hooks_registered = True
        self.addon._prefetcher = _FakePrefetcher()

        self.addon._on_profile_did_open()
        self.addon._on_profile_did_open()

        self.assertEqual(connected, [self.addon._on_app_about_to_quit])

    def test_clear_audio_cache_stops_resets_prefetcher_and_removes_audio_files(self):
        prefetcher = _FakePrefetcher()
        engine = _FakeEngine()
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._new_cache_state(root / "state.json")
            state.mark_failed(self._cache_key("cached"), "network failed")
            self.addon._audio_cache_state = state
            audio_file = root / "cached.mp3"
            second_audio_file = root / "other.mp3"
            non_audio_file = root / "keep.txt"
            audio_file.write_bytes(b"audio")
            second_audio_file.write_bytes(b"audio")
            non_audio_file.write_text("keep")
            self.addon._audio_cache = types.SimpleNamespace(
                iter_audio_files=lambda: iter([audio_file, second_audio_file])
            )

            self.addon.clear_audio_cache()

            # Files are unlinked in the background, never on the UI thread.
            self.assertTrue(audio_file.exists())
            self.assertEqual(
                self.hooks.background_calls[-1]["kwargs"],
                {"uses_collection": False},
            )
            self._run_all_timers()

            self.assertFalse(audio_file.exists())
            self.assertFalse(second_audio_file.exists())
            self.assertTrue(non_audio_file.exists())

        self.assertTrue(prefetcher.stopped)
        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.CLEAR_AUDIO_CACHE_STOP_TIMEOUT_SECONDS],
        )
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.CLEAR_AUDIO_CACHE_STOP_TIMEOUT_SECONDS],
        )
        self.assertIsNone(self.addon._prefetcher)
        self.assertEqual(self.hooks.tooltips[-1], "Cleared 2 TTS audio files")
        self.assertEqual(
            state.summary(),
            {"pending": 0, "failed": 0},
        )

    def test_confirm_clear_audio_cache_asks_first(self):
        calls = []
        self.addon.clear_audio_cache = lambda: calls.append("clear")

        self.hooks.ask_user_answers.append(False)
        self.addon.confirm_clear_audio_cache()
        self.assertEqual(calls, [])

        self.hooks.ask_user_answers.append(True)
        self.addon.confirm_clear_audio_cache()
        self.assertEqual(calls, ["clear"])
        self.assertEqual(len(self.hooks.ask_user_questions), 2)

    def test_clear_audio_cache_cancels_engine_before_stopping_prefetcher(self):
        events = []

        class RecordingEngine(_FakeEngine):
            def stop(self):
                events.append("engine.stop")
                super().stop()

        class RecordingPrefetcher(_FakePrefetcher):
            def stop(self, timeout=None):
                events.append("prefetcher.stop")
                return super().stop(timeout)

        self.addon._engine = RecordingEngine()
        self.addon._prefetcher = RecordingPrefetcher()
        self.addon._audio_cache = types.SimpleNamespace(
            iter_audio_files=lambda: iter([])
        )

        self.addon.clear_audio_cache()

        self.assertEqual(events[:2], ["engine.stop", "prefetcher.stop"])

    def test_clear_audio_cache_skips_deleting_files_when_prefetcher_stop_times_out(self):
        prefetcher = _FakePrefetcher(stop_result=False)
        self.addon._prefetcher = prefetcher

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_file = root / "cached.mp3"
            audio_file.write_bytes(b"audio")
            self.addon._audio_cache = types.SimpleNamespace(
                iter_audio_files=lambda: iter([audio_file])
            )

            self.addon.clear_audio_cache()

            self.assertTrue(audio_file.exists())

        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.CLEAR_AUDIO_CACHE_STOP_TIMEOUT_SECONDS],
        )
        self.assertIs(self.addon._prefetcher, prefetcher)
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Skipped clearing TTS audio cache because audio generation is still in progress",
        )

    def test_clear_audio_cache_skips_deleting_files_when_live_speech_is_active(self):
        prefetcher = _FakePrefetcher()
        engine = _FakeEngine(wait_result=False)
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine

        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            audio_file = root / "cached.mp3"
            audio_file.write_bytes(b"audio")
            self.addon._audio_cache = types.SimpleNamespace(
                iter_audio_files=lambda: iter([audio_file])
            )

            self.addon.clear_audio_cache()

            self.assertTrue(audio_file.exists())

        self.assertIsNone(self.addon._prefetcher)
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.CLEAR_AUDIO_CACHE_STOP_TIMEOUT_SECONDS],
        )
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Skipped clearing TTS audio cache because audio generation is still in progress",
        )

    def test_toggle_tts_disabling_stops_prefetcher_with_timeout(self):
        prefetcher = _FakePrefetcher()
        engine = _FakeEngine()
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine

        self.addon.toggle_tts()

        self.assertFalse(self.addon.mw.addonManager.config["enabled"])
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )
        self.assertIsNone(self.addon._prefetcher)

    def test_settings_save_disabling_stops_prefetcher_with_timeout(self):
        prefetcher = _FakePrefetcher()
        engine = _FakeEngine()
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine
        dialog = object.__new__(self.addon.SettingsDialog)
        dialog.conf = self.addon.get_config()
        dialog.enabled_cb = _FakeCheckBox(False)
        dialog.speed_spin = _FakeSpinBox(1.25)
        dialog.speak_q_cb = _FakeCheckBox(True)
        dialog.speak_a_cb = _FakeCheckBox(False)
        dialog.fallback_cb = _FakeCheckBox(True)
        accepted = []
        dialog.accept = lambda: accepted.append(True)

        dialog._save()

        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )
        self.assertEqual(engine.stop_count, 1)
        self.assertIsNone(self.addon._prefetcher)
        self.assertFalse(self.addon.mw.addonManager.config["enabled"])
        self.assertEqual(self.addon.mw.addonManager.config["speed"], 1.25)
        self.assertEqual(accepted, [True])


if __name__ == "__main__":
    unittest.main()
