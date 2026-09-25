import json
import re
import sys
import threading
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module


M = load_addon_module("monitor_model")
IconState = M.IconState
MonitorSnapshot = M.MonitorSnapshot


class FakeClock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t


def _status(total=100, cached=40, pending=0, active=0, failed=0,
            edge=None, current=None, started=None, stale=False,
            running=None, paused=False, sync=False):
    return {
        "cache": {"total": total, "cached": cached, "stale": stale,
                  "scanned_at": 500.0},
        "worker": {"pending": pending, "active": active, "paused": paused,
                   "current": current, "current_started_at": started},
        "state": {"pending": 0, "failed": failed},
        "scan": {"running": running, "sync_in_progress": sync, "last": {}},
        "engine": {"edge": edge or {}, "live": {"hits": 3, "misses": 1}},
    }


def snap(**kwargs):
    return MonitorSnapshot(**kwargs)


class WarmStatsTests(unittest.TestCase):
    def test_defaults(self):
        stats = M.WarmStats()
        data = stats.snapshot()
        self.assertEqual(data["completed"], 0)
        self.assertEqual(data["failed"], 0)
        self.assertIsNone(data["rate_per_min"])
        self.assertEqual(data["recent_errors"], ())
        self.assertIsNone(data["scan_kind"])

    def test_scan_lifecycle(self):
        stats = M.WarmStats()
        stats.begin_scan("full")
        stats.scan_progress(0, 30120)
        stats.scan_progress(3400)
        data = stats.snapshot()
        self.assertEqual(
            (data["scan_kind"], data["scan_checked"], data["scan_total"]),
            ("full", 3400, 30120),
        )
        stats.end_scan()
        self.assertIsNone(stats.snapshot()["scan_kind"])
        # Progress after the scan ended is ignored.
        stats.scan_progress(10)
        self.assertEqual(stats.snapshot()["scan_checked"], 0)

    def test_results_and_cancelled_items(self):
        stats = M.WarmStats()
        stats.on_result(True)
        stats.on_result(False, "request timed out", "Ruxolitinib is used")
        stats.on_result(None, None, "cancelled")
        data = stats.snapshot()
        self.assertEqual((data["completed"], data["failed"]), (1, 1))
        self.assertEqual(len(data["recent_errors"]), 1)
        entry = data["recent_errors"][0]
        self.assertEqual(entry.error, "request timed out")
        self.assertEqual(entry.preview, "Ruxolitinib is used")

    def test_version_bumps_only_on_changes(self):
        stats = M.WarmStats()
        v0 = stats.snapshot()["version"]
        stats.on_result(None)
        stats.end_scan()
        stats.clear_errors()
        self.assertEqual(stats.snapshot()["version"], v0)
        stats.on_result(True)
        self.assertGreater(stats.snapshot()["version"], v0)

    def test_error_ring_is_capped_newest_first_and_truncated(self):
        stats = M.WarmStats()
        for i in range(60):
            stats.on_result(False, f"err {i} " + "x" * 400, "p" * 200)
        errors = stats.snapshot()["recent_errors"]
        self.assertEqual(len(errors), M.MAX_ERRORS)
        self.assertTrue(errors[0].error.startswith("err 59"))
        self.assertLessEqual(len(errors[0].error), M.MAX_ERROR_CHARS)
        self.assertLessEqual(len(errors[0].preview), M.MAX_PREVIEW_CHARS)

    def test_rate_needs_min_samples_and_uses_window(self):
        clock = FakeClock()
        stats = M.WarmStats(now=clock, window_s=120)
        for _ in range(4):
            clock.t += 1
            stats.on_result(True)
        self.assertIsNone(stats.rate_per_min())
        for _ in range(6):
            clock.t += 1
            stats.on_result(True)
        # 10 results over ~9 s.
        self.assertAlmostEqual(stats.rate_per_min(), 10 * 60 / 9, places=3)
        clock.t += 500  # everything falls out of the window
        self.assertIsNone(stats.rate_per_min())

    def test_thread_stress_counts_exact(self):
        stats = M.WarmStats()

        def work():
            for i in range(1000):
                stats.on_result(i % 2 == 0, "e")

        threads = [threading.Thread(target=work) for _ in range(8)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        data = stats.snapshot()
        self.assertEqual(data["completed"], 4000)
        self.assertEqual(data["failed"], 4000)


class BuildSnapshotTests(unittest.TestCase):
    def test_builds_from_status_and_stats(self):
        stats = {"completed": 10, "failed": 2, "rate_per_min": 120.0,
                 "recent_errors": (), "scan_kind": None, "scan_checked": 0,
                 "scan_total": None}
        s = M.build_snapshot(
            _status(pending=58, active=2, failed=7, current="Hello world",
                    started=90.0, paused=True,
                    edge={"open": True, "retry_in": 30, "last_error": "net"}),
            stats, cache_bytes=5, cache_limit_bytes=100,
            now_mono=93.5, now_wall=2000.0,
        )
        self.assertEqual((s.total, s.cached, s.pending, s.active), (100, 40, 58, 2))
        self.assertEqual(s.failed, 7)
        self.assertAlmostEqual(s.eta_seconds, 30.0)
        self.assertAlmostEqual(s.current_elapsed_s, 3.5)
        self.assertIs(s.edge_ok, False)
        self.assertEqual(s.edge_error, "net")
        self.assertEqual(s.edge_retry_in, 30)
        self.assertEqual((s.live_hits, s.live_misses), (3, 1))
        self.assertTrue(s.edge_waiting)  # worker paused but not by the user
        self.assertFalse(s.paused)

    def test_tolerates_empty_input(self):
        s = M.build_snapshot({}, {})
        self.assertIsNone(s.total)
        self.assertIsNone(s.edge_ok)
        self.assertEqual(M.derive_icon_state(s), IconState.IDLE)

    def test_user_pause_is_not_edge_wait(self):
        s = M.build_snapshot(_status(paused=True), {}, user_paused=True)
        self.assertTrue(s.paused)
        self.assertFalse(s.edge_waiting)


class DeriveIconStateTests(unittest.TestCase):
    def test_precedence(self):
        cases = [
            (snap(enabled=False, paused=True, pending=5), IconState.DISABLED),
            (snap(prefetch_enabled=False), IconState.DISABLED),
            (snap(edge_ok=False, pending=5, paused=True), IconState.ERROR),
            (snap(edge_ok=False, pending=5, completed_session=1), IconState.WARMING),
            (snap(paused=True, scanning="full", pending=3), IconState.PAUSED),
            (snap(scanning="full", pending=3), IconState.SCANNING),
            (snap(pending=3, failed=2), IconState.WARMING),
            (snap(failed=2, total=10, cached=10), IconState.WARNING),
            (snap(edge_ok=False), IconState.WARNING),
            (snap(total=10, cached=10), IconState.COMPLETE),
            (snap(total=10, cached=9), IconState.IDLE),
            (snap(), IconState.IDLE),
        ]
        for s, expected in cases:
            with self.subTest(expected=expected, snap=s):
                self.assertEqual(M.derive_icon_state(s), expected)


class FormattingTests(unittest.TestCase):
    def test_format_bytes(self):
        self.assertEqual(M.format_bytes(None), "-")
        self.assertEqual(M.format_bytes(512), "512 B")
        self.assertEqual(M.format_bytes(159 * 1024 * 1024), "159 MB")
        self.assertEqual(M.format_bytes(2 * 1024 ** 3), "2.0 GB")

    def test_format_eta(self):
        self.assertEqual(M.format_eta(None), "-")
        self.assertEqual(M.format_eta(30), "<1m")
        self.assertEqual(M.format_eta(125), "2m")
        self.assertEqual(M.format_eta(4800), "1h 20m")
        self.assertEqual(M.format_eta(3600), "1h")
        self.assertEqual(M.format_eta(90000), "1d 1h")

    def test_format_rate(self):
        self.assertEqual(M.format_rate(None), "-")
        self.assertEqual(M.format_rate(3.25), "3.2/min")
        self.assertEqual(M.format_rate(312.4), "312/min")

    def test_progress_fraction_and_text(self):
        self.assertIsNone(M.progress_fraction(snap()))
        s = snap(total=28912, cached=4780)
        self.assertAlmostEqual(M.progress_fraction(s), 4780 / 28912)
        self.assertEqual(M.progress_text(s), "4,780 / 28,912 (16.5%)")
        self.assertEqual(M.progress_text(snap()), "No full scan yet")

    def test_tooltip_text(self):
        s = snap(total=28912, cached=4780, pending=100, rate_per_min=312,
                 eta_seconds=4800)
        self.assertEqual(
            M.tooltip_text(s),
            "TTS cache 4,780/28,912 (16%) - 100 queued - 312/min - ETA 1h 20m",
        )
        self.assertIn("paused", M.tooltip_text(snap(paused=True)))
        self.assertIn("disabled", M.tooltip_text(snap(enabled=False)))
        self.assertIn(
            "scanning 3,400/30,120 cards",
            M.tooltip_text(snap(scanning="full", scan_checked=3400,
                                scan_total_cards=30120)),
        )

    def test_edge_text(self):
        self.assertEqual(M.edge_text(snap()), "Not used yet")
        self.assertEqual(M.edge_text(snap(edge_ok=True)), "OK")
        self.assertEqual(
            M.edge_text(snap(edge_ok=False, edge_error="request timed out",
                             edge_retry_in=300)),
            "Unavailable: request timed out (retry in 5m)",
        )


class ToolbarTests(unittest.TestCase):
    def test_signature_is_stable_and_changes_with_state(self):
        a = snap(total=10, cached=4, pending=6)
        b = snap(total=10, cached=4, pending=6, taken_at=99.0)
        self.assertEqual(M.toolbar_signature(a), M.toolbar_signature(b))
        c = snap(total=10, cached=5, pending=5)
        self.assertNotEqual(M.toolbar_signature(a), M.toolbar_signature(c))

    def test_link_html_is_own_anchor(self):
        html = M.toolbar_link_html(
            "anki_tts_monitor", "anki-tts-monitor",
            snap(total=10, cached=4, pending=6),
        )
        self.assertIn('id="anki-tts-monitor"', html)
        self.assertIn('class="hitem"', html)
        self.assertIn("pycmd('anki_tts_monitor')", html)
        self.assertIn('data-state="warming"', html)
        self.assertIn("tts-ring tts-spin", html)
        self.assertIn(">40%<", html)
        # aria-label must be plain text, never HTML.
        label = re.search(r'aria-label="([^"]*)"', html).group(1)
        self.assertNotIn("<", label)

    def test_update_js_escapes_strings(self):
        s = snap(total=3, cached=1, pending=1)
        js = M.toolbar_update_js('x"</script>\n', s)
        self.assertNotIn("</script>", js)
        self.assertNotIn("\n", js)
        literal = re.search(r"getElementById\((.*?)\);if", js).group(1)
        self.assertEqual(json.loads(literal.replace("<\\/", "</")),
                         'x"</script>\n')
        self.assertIn("classList.toggle('tts-spin',true)", js)
        idle_js = M.toolbar_update_js("i", snap())
        self.assertIn("classList.toggle('tts-spin',false)", idle_js)


if __name__ == "__main__":
    unittest.main()
