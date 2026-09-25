import importlib
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fake_aqt import _AQT_MODULES, _MISSING, _install_fake_aqt  # noqa: E402


def _purge_addon_modules():
    for name in list(sys.modules):
        if name == "anki_tts_addon" or name.startswith("anki_tts_addon."):
            del sys.modules[name]


LINKS = [
    '<a id="decks">Decks</a>',
    '<a id="add">Add</a>',
    '<a id="browse">Browse</a>',
    '<a id="stats">Stats</a>',
    '<a class=hitem id="sync">Sync</a>',
]


class _Provider:
    def __init__(self, model):
        self.model = model
        self.snap = model.MonitorSnapshot()
        self.calls = 0

    def __call__(self):
        self.calls += 1
        return self.snap


class MonitorUiTestCase(unittest.TestCase):
    def setUp(self):
        self._saved = {name: sys.modules.get(name, _MISSING) for name in _AQT_MODULES}
        _purge_addon_modules()
        self.hooks = _install_fake_aqt()
        self.addon = importlib.import_module("anki_tts_addon")
        self._user_files = tempfile.TemporaryDirectory()
        self.addCleanup(self._user_files.cleanup)
        user_files = Path(self._user_files.name)
        self.addon._addon_user_files_dir = lambda: user_files
        self.ui = sys.modules["anki_tts_addon.monitor_ui"]
        self.model = sys.modules["anki_tts_addon.monitor_model"]
        self.mw = self.addon.mw
        self.web = self.mw.toolbar.web

    def tearDown(self):
        try:
            self.ui.teardown()
        except Exception:
            pass
        sys.modules["anki_tts_addon.addon_log"].remove("anki_tts_addon")
        _purge_addon_modules()
        for name in _AQT_MODULES:
            sys.modules.pop(name, None)
            saved = self._saved[name]
            if saved is not _MISSING:
                sys.modules[name] = saved

    def use_provider(self, actions=None):
        provider = _Provider(self.model)
        self.ui.configure(provider, actions or {})
        return provider


class ToolbarTests(MonitorUiTestCase):
    def test_hooks_registered_once_at_import(self):
        self.assertEqual(
            self.hooks.top_toolbar_did_init_links,
            [self.ui.on_top_toolbar_did_init_links],
        )
        self.ui.register_toolbar_hooks()
        self.assertEqual(len(self.hooks.top_toolbar_did_init_links), 1)
        self.assertEqual(len(self.hooks.top_toolbar_did_redraw), 1)

    def test_link_inserted_right_after_sync(self):
        links = list(LINKS)
        toolbar = self.mw.toolbar
        self.ui.on_top_toolbar_did_init_links(links, toolbar)
        self.assertEqual(len(links), 6)
        self.assertIn('id="sync"', links[4])
        self.assertIn('id="anki-tts-monitor"', links[5])
        self.assertIs(
            toolbar.link_handlers[self.ui.TOOLBAR_CMD], self.ui.toggle_dock
        )

    def test_link_appended_when_sync_missing(self):
        links = LINKS[:3]
        self.ui.on_top_toolbar_did_init_links(links, self.mw.toolbar)
        self.assertIn('id="anki-tts-monitor"', links[-1])

    def test_broken_provider_never_breaks_the_toolbar(self):
        def boom():
            raise RuntimeError("boom")

        self.ui.configure(boom, {})
        links = list(LINKS)
        with self.assertLogs(self.ui.log, level="WARNING"):
            self.ui.on_top_toolbar_did_init_links(links, self.mw.toolbar)
        self.assertEqual(links, LINKS)

    def test_tick_pushes_only_on_change(self):
        provider = self.use_provider()
        self.ui.setup()
        self.assertEqual(len(self.web.evals), 1)
        self.ui._tick()
        self.ui._tick()
        self.assertEqual(len(self.web.evals), 1)
        provider.snap = self.model.MonitorSnapshot(total=10, cached=3, pending=7)
        self.ui._tick()
        self.assertEqual(len(self.web.evals), 2)
        self.assertIn('"warming"', self.web.evals[-1])

    def test_redraw_forces_repush(self):
        self.use_provider()
        self.ui.setup()
        self.ui._tick()
        self.assertEqual(len(self.web.evals), 1)
        self.ui.on_top_toolbar_did_redraw(self.mw.toolbar)
        self.ui._tick()
        self.assertEqual(len(self.web.evals), 2)

    def test_no_eval_without_toolbar_web(self):
        self.use_provider()
        self.mw.toolbar = None
        self.ui.setup()
        self.ui._tick()  # must not raise

    def test_no_eval_after_teardown_and_timer_stops(self):
        provider = self.use_provider()
        self.ui.setup()
        timer = self.ui._timer
        self.assertTrue(timer.isActive())
        self.ui.teardown()
        self.assertFalse(timer.isActive())
        provider.snap = self.model.MonitorSnapshot(pending=3)
        self.ui._tick()
        self.ui.push_toolbar_state(provider.snap)
        self.assertEqual(len(self.web.evals), 1)

    def test_setup_draws_toolbar_when_icon_missing(self):
        self.use_provider()
        self.ui.setup()
        self.assertEqual(self.mw.toolbar.draw_count, 1)
        self.ui.teardown()
        self.ui.on_top_toolbar_did_init_links(list(LINKS), self.mw.toolbar)
        self.ui.setup()
        self.assertEqual(self.mw.toolbar.draw_count, 1)

    def test_timer_interval_follows_activity(self):
        provider = self.use_provider()
        self.ui.setup()
        self.assertEqual(self.ui._timer.interval(), self.ui.SLOW_TICK_MS)
        provider.snap = self.model.MonitorSnapshot(pending=4)
        self.ui._tick()
        self.assertEqual(self.ui._timer.interval(), self.ui.FAST_TICK_MS)
        provider.snap = self.model.MonitorSnapshot()
        self.ui._tick()
        self.assertEqual(self.ui._timer.interval(), self.ui.SLOW_TICK_MS)
        self.ui.show_dock()
        self.ui._tick()
        self.assertEqual(self.ui._timer.interval(), self.ui.FAST_TICK_MS)


class DockTests(MonitorUiTestCase):
    def test_dock_created_once_and_toggles(self):
        self.use_provider()
        self.ui.toggle_dock()
        panel = self.ui._panel
        self.assertTrue(panel.dock.isVisible())
        self.assertEqual(len(self.hooks.docks), 1)
        self.assertEqual(self.hooks.docks[0][0], 2)  # RightDockWidgetArea
        self.assertEqual(panel.dock.values["setObjectName"], "AnkiTTSMonitorDock")
        self.ui.toggle_dock()
        self.assertFalse(panel.dock.isVisible())
        self.ui.toggle_dock()
        self.assertIs(self.ui._panel, panel)
        self.assertEqual(len(self.hooks.docks), 1)

    def test_teardown_hides_dock(self):
        self.use_provider()
        self.ui.setup()
        self.ui.show_dock()
        self.ui.teardown()
        self.assertFalse(self.ui._panel.dock.isVisible())

    def test_render_fills_labels(self):
        provider = self.use_provider()
        M = self.model
        provider.snap = M.MonitorSnapshot(
            total=28912, cached=4780, pending=120, active=1, failed=3,
            current_preview="Ruxolitinib", current_elapsed_s=3.2,
            rate_per_min=312, eta_seconds=4800, cache_bytes=159 * 1024 * 1024,
            cache_limit_bytes=2 * 1024 ** 3, edge_ok=True, paused=True,
            recent_errors=(M.ErrorEntry(at=0.0, preview="card", error="request timed out"),),
        )
        self.ui.show_dock()
        panel = self.ui._panel
        self.assertEqual(panel.progress.format(), "4,780 / 28,912 (16.5%)")
        self.assertEqual(panel.progress.value(), 165)
        self.assertEqual(panel.labels["queue"].text(), "120 pending, 1 active")
        self.assertIn("Ruxolitinib", panel.labels["current"].text())
        self.assertIn("3.2s", panel.labels["current"].text())
        self.assertEqual(panel.labels["rate"].text(), "312/min")
        self.assertEqual(panel.labels["eta"].text(), "1h 20m")
        self.assertEqual(panel.labels["disk"].text(), "159 MB of 2.0 GB")
        self.assertEqual(panel.labels["edge"].text(), "OK")
        self.assertEqual(panel.buttons["toggle_pause"].text(), "Resume")
        self.assertEqual(panel.errors.count(), 1)
        self.assertIn("request timed out", panel.errors.items[0])

    def test_render_skips_unchanged_snapshot(self):
        provider = self.use_provider()
        self.ui.show_dock()
        panel = self.ui._panel
        panel.labels["rate"].setText("sentinel")
        panel.render(provider.snap)
        self.assertEqual(panel.labels["rate"].text(), "sentinel")
        panel.render(provider.snap, force=True)
        self.assertEqual(panel.labels["rate"].text(), "-")

    def test_buttons_call_actions_and_refresh(self):
        calls = []
        actions = {
            name: (lambda n=name: calls.append(n))
            for name in self.ui.ACTION_NAMES
        }
        provider = self.use_provider(actions)
        self.ui.setup()
        self.ui.show_dock()
        before = provider.calls
        for name, button in self.ui._panel.buttons.items():
            for callback in button.clicked.callbacks:
                callback(False)
        self.assertEqual(sorted(calls), sorted(self.ui.ACTION_NAMES))
        self.assertGreater(provider.calls, before)

    def test_failing_action_is_logged_not_raised(self):
        def boom():
            raise RuntimeError("nope")

        self.use_provider({"warm_all": boom})
        self.ui.show_dock()
        with self.assertLogs(self.ui.log, level="WARNING"):
            for callback in self.ui._panel.buttons["warm_all"].clicked.callbacks:
                callback()


class AddonWiringTests(MonitorUiTestCase):
    def test_profile_open_starts_monitor_and_close_stops_it(self):
        self.addon._menu_added = True
        self.addon._hooks_registered = False
        self.addon._on_profile_did_open()
        self.assertTrue(self.ui._alive)
        self.assertTrue(self.ui._timer.isActive())
        self.assertGreaterEqual(len(self.web.evals), 1)
        self.addon._on_profile_will_close()
        self.assertFalse(self.ui._alive)
        self.assertFalse(self.ui._timer.isActive())

    def test_monitor_snapshot_reads_no_config(self):
        calls = []
        manager = self.mw.addonManager
        original = manager.getConfig
        manager.getConfig = lambda name: calls.append(name) or original(name)
        snap = self.addon.monitor_snapshot()
        self.assertEqual(calls, [])
        self.assertIsNone(snap.cache_bytes)
        self.assertEqual(
            snap.cache_limit_bytes, self.addon.DEFAULT_CACHE_MAX_MB * 1024 * 1024
        )

    def test_worker_results_do_not_post_to_main_thread(self):
        posted = []
        self.mw.taskman.run_on_main = lambda fn: posted.append(fn)
        for i in range(100):
            self.addon._on_audio_prefetch_result(
                f"text {i}", {"speed": 1.5}, i % 10 != 0, "request timed out"
            )
        self.assertEqual(posted, [])
        data = self.addon.stats().snapshot()
        self.assertEqual((data["completed"], data["failed"]), (90, 10))
        self.assertEqual(data["recent_errors"][0].error, "request timed out")

    def test_toggle_pause_pauses_worker_and_new_workers(self):
        class PausablePrefetcher:
            def __init__(self):
                self.paused = False

            def pause(self):
                self.paused = True

            def resume(self):
                self.paused = False

            def is_paused(self):
                return self.paused

        worker = PausablePrefetcher()
        self.addon._prefetcher = worker
        self.addon.toggle_pause_audio_cache()
        self.assertTrue(worker.paused)
        self.assertTrue(self.addon.monitor_snapshot().paused)
        self.addon.toggle_pause_audio_cache()
        self.assertFalse(worker.paused)
        self.assertFalse(self.addon.monitor_snapshot().paused)

    def test_retry_failed_resets_backoff_and_starts_warm_job(self):
        started = []
        self.addon._start_warm_job = lambda **kw: started.append(kw)
        state = self.addon.audio_cache_state()
        key = self.addon._cache_key_for_text("hello", {"speed": 1.5})
        state.mark_failed(key, "request timed out", "hello")
        self.assertFalse(state.can_retry(key))

        self.addon.retry_failed_audio_cache()

        self.assertTrue(state.can_retry(key))
        self.assertEqual(started, [{"full": False, "manual": True}])

    def test_retry_failed_with_nothing_failed_only_tells_user(self):
        started = []
        self.addon._start_warm_job = lambda **kw: started.append(kw)
        self.addon.retry_failed_audio_cache()
        self.assertEqual(started, [])
        self.assertEqual(self.hooks.tooltips[-1], "No failed TTS audio to retry")

    def test_show_monitor_measures_cache_in_background_once(self):
        self.addon.show_monitor()
        self.addon.show_monitor()
        measure_calls = [
            c for c in self.hooks.background_calls
            if getattr(c["task"], "__name__", "") == "total_bytes"
        ]
        self.assertEqual(len(measure_calls), 1)
        self.assertEqual(measure_calls[0]["kwargs"], {"uses_collection": False})
        while self.hooks.timers:
            self.hooks.timers.pop(0)[1]()
        self.assertEqual(self.addon.monitor_snapshot().cache_bytes, 0)

    def test_open_cache_folder_uses_open_folder(self):
        self.addon.open_audio_cache_folder()
        self.assertEqual(
            self.hooks.opened_folders,
            [str(Path(self._user_files.name) / "audio_cache")],
        )

    def test_addon_works_without_toolbar_hooks(self):
        # Older Anki: no toolbar hooks and no toolbar web view.
        del self.hooks.top_toolbar_did_init_links
        del self.hooks.top_toolbar_did_redraw
        self.mw.toolbar = types.SimpleNamespace()
        _purge_addon_modules()
        addon = importlib.import_module("anki_tts_addon")
        addon._addon_user_files_dir = lambda: Path(self._user_files.name)
        addon._menu_added = True
        addon._hooks_registered = False
        addon._on_profile_did_open()
        addon.show_monitor()
        addon._on_profile_will_close()


if __name__ == "__main__":
    unittest.main()
