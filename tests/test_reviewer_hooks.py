import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path


_AQT_MODULES = ("aqt", "aqt.qt", "aqt.utils")
_MISSING = object()
_DEFAULT_CONFIG = {
    "enabled": True,
    "speak_question": True,
    "speak_answer": True,
    "fallback_to_system": True,
    "speed": 1.5,
    "cache_enabled": True,
    "prefetch_enabled": True,
    "cache_max_mb": 2048,
}


def _purge_addon_modules():
    for name in list(sys.modules):
        if name == "anki_tts_addon" or name.startswith("anki_tts_addon."):
            del sys.modules[name]


class _FakeAddonManager:
    def __init__(self):
        self.config = dict(_DEFAULT_CONFIG)

    def getConfig(self, _name):
        return dict(self.config)

    def writeConfig(self, _name, conf):
        self.config = dict(conf)


class _FakeEngine:
    def __init__(self, wait_result=True):
        self.stop_count = 0
        self.spoken = []
        self.wait_result = wait_result
        self.wait_timeouts = []

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


class _FakeCollection:
    def __init__(self, cards):
        self.cards = dict(cards)
        self.find_queries = []
        self.get_card_ids = []

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


def _install_fake_aqt():
    hooks = types.SimpleNamespace(
        card_will_show=[],
        reviewer_did_show_question=[],
        reviewer_did_show_answer=[],
        reviewer_will_end=[],
        profile_did_open=[],
        profile_will_close=[],
        sync_did_finish=[],
        menus=[],
        tooltips=[],
        timers=[],
    )

    def add_menu(menu):
        hooks.menus.append(menu)

    aqt_module = types.ModuleType("aqt")
    aqt_module.gui_hooks = hooks
    aqt_module.mw = types.SimpleNamespace(
        addonManager=_FakeAddonManager(),
        taskman=types.SimpleNamespace(run_on_main=lambda fn: fn()),
        col=None,
        form=types.SimpleNamespace(
            menubar=types.SimpleNamespace(addMenu=add_menu)
        ),
    )

    qt_module = types.ModuleType("aqt.qt")

    class _FakeSignal:
        def __init__(self):
            self.callbacks = []

        def connect(self, callback):
            self.callbacks.append(callback)

    class _FakeAction:
        def __init__(self, *args, **_kwargs):
            self.text = args[0] if args else ""
            self.triggered = _FakeSignal()
            self.shortcut = None

        def setShortcut(self, shortcut):
            self.shortcut = shortcut

    class _FakeMenu:
        def __init__(self, *args, **_kwargs):
            self.title = args[0] if args else ""
            self.actions = []

        def addAction(self, action):
            self.actions.append(action)

    class _FakeTimer:
        @staticmethod
        def singleShot(delay_ms, callback):
            hooks.timers.append((delay_ms, callback))

    class _FakeWidget:
        def __init__(self, *_args, **_kwargs):
            pass

    qt_module.QAction = _FakeAction
    qt_module.QMenu = _FakeMenu
    qt_module.QDialog = _FakeWidget
    qt_module.QVBoxLayout = _FakeWidget
    qt_module.QHBoxLayout = _FakeWidget
    qt_module.QLabel = _FakeWidget
    qt_module.QDoubleSpinBox = _FakeWidget
    qt_module.QCheckBox = _FakeWidget
    qt_module.QPushButton = _FakeWidget
    qt_module.QGroupBox = _FakeWidget
    qt_module.QTimer = _FakeTimer

    utils_module = types.ModuleType("aqt.utils")
    utils_module.tooltip = lambda msg: hooks.tooltips.append(msg)

    sys.modules["aqt"] = aqt_module
    sys.modules["aqt.qt"] = qt_module
    sys.modules["aqt.utils"] = utils_module

    return hooks


class ReviewerHookTests(unittest.TestCase):
    def setUp(self):
        self._saved_modules = {
            name: sys.modules.get(name, _MISSING) for name in _AQT_MODULES
        }
        _purge_addon_modules()
        self.hooks = _install_fake_aqt()
        self.addon = importlib.import_module("anki_tts_addon")

    def tearDown(self):
        _purge_addon_modules()
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
                "Clear Audio Cache",
            ],
        )
        warm_action = actions[action_texts.index("Warm All Audio Cache")]
        status_action = actions[action_texts.index("Audio Cache Status")]
        clear_action = actions[action_texts.index("Clear Audio Cache")]
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
            [self.addon.clear_audio_cache],
        )

    def test_profile_open_registers_sync_hook_when_available(self):
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.addon._prefetcher = _FakePrefetcher()

        self.addon._on_profile_did_open()

        self.assertIn(self.addon.on_sync_did_finish, self.hooks.sync_did_finish)

    def test_profile_open_cleans_stale_audio_temp_files_before_warming(self):
        events = []

        class RecordingCache:
            def cleanup_temp_files(self):
                events.append("cleanup")
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

        self.assertEqual(events, ["cleanup"])

        self.hooks.timers.pop(0)[1]()

        self.assertEqual(events, ["cleanup", "warm"])

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
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )
        self.assertIsNone(self.addon._prefetcher)
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )
        self.assertIsNone(self.addon._engine)

    def test_profile_close_preserves_state_when_prefetcher_stop_times_out(self):
        prefetcher = _FakePrefetcher(stop_result=False)
        engine = _FakeEngine()
        audio_cache = object()
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine
        self.addon._audio_cache = audio_cache
        self.addon._engine_cache_enabled = True

        self.addon._on_profile_will_close()

        self.assertEqual(
            prefetcher.stop_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )
        self.assertIs(self.addon._prefetcher, prefetcher)
        self.assertEqual(engine.stop_count, 1)
        self.assertIs(self.addon._engine, engine)
        self.assertIs(self.addon._audio_cache, audio_cache)
        self.assertTrue(self.addon._engine_cache_enabled)

    def test_profile_close_preserves_state_when_live_speech_stop_times_out(self):
        prefetcher = _FakePrefetcher()
        engine = _FakeEngine(wait_result=False)
        audio_cache = object()
        self.addon._prefetcher = prefetcher
        self.addon._engine = engine
        self.addon._audio_cache = audio_cache
        self.addon._engine_cache_enabled = True

        self.addon._on_profile_will_close()

        self.assertIsNone(self.addon._prefetcher)
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )
        self.assertIs(self.addon._engine, engine)
        self.assertIs(self.addon._audio_cache, audio_cache)
        self.assertTrue(self.addon._engine_cache_enabled)

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

    def test_profile_close_invalidates_pending_status_timer(self):
        old_collection = _FakeCollection({1: _FakeDueCard(1, "old")})
        new_collection = _FakeCollection({2: _FakeDueCard(2, "new")})
        self.addon.mw.col = old_collection

        self.addon.show_audio_cache_status()
        self.addon._on_profile_will_close()
        self.addon.mw.col = new_collection
        self._run_all_timers()

        self.assertEqual(old_collection.find_queries, [])
        self.assertEqual(new_collection.find_queries, [])
        self.assertEqual(
            self.hooks.tooltips,
            ["Checking TTS audio cache status..."],
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

        self.assertEqual(len(self.hooks.timers), 2)

        self._run_next_timer()

        self.assertEqual(old_collection.find_queries, [])
        self.assertEqual(new_collection.find_queries, [])
        self.assertEqual(prefetcher.enqueued, [])

        self._run_next_timer()

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

    def test_engine_keeps_existing_engine_when_prefetcher_stop_times_out(self):
        engine = _FakeEngine()
        prefetcher = _FakePrefetcher(stop_result=False)
        self.addon._engine = engine
        self.addon._engine_cache_enabled = True
        self.addon._prefetcher = prefetcher
        self.set_config(cache_enabled=False)

        result = self.addon.engine()

        self.assertIs(result, engine)
        self.assertIs(self.addon._engine, engine)
        self.assertTrue(self.addon._engine_cache_enabled)
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

    def test_engine_keeps_existing_engine_when_live_speech_stop_times_out(self):
        engine = _FakeEngine(wait_result=False)
        self.addon._engine = engine
        self.addon._engine_cache_enabled = True
        self.addon._prefetcher = None
        self.set_config(cache_enabled=False)

        result = self.addon.engine()

        self.assertIs(result, engine)
        self.assertIs(self.addon._engine, engine)
        self.assertTrue(self.addon._engine_cache_enabled)
        self.assertEqual(engine.stop_count, 1)
        self.assertEqual(
            engine.wait_timeouts,
            [self.addon.PREFETCH_STOP_TIMEOUT_SECONDS],
        )

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

    def test_warm_all_audio_cache_batches_card_rendering_across_timer_ticks(self):
        collection = _FakeCollection(
            {
                1: _FakeDueCard(1, "first"),
                2: _FakeDueCard(2, "second"),
            }
        )
        self.addon.mw.col = collection
        self.addon.MANUAL_WARM_BATCH_SIZE = 1
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()
        self._run_next_timer()

        self.assertEqual(collection.get_card_ids, [1])
        self.assertEqual(prefetcher.enqueued, [("first", _DEFAULT_CONFIG)])
        self.assertEqual(len(self.hooks.timers), 1)
        self.assertEqual(
            self.hooks.timers[0][0],
            self.addon.MANUAL_WARM_BATCH_DELAY_MS,
        )

        self._run_next_timer()

        self.assertEqual(collection.get_card_ids, [1, 2])
        self.assertEqual(
            prefetcher.enqueued,
            [("first", _DEFAULT_CONFIG), ("second", _DEFAULT_CONFIG)],
        )
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queued 2 cards for TTS audio caching",
        )

    def test_warm_all_audio_cache_reports_progress_before_final_queue_count(self):
        collection = _FakeCollection(
            {
                1: _FakeDueCard(1, "first"),
                2: _FakeDueCard(2, "second"),
                3: _FakeDueCard(3, "third"),
            }
        )
        self.addon.mw.col = collection
        self.addon.MANUAL_WARM_BATCH_SIZE = 1
        self.addon.WARM_PROGRESS_INTERVAL_CARDS = 2
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.warm_all_audio_cache()
        self._run_next_timer()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queueing TTS audio cache...",
        )

        self._run_next_timer()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queueing TTS audio cache: 2/3 cards checked; 2 queued",
        )

        self._run_next_timer()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Queued 3 cards for TTS audio caching",
        )

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

    def test_warm_all_audio_cache_marks_cached_keys_succeeded_without_queueing(self):
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

            self.assertEqual(state.entry(key)["status"], "succeeded")
            self.assertEqual(prefetcher.enqueued, [])
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Queued 0 cards for TTS audio caching",
            )

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
            self.addon._audio_cache_state = state

            self.addon._on_audio_prefetch_result(
                "succeeded", {"speed": 1.5}, True, None
            )
            self.addon._on_audio_prefetch_result(
                "failed", {"speed": 1.5}, False, "network failed"
            )

            success = state.entry(self._cache_key("succeeded"))
            failure = state.entry(self._cache_key("failed"))
            self.assertEqual(success["status"], "succeeded")
            self.assertEqual(failure["status"], "failed")
            self.assertEqual(failure["last_error"], "network failed")

    def test_prefetch_result_callback_ignores_canceled_prefetch(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._new_cache_state(root / "state.json")
            key = self._cache_key("canceled")
            state.mark_pending(key, "canceled")
            self.addon._audio_cache_state = state

            self.addon._on_audio_prefetch_result(
                "canceled", {"speed": 1.5}, None, None
            )

            entry = state.entry(key)
            self.assertEqual(entry["status"], "pending")
            self.assertIsNone(entry["last_error"])

    def test_audio_cache_status_includes_pending_and_failed_counts(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            state = self._new_cache_state(root / "state.json")
            state.mark_pending(self._cache_key("pending"), "pending")
            state.mark_failed(self._cache_key("failed"), "network failed")
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = state
            self.addon.mw.col = _FakeCollection(
                {
                    1: _FakeDueCard(1, "pending"),
                    2: _FakeDueCard(2, "failed"),
                    3: _FakeDueCard(3, "uncounted missing"),
                }
            )

            self.addon.show_audio_cache_status()
            self._run_all_timers()

            self.assertEqual(
                self.hooks.tooltips[-1],
                (
                    "Audio cache incomplete: 0/3 cards cached; 3 missing; "
                    "1 pending; 1 failed; worker idle"
                ),
            )

    def test_audio_cache_status_reports_complete_cache_for_speakable_cards(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self.addon.AudioCache(Path(temp_dir))
            self._write_cached_audio(cache, "speakable")
            self.addon._audio_cache = cache
            self.addon.mw.col = _FakeCollection(
                {
                    1: _FakeDueCard(1, "speakable"),
                    2: _FakeDueCard(2, "", question_html=""),
                }
            )

            self.addon.show_audio_cache_status()
            self._run_all_timers()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache complete: 1/1 cards cached; worker idle",
        )

    def test_audio_cache_status_reports_missing_and_running_worker(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self.addon.AudioCache(Path(temp_dir))
            self._write_cached_audio(cache, "cached")
            self.addon._audio_cache = cache
            self.addon._prefetcher = _FakePrefetcher(
                status_result={"running": True, "active": 1, "pending": 1}
            )
            self.addon.mw.col = _FakeCollection(
                {
                    1: _FakeDueCard(1, "cached"),
                    2: _FakeDueCard(2, "missing"),
                }
            )

            self.addon.show_audio_cache_status()
            self._run_all_timers()

        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache incomplete: 1/2 cards cached; 1 missing; worker running",
        )

    def test_audio_cache_status_defers_collection_scan_until_timer_fires(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = self._new_cache_state(
                root / "state.json"
            )
            collection = _FakeCollection({1: _FakeDueCard(1, "speakable")})
            self.addon.mw.col = collection

            self.addon.show_audio_cache_status()

            self.assertEqual(collection.find_queries, [])
            self.assertEqual(collection.get_card_ids, [])
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Checking TTS audio cache status...",
            )
            self.assertEqual(len(self.hooks.timers), 1)

            self._run_all_timers()

            self.assertEqual(collection.find_queries, [""])
            self.assertEqual(collection.get_card_ids, [1])
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Audio cache incomplete: 0/1 cards cached; 1 missing; worker idle",
            )

    def test_audio_cache_status_batches_card_rendering_across_timer_ticks(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = self._new_cache_state(
                root / "state.json"
            )
            collection = _FakeCollection(
                {
                    1: _FakeDueCard(1, "first"),
                    2: _FakeDueCard(2, "second"),
                }
            )
            self.addon.mw.col = collection
            self.addon.STATUS_BATCH_SIZE = 1

            self.addon.show_audio_cache_status()
            self._run_next_timer()

            self.assertEqual(collection.get_card_ids, [1])
            self.assertEqual(len(self.hooks.timers), 1)
            self.assertEqual(
                self.hooks.timers[0][0],
                self.addon.STATUS_BATCH_DELAY_MS,
            )
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Checking TTS audio cache status...",
            )

            self._run_next_timer()

            self.assertEqual(collection.get_card_ids, [1, 2])
            self.assertEqual(
                self.hooks.tooltips[-1],
                "Audio cache incomplete: 0/2 cards cached; 2 missing; worker idle",
            )

    def test_audio_cache_status_reports_progress_before_final_status(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            self.addon._audio_cache = self.addon.AudioCache(root / "cache")
            self.addon._audio_cache_state = self._new_cache_state(
                root / "state.json"
            )
            collection = _FakeCollection(
                {
                    1: _FakeDueCard(1, "first"),
                    2: _FakeDueCard(2, "second"),
                    3: _FakeDueCard(3, "third"),
                }
            )
            self.addon.mw.col = collection
            self.addon.STATUS_BATCH_SIZE = 1
            self.addon.STATUS_PROGRESS_INTERVAL_CARDS = 2

            self.addon.show_audio_cache_status()
            self._run_next_timer()

            self.assertEqual(
                self.hooks.tooltips[-1],
                "Checking TTS audio cache status...",
            )

            self._run_next_timer()

            self.assertEqual(
                self.hooks.tooltips[-1],
                (
                    "Checking TTS audio cache: 2/3 cards checked; "
                    "0/2 speakable cached"
                ),
            )

            self._run_next_timer()

            self.assertEqual(
                self.hooks.tooltips[-1],
                "Audio cache incomplete: 0/3 cards cached; 3 missing; worker idle",
            )

    def test_warm_all_audio_cache_reports_final_status_when_prefetcher_becomes_idle(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self.addon.AudioCache(Path(temp_dir))
            self.addon._audio_cache = cache
            self.addon.mw.col = _FakeCollection(
                {1: _FakeDueCard(1, "speakable")}
            )
            prefetcher = _FakePrefetcher()
            self.addon._engine = _FakeEngine()
            self.addon._prefetcher = None
            self.addon.AudioPrefetcher = lambda _engine: prefetcher

            self.addon.warm_all_audio_cache()
            self._run_all_timers()
            self._write_cached_audio(cache, "speakable")
            prefetcher.idle_callback()
            self._run_all_timers()

        self.assertEqual(
            self.hooks.tooltips[-3],
            "Queued 1 cards for TTS audio caching",
        )
        self.assertEqual(
            self.hooks.tooltips[-2],
            "Checking TTS audio cache status...",
        )
        self.assertEqual(
            self.hooks.tooltips[-1],
            "Audio cache complete: 1/1 cards cached; worker idle",
        )

    def test_warm_all_audio_cache_shows_queued_before_fast_idle_status(self):
        class FastIdlePrefetcher(_FakePrefetcher):
            def __init__(self, cache_writer):
                super().__init__()
                self.cache_writer = cache_writer

            def enqueue(self, text, config):
                accepted = super().enqueue(text, config)
                if accepted:
                    self.cache_writer(text)
                    self.idle_callback()
                return accepted

        with tempfile.TemporaryDirectory() as temp_dir:
            cache = self.addon.AudioCache(Path(temp_dir))
            self.addon._audio_cache = cache
            self.addon.mw.col = _FakeCollection(
                {1: _FakeDueCard(1, "speakable")}
            )
            prefetcher = FastIdlePrefetcher(
                lambda text: self._write_cached_audio(cache, text)
            )
            self.addon._engine = _FakeEngine()
            self.addon._prefetcher = None
            self.addon.AudioPrefetcher = lambda _engine: prefetcher

            self.addon.warm_all_audio_cache()
            self._run_all_timers()

        self.assertEqual(
            self.hooks.tooltips[-3],
            "Queued 1 cards for TTS audio caching",
        )
        self.assertEqual(
            self.hooks.tooltips[-2],
            "Checking TTS audio cache status...",
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
        self.assertEqual(len(self.hooks.timers), 1)
        self.assertEqual(
            self.hooks.timers[0][0],
            self.addon.AUTO_WARM_START_DELAY_MS,
        )

        self.hooks.timers.pop(0)[1]()

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

        self.hooks.timers.pop(0)[1]()

        self.assertEqual(collection.find_queries, [""])
        self.assertEqual(prefetcher.enqueued, [("speakable", _DEFAULT_CONFIG)])
        self.assertEqual(self.hooks.tooltips, [])

    def test_auto_warm_batches_card_rendering_across_timer_ticks(self):
        collection = _FakeCollection(
            {
                1: _FakeDueCard(1, "first"),
                2: _FakeDueCard(2, "second"),
            }
        )
        self.addon.mw.col = collection
        self.addon.AUTO_WARM_BATCH_SIZE = 1
        prefetcher = _FakePrefetcher()
        self.addon._engine = _FakeEngine()
        self.addon._prefetcher = None
        self.addon.AudioPrefetcher = lambda _engine: prefetcher

        self.addon.on_sync_did_finish()
        self.hooks.timers.pop(0)[1]()

        self.assertEqual(collection.get_card_ids, [1])
        self.assertEqual(prefetcher.enqueued, [("first", _DEFAULT_CONFIG)])
        self.assertEqual(len(self.hooks.timers), 1)
        self.assertEqual(
            self.hooks.timers[0][0],
            self.addon.AUTO_WARM_BATCH_DELAY_MS,
        )

        self.hooks.timers.pop(0)[1]()

        self.assertEqual(collection.get_card_ids, [1, 2])
        self.assertEqual(
            prefetcher.enqueued,
            [("first", _DEFAULT_CONFIG), ("second", _DEFAULT_CONFIG)],
        )

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
            {"pending": 0, "succeeded": 0, "failed": 0},
        )

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
