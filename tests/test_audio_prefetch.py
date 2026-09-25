import sys
import inspect
import threading
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from module_loader import load_addon_module


_MODULE = load_addon_module("audio_prefetch")
AudioPrefetcher = _MODULE.AudioPrefetcher


class FakeEngine:
    def __init__(self):
        self.calls = []

    def prefetch(self, text, config):
        self.calls.append((text, dict(config)))


class FailingOnceEngine(FakeEngine):
    def prefetch(self, text, config):
        self.calls.append((text, dict(config)))
        if len(self.calls) == 1:
            raise RuntimeError("first prefetch failed")


class BlockingEngine(FakeEngine):
    def __init__(self):
        super().__init__()
        self.entered = threading.Event()
        self.release = threading.Event()

    def prefetch(self, text, config):
        self.calls.append((text, dict(config)))
        self.entered.set()
        self.release.wait(timeout=10)


class MutatingEngine(FakeEngine):
    def prefetch(self, text, config):
        self.calls.append((text, dict(config)))
        config["speed"] = 2.0


class ReportingEngine(FakeEngine):
    def prefetch(self, text, config):
        self.calls.append((text, dict(config)))
        return text == "ok"


class CancelledEngine(FakeEngine):
    def prefetch(self, text, config):
        self.calls.append((text, dict(config)))
        return None


class PausedGetQueue:
    def __init__(self):
        self.item = None
        self.item_ready = threading.Event()
        self.item_taken = threading.Event()
        self.release_get = threading.Event()
        self.task_done_count = 0

    def put(self, item):
        self.item = item
        self.item_ready.set()

    def get(self):
        self.item_ready.wait(timeout=2)
        item = self.item
        self.item = None
        self.item_ready.clear()
        self.item_taken.set()
        self.release_get.wait(timeout=2)
        return item

    def get_nowait(self):
        raise _MODULE.queue.Empty

    def task_done(self):
        self.task_done_count += 1


class AudioPrefetcherTests(unittest.TestCase):
    def _line_number_containing(self, function, needle):
        lines, first_line = inspect.getsourcelines(function)
        for offset, line in enumerate(lines):
            if needle in line:
                return first_line + offset
        self.fail(f"Could not find line containing {needle!r}")

    def test_enqueue_dedupes_same_text_and_config(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)

        self.assertTrue(prefetcher.enqueue("hello", {"speed": 1.5}))
        self.assertFalse(prefetcher.enqueue("hello", {"speed": 1.5}))
        prefetcher.drain_for_tests()

        self.assertEqual(engine.calls, [("hello", {"speed": 1.5})])

    def test_different_speed_is_not_deduped(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)

        prefetcher.enqueue("hello", {"speed": 1.0})
        prefetcher.enqueue("hello", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(
            engine.calls,
            [("hello", {"speed": 1.0}), ("hello", {"speed": 1.5})],
        )

    def test_reenqueue_after_processing_succeeds(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)

        prefetcher.enqueue("hello", {"speed": 1.5})
        prefetcher.drain_for_tests()
        prefetcher.enqueue("hello", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(
            engine.calls,
            [("hello", {"speed": 1.5}), ("hello", {"speed": 1.5})],
        )

    def test_empty_text_is_ignored(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)

        self.assertFalse(prefetcher.enqueue("", {"speed": 1.5}))
        prefetcher.drain_for_tests()

        self.assertEqual(engine.calls, [])

    def test_start_and_stop_worker(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        deadline = time.monotonic() + 2

        prefetcher.start()
        prefetcher.enqueue("hello", {"speed": 1.5})
        while time.monotonic() < deadline and not engine.calls:
            time.sleep(0.01)
        prefetcher.stop()

        self.assertEqual(engine.calls, [("hello", {"speed": 1.5})])

    def test_worker_continues_after_prefetch_error(self):
        engine = FailingOnceEngine()
        prefetcher = AudioPrefetcher(engine)
        self.addCleanup(prefetcher.stop)
        deadline = time.monotonic() + 2

        with self.assertLogs(_MODULE.log, level="WARNING") as logs:
            prefetcher.start()
            prefetcher.enqueue("first", {"speed": 1.5})
            prefetcher.enqueue("second", {"speed": 1.5})
            while time.monotonic() < deadline and len(engine.calls) < 2:
                time.sleep(0.01)

        self.assertEqual(
            engine.calls,
            [("first", {"speed": 1.5}), ("second", {"speed": 1.5})],
        )
        self.assertIn("Audio prefetch failed", logs.output[0])
        self.assertIsNotNone(logs.records[0].exc_info)

    def test_stop_discards_pending_backlog(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)
        stop_done = threading.Event()

        try:
            prefetcher.start()
            prefetcher.enqueue("active", {"speed": 1.5})
            self.assertTrue(engine.entered.wait(timeout=2))
            prefetcher.enqueue("pending", {"speed": 1.5})

            stop_thread = threading.Thread(
                target=lambda: (prefetcher.stop(), stop_done.set())
            )
            stop_thread.start()
            self.assertFalse(stop_done.wait(timeout=0.15))
            engine.release.set()
            stop_thread.join(timeout=2)

            self.assertTrue(stop_done.is_set())
            self.assertEqual(engine.calls, [("active", {"speed": 1.5})])
        finally:
            engine.release.set()
            prefetcher.stop()

    def test_stop_with_timeout_returns_while_prefetch_is_active(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)

        try:
            prefetcher.start()
            prefetcher.enqueue("active", {"speed": 1.5})
            self.assertTrue(engine.entered.wait(timeout=2))

            started = time.monotonic()
            stopped = prefetcher.stop(timeout=0.05)
            elapsed = time.monotonic() - started

            self.assertFalse(stopped)
            self.assertLess(elapsed, 0.5)
            self.assertIsNotNone(prefetcher._thread)
            self.assertTrue(prefetcher._thread.is_alive())

            prefetcher.enqueue("ignored", {"speed": 1.5})
            self.assertEqual(engine.calls, [("active", {"speed": 1.5})])
        finally:
            engine.release.set()
            prefetcher.stop()

    def test_start_reports_not_accepting_while_timed_stop_is_pending(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)

        try:
            self.assertTrue(prefetcher.start())
            prefetcher.enqueue("active", {"speed": 1.5})
            self.assertTrue(engine.entered.wait(timeout=2))

            self.assertFalse(prefetcher.stop(timeout=0.05))

            self.assertFalse(prefetcher.start())
            prefetcher.enqueue("ignored", {"speed": 1.5})
            self.assertEqual(engine.calls, [("active", {"speed": 1.5})])
        finally:
            engine.release.set()
            prefetcher.stop()

    def test_start_recovers_after_timed_stop_worker_exits(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)

        try:
            self.assertTrue(prefetcher.start())
            prefetcher.enqueue("active", {"speed": 1.5})
            self.assertTrue(engine.entered.wait(timeout=2))

            self.assertFalse(prefetcher.stop(timeout=0.05))
            engine.release.set()
            deadline = time.monotonic() + 2
            while (
                time.monotonic() < deadline
                and prefetcher._thread is not None
                and prefetcher._thread.is_alive()
            ):
                time.sleep(0.01)

            self.assertTrue(prefetcher.start())
            self.assertTrue(prefetcher.enqueue("after", {"speed": 1.5}))
            prefetcher.drain_for_tests()

            self.assertEqual(
                engine.calls,
                [("active", {"speed": 1.5}), ("after", {"speed": 1.5})],
            )
        finally:
            engine.release.set()
            prefetcher.stop()

    def test_enqueue_does_not_revive_stopped_worker_without_start(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)

        try:
            self.assertTrue(prefetcher.start())
            prefetcher.enqueue("active", {"speed": 1.5})
            self.assertTrue(engine.entered.wait(timeout=2))

            self.assertFalse(prefetcher.stop(timeout=0.05))
            engine.release.set()
            deadline = time.monotonic() + 2
            while (
                time.monotonic() < deadline
                and prefetcher._thread is not None
                and prefetcher._thread.is_alive()
            ):
                time.sleep(0.01)

            # Without a live thread nothing would ever process the item.
            self.assertFalse(prefetcher.enqueue("after", {"speed": 1.5}))
            self.assertEqual(prefetcher.status()["pending"], 0)

            self.assertTrue(prefetcher.start())
            self.assertTrue(prefetcher.enqueue("after", {"speed": 1.5}))
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and len(engine.calls) < 2:
                time.sleep(0.01)

            self.assertEqual(
                engine.calls,
                [("active", {"speed": 1.5}), ("after", {"speed": 1.5})],
            )
        finally:
            engine.release.set()
            prefetcher.stop()

    def test_worker_discards_dequeued_item_when_stop_happens_before_processing(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        paused_queue = PausedGetQueue()
        prefetcher._queue = paused_queue

        prefetcher.start()
        prefetcher.enqueue("hello", {"speed": 1.5})
        self.assertTrue(paused_queue.item_taken.wait(timeout=2))

        self.assertFalse(prefetcher.stop(timeout=0.05))
        paused_queue.release_get.set()
        self.assertTrue(prefetcher.stop(timeout=2))
        prefetcher._queue = _MODULE.queue.Queue()
        prefetcher.start()
        self.assertTrue(prefetcher.enqueue("hello", {"speed": 1.5}))
        prefetcher.drain_for_tests()

        self.assertEqual(engine.calls, [("hello", {"speed": 1.5})])
        self.assertEqual(paused_queue.task_done_count, 2)

    def test_stop_after_accept_check_before_process_skips_prefetch(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        paused = threading.Event()
        release = threading.Event()
        process_line = self._line_number_containing(
            AudioPrefetcher._run,
            "self._process_if_accepting(text, config)",
        )

        def trace(frame, event, arg):
            if (
                event == "line"
                and frame.f_code is AudioPrefetcher._run.__code__
                and frame.f_lineno == process_line
            ):
                paused.set()
                release.wait(timeout=2)
            return trace

        previous_trace = getattr(threading, "gettrace", lambda: None)()
        threading.settrace(trace)
        try:
            prefetcher.start()
        finally:
            threading.settrace(previous_trace)

        try:
            self.assertTrue(prefetcher.enqueue("hello", {"speed": 1.5}))
            self.assertTrue(paused.wait(timeout=2))

            self.assertFalse(prefetcher.stop(timeout=0.05))
            release.set()
            self.assertTrue(prefetcher.stop(timeout=2))

            self.assertEqual(engine.calls, [])
        finally:
            release.set()
            prefetcher.stop(timeout=2)

    def test_successful_stop_clears_thread_reference_and_worker_is_dead(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        deadline = time.monotonic() + 2

        prefetcher.start()
        thread = prefetcher._thread
        prefetcher.enqueue("hello", {"speed": 1.5})
        while time.monotonic() < deadline and not engine.calls:
            time.sleep(0.01)
        prefetcher.stop()

        self.assertEqual(engine.calls, [("hello", {"speed": 1.5})])
        self.assertIsNone(prefetcher._thread)
        self.assertFalse(thread.is_alive())

    def test_enqueue_during_and_after_stop_is_ignored_until_restart(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)
        stop_done = threading.Event()
        deadline = time.monotonic() + 2

        try:
            prefetcher.start()
            prefetcher.enqueue("active", {"speed": 1.5})
            self.assertTrue(engine.entered.wait(timeout=2))
            stop_thread = threading.Thread(
                target=lambda: (prefetcher.stop(), stop_done.set())
            )
            stop_thread.start()
            time.sleep(0.05)

            prefetcher.enqueue("during-stop", {"speed": 1.5})
            engine.release.set()
            stop_thread.join(timeout=2)
            prefetcher.enqueue("after-stop", {"speed": 1.5})

            prefetcher.start()
            prefetcher.enqueue("after-restart", {"speed": 1.5})
            while time.monotonic() < deadline and len(engine.calls) < 2:
                time.sleep(0.01)
            prefetcher.stop()

            self.assertTrue(stop_done.is_set())
            self.assertEqual(
                engine.calls,
                [
                    ("active", {"speed": 1.5}),
                    ("after-restart", {"speed": 1.5}),
                ],
            )
        finally:
            engine.release.set()
            prefetcher.stop()

    def test_stop_before_start_discards_pending_and_allows_restart(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        deadline = time.monotonic() + 2

        prefetcher.enqueue("before", {"speed": 1.5})
        prefetcher.stop()
        prefetcher.start()
        prefetcher.enqueue("after", {"speed": 1.5})
        while time.monotonic() < deadline and not engine.calls:
            time.sleep(0.01)
        prefetcher.stop()

        self.assertEqual(engine.calls, [("after", {"speed": 1.5})])

    def test_engine_config_mutation_does_not_leak_queued_key(self):
        engine = MutatingEngine()
        prefetcher = AudioPrefetcher(engine)

        prefetcher.enqueue("hello", {"speed": 1.5})
        prefetcher.drain_for_tests()
        prefetcher.enqueue("hello", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(
            engine.calls,
            [("hello", {"speed": 1.5}), ("hello", {"speed": 1.5})],
        )

    def test_status_reports_pending_active_and_running_counts(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)

        try:
            prefetcher.start()
            prefetcher.enqueue("active", {"speed": 1.5})
            self.assertTrue(engine.entered.wait(timeout=2))
            prefetcher.enqueue("pending", {"speed": 1.5})

            status = prefetcher.status()

            self.assertTrue(status["running"])
            self.assertEqual(status["active"], 1)
            self.assertEqual(status["pending"], 1)
        finally:
            engine.release.set()
            prefetcher.stop()

    def test_idle_callback_runs_once_after_queue_drains(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        callbacks = []

        prefetcher.set_idle_callback(lambda: callbacks.append("idle"))
        prefetcher.enqueue("first", {"speed": 1.5})
        prefetcher.enqueue("second", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(callbacks, ["idle"])

        prefetcher.drain_for_tests()
        self.assertEqual(callbacks, ["idle"])

        prefetcher.enqueue("third", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(callbacks, ["idle", "idle"])

    def test_result_callback_records_success_and_failure(self):
        engine = ReportingEngine()
        prefetcher = AudioPrefetcher(engine)
        results = []

        prefetcher.set_result_callback(
            lambda text, config, ok, error: results.append(
                (text, config["speed"], ok, error)
            )
        )
        prefetcher.enqueue("ok", {"speed": 1.5})
        prefetcher.enqueue("fail", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(
            results,
            [
                ("ok", 1.5, True, None),
                ("fail", 1.5, False, "prefetch failed"),
            ],
        )

    def test_result_callback_treats_none_result_as_canceled_not_failed(self):
        engine = CancelledEngine()
        prefetcher = AudioPrefetcher(engine)
        results = []

        prefetcher.set_result_callback(
            lambda text, config, ok, error: results.append(
                (text, config["speed"], ok, error)
            )
        )
        prefetcher.enqueue("canceled", {"speed": 1.5})
        prefetcher.drain_for_tests()

        self.assertEqual(
            results,
            [("canceled", 1.5, None, None)],
        )


    def test_result_callback_exception_does_not_kill_worker(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        self.addCleanup(prefetcher.stop)

        def bad_callback(text, config, ok, error):
            raise OSError("disk full")

        prefetcher.set_result_callback(bad_callback)
        with self.assertLogs(_MODULE.log, level="ERROR"):
            prefetcher.start()
            prefetcher.enqueue("first", {"speed": 1.5})
            prefetcher.enqueue("second", {"speed": 1.5})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and len(engine.calls) < 2:
                time.sleep(0.01)

        self.assertEqual(len(engine.calls), 2)
        self.assertTrue(prefetcher._thread.is_alive())

    def test_idle_callback_exception_does_not_kill_worker(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        self.addCleanup(prefetcher.stop)

        def bad_idle():
            raise RuntimeError("run_on_main during teardown")

        prefetcher.set_idle_callback(bad_idle)
        with self.assertLogs(_MODULE.log, level="ERROR"):
            prefetcher.start()
            prefetcher.enqueue("first", {"speed": 1.5})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not engine.calls:
                time.sleep(0.01)
            time.sleep(0.05)
            prefetcher.enqueue("second", {"speed": 1.5})
            while time.monotonic() < deadline and len(engine.calls) < 2:
                time.sleep(0.01)

        self.assertEqual(len(engine.calls), 2)
        self.assertTrue(prefetcher._thread.is_alive())

    def test_stop_cancels_inflight_engine_request(self):
        class CancellableEngine(BlockingEngine):
            def __init__(self):
                super().__init__()
                self.cancel_calls = 0

            def cancel_prefetch(self):
                self.cancel_calls += 1
                self.release.set()

        engine = CancellableEngine()
        prefetcher = AudioPrefetcher(engine)
        prefetcher.start()
        prefetcher.enqueue("active", {"speed": 1.5})
        self.assertTrue(engine.entered.wait(timeout=2))

        started = time.monotonic()
        self.assertTrue(prefetcher.stop(timeout=1))

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(engine.cancel_calls, 1)

    def test_worker_pauses_queue_while_engine_reports_pause(self):
        class PausingEngine(FakeEngine):
            def __init__(self):
                super().__init__()
                self.pause = 5.0

            def prefetch_pause_seconds(self):
                return self.pause

        engine = PausingEngine()
        prefetcher = AudioPrefetcher(engine)
        prefetcher.PAUSE_POLL_SECONDS = 0.01
        self.addCleanup(prefetcher.stop)

        with self.assertLogs(_MODULE.log, level="WARNING") as logs:
            prefetcher.start()
            prefetcher.enqueue("waiting", {"speed": 1.5})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not prefetcher.status()["paused"]:
                time.sleep(0.01)
            time.sleep(0.05)

            self.assertTrue(prefetcher.status()["paused"])
            self.assertEqual(engine.calls, [])
            engine.pause = 0
            while time.monotonic() < deadline and not engine.calls:
                time.sleep(0.01)

        self.assertEqual(engine.calls, [("waiting", {"speed": 1.5})])
        self.assertFalse(prefetcher.status()["paused"])
        self.assertEqual(len(logs.output), 1)

    def test_stop_while_paused_returns_promptly(self):
        class PausedEngine(FakeEngine):
            def prefetch_pause_seconds(self):
                return 60

        engine = PausedEngine()
        prefetcher = AudioPrefetcher(engine)
        with self.assertLogs(_MODULE.log, level="WARNING"):
            prefetcher.start()
            prefetcher.enqueue("waiting", {"speed": 1.5})
            deadline = time.monotonic() + 2
            while time.monotonic() < deadline and not prefetcher.status()["paused"]:
                time.sleep(0.01)

            started = time.monotonic()
            self.assertTrue(prefetcher.stop(timeout=1))

        self.assertLess(time.monotonic() - started, 0.5)
        self.assertEqual(engine.calls, [])

    def test_status_reports_counts_and_last_error(self):
        engine = ReportingEngine()
        prefetcher = AudioPrefetcher(engine)

        prefetcher.enqueue("ok", {"speed": 1.5})
        prefetcher.enqueue("fail", {"speed": 1.5})
        prefetcher.drain_for_tests()

        status = prefetcher.status()
        self.assertEqual(status["succeeded"], 1)
        self.assertEqual(status["failed"], 1)
        self.assertEqual(status["last_error"], "prefetch failed")
        self.assertIsNone(status["current"])
        self.assertFalse(status["paused"])

    def test_user_pause_holds_queue_and_resume_drains_it(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        prefetcher.USER_PAUSE_POLL_SECONDS = 0.01
        self.addCleanup(prefetcher.stop, 1)
        prefetcher.pause()
        self.assertTrue(prefetcher.is_paused())
        prefetcher.start()
        prefetcher.enqueue("a", {"speed": 1.5})
        prefetcher.enqueue("b", {"speed": 1.5})
        time.sleep(0.1)
        self.assertEqual(engine.calls, [])
        status = prefetcher.status()
        self.assertTrue(status["paused"])
        self.assertTrue(status["user_paused"])
        self.assertFalse(status["edge_waiting"])
        self.assertEqual(status["pending"], 2)

        prefetcher.resume()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and len(engine.calls) < 2:
            time.sleep(0.01)
        self.assertEqual([c[0] for c in engine.calls], ["a", "b"])
        self.assertFalse(prefetcher.status()["paused"])

    def test_pause_lets_current_item_finish(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)
        self.addCleanup(prefetcher.stop, 1)
        prefetcher.start()
        prefetcher.enqueue("first", {"speed": 1.5})
        prefetcher.enqueue("second", {"speed": 1.5})
        self.assertTrue(engine.entered.wait(timeout=2))
        prefetcher.pause()
        engine.release.set()
        time.sleep(0.2)
        self.assertEqual([c[0] for c in engine.calls], ["first"])

    def test_stop_while_user_paused_returns_quickly(self):
        # Quit-hang regression: a paused worker must never block close.
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        prefetcher.USER_PAUSE_POLL_SECONDS = 5  # prove stop() wakes the wait
        prefetcher.pause()
        prefetcher.start()
        prefetcher.enqueue("held", {"speed": 1.5})
        time.sleep(0.05)
        started = time.monotonic()
        self.assertTrue(prefetcher.stop(timeout=1))
        self.assertLess(time.monotonic() - started, 1.0)
        self.assertEqual(engine.calls, [])

    def test_restart_keeps_user_pause(self):
        engine = FakeEngine()
        prefetcher = AudioPrefetcher(engine)
        prefetcher.USER_PAUSE_POLL_SECONDS = 0.01
        prefetcher.pause()
        prefetcher.start()
        self.assertTrue(prefetcher.stop(timeout=1))
        prefetcher.start()
        self.addCleanup(prefetcher.stop, 1)
        prefetcher.enqueue("x", {"speed": 1.5})
        time.sleep(0.1)
        self.assertEqual(engine.calls, [])
        prefetcher.resume()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and not engine.calls:
            time.sleep(0.01)
        self.assertEqual(len(engine.calls), 1)

    def test_status_reports_current_item_start_time(self):
        engine = BlockingEngine()
        prefetcher = AudioPrefetcher(engine)
        self.addCleanup(prefetcher.stop, 1)
        prefetcher.start()
        before = time.monotonic()
        prefetcher.enqueue("slow   card\ntext", {"speed": 1.5})
        self.assertTrue(engine.entered.wait(timeout=2))
        status = prefetcher.status()
        self.assertEqual(status["current"], "slow card text")
        self.assertGreaterEqual(status["current_started_at"], before)
        engine.release.set()
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline and prefetcher.status()["current"]:
            time.sleep(0.01)
        self.assertIsNone(prefetcher.status()["current_started_at"])

    def test_failure_uses_engine_reason_instead_of_generic_message(self):
        class ReasonEngine(FakeEngine):
            def prefetch(self, text, config):
                self.calls.append((text, dict(config)))
                return False

            def last_prefetch_error(self):
                return "request timed out"

        results = []
        prefetcher = AudioPrefetcher(ReasonEngine())
        prefetcher.set_result_callback(
            lambda text, config, ok, error: results.append((ok, error))
        )
        prefetcher.enqueue("x", {"speed": 1.5})
        prefetcher.drain_for_tests()
        self.assertEqual(results, [(False, "request timed out")])
        self.assertEqual(prefetcher.status()["last_error"], "request timed out")


if __name__ == "__main__":
    unittest.main()
