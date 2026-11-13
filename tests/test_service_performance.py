"""Responsiveness, wakeup and journal contracts without a hardware device."""
import io
import os
from pathlib import Path
import signal
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from hrx_fancontrol.control.commands import CommandServer, Request, command_client
from hrx_fancontrol.control.lifecycle import StopEvent, StopSignals
from hrx_fancontrol.reporting.logger import AsyncLogger, journal_line
import test_service_commands as fixtures
from test_controlled_execution import report, status_result


class ServicePerformanceTests(unittest.TestCase):
    setUp = fixtures.ServiceTests.setUp

    def blocked_status(self, action):
        entered, release = threading.Event(), threading.Event()
        errors = []
        def blocked(*_):
            entered.set()
            if not release.wait(3):
                raise AssertionError("fake hardware was not released")
            return report(1, self.now, recommended=1) if action == "sampling" else fixtures.success()
        if action == "sampling":
            self.collect.side_effect = blocked
        else:
            self.ipmi.control_status.side_effect = lambda: status_result(2 if release.is_set() else 1)
            self.ipmi.set_parameter.side_effect = blocked
            self.queue.put("manual", 20)
            previous_emit = self.executor.emit
            def publish_event(event, state):
                previous_emit(event, state)
                self.controller.publish_status()
            self.executor.emit = publish_event
        def run():
            try:
                self.controller.tick(self.queue)
            except Exception as exc:
                errors.append(exc)
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory, \
                CommandServer(directory, self.controller.cached_status) as server:
            worker = threading.Thread(target=run)
            worker.start()
            try:
                self.assertTrue(entered.wait(2))
                state = command_client({"action": "status"}, directory)["status"]
                self.assertTrue(worker.is_alive())  # Returned before any hardware completion.
                self.assertEqual(state["activity"], "sampling" if action == "sampling" else "command")
                self.assertEqual(list(server.pending()), [])
                if action == "sampling":
                    self.assertIsNone(state["last_sample_time"])
                else:
                    self.assertFalse(state["automatic"])
                    self.assertEqual(state["manual_parameter"], 20)
                    self.assertEqual(state["control"]["parameter"], 20)
                    self.assertFalse(state["control"]["verified"])
            finally:
                release.set()
                worker.join(3)
            self.assertFalse(worker.is_alive())
            self.assertFalse(errors)
            return self.controller.cached_status()

    def test_status_returns_before_blocked_sensor_read(self):
        state = self.blocked_status("sampling")
        self.assertIsNone(state["activity"])
        self.assertIsNotNone(state["last_sample_time"])

    def test_status_returns_new_unconfirmed_intent_during_blocked_manual_write(self):
        state = self.blocked_status("manual")
        self.assertTrue(state["control"]["verified"])
        self.ipmi.set_parameter.assert_called_once_with(20)

    def test_cached_ages_keep_growing_and_returned_objects_are_independent(self):
        self.controller.tick(self.queue)
        before = self.controller.cached_status()
        self.now += 45
        state = self.controller.cached_status()
        self.assertEqual(state["sample_age_seconds"], 45)
        self.assertEqual(state["control"]["bmc_read_age_seconds"], 45)
        state["fans_rpm"].clear()
        state["control"]["bmc_raw_bytes"].clear()
        self.assertEqual(self.controller.cached_status()["fans_rpm"], before["fans_rpm"])
        self.assertEqual(len(self.controller.cached_status()["control"]["bmc_raw_bytes"]), 9)
        self.collect.assert_called_once()

    def test_idle_wait_has_no_polling_status_does_not_wake_it_and_stop_is_prompt(self):
        stop = StopEvent()
        self.addCleanup(stop.close)
        self.executor.stop = stop
        self.controller.apply({"action": "off"})
        waiting, wait_timeouts = threading.Event(), []
        original_wait = stop.wake.wait
        def observed_wait(timeout=None):
            wait_timeouts.append(timeout)
            waiting.set()
            return original_wait(timeout)
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory, \
                CommandServer(directory, self.controller.cached_status, stop.wake) as server, \
                patch.object(stop.wake, "wait", side_effect=observed_wait), \
                patch.dict(os.environ, {}, clear=True):
            worker = threading.Thread(target=self.controller.run, args=(server,))
            worker.start()
            try:
                self.assertTrue(waiting.wait(2))
                state = command_client({"action": "status"}, directory)["status"]
                self.assertFalse(state["automatic"])
                self.assertEqual(wait_timeouts, [None])
                self.assertEqual(command_client({"action": "off"}, directory)["status"]["mode"], "paused")
            finally:
                stop.set()
                worker.join(2)
            self.assertFalse(worker.is_alive())
        self.collect.assert_not_called()
        self.ipmi.set_level.assert_not_called()

    def test_deadline_wait_and_main_loop_watchdog_are_scheduled(self):
        self.controller.next_poll = self.now + 15
        self.assertEqual(self.controller.wait_seconds(), 15)
        self.executor.status = "pending-verification"
        self.executor.completed_at = self.now - 89
        self.assertEqual(self.controller.wait_seconds(), 1)
        self.controller.apply({"action": "off"})
        waits = []
        server = Mock()
        server.pending.return_value = ()
        def wait(timeout):
            waits.append(timeout)
            self.now += timeout
            if len(waits) == 2:
                self.stop.set()
        server.wake.wait.side_effect = wait
        with patch.dict(os.environ, {"WATCHDOG_USEC": "2000000", "NOTIFY_SOCKET": "@fake"}), \
                patch("hrx_fancontrol.control.service.notify") as notify:
            self.controller.run(server)
        self.assertEqual(waits, [1, 1])
        self.assertEqual(notify.call_count, 2)

    def test_empty_queue_clear_cannot_erase_a_concurrent_enqueue(self):
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory, CommandServer(directory) as server:
            empty, trying = threading.Event(), threading.Event()
            original = server.requests.get_nowait
            def get_empty():
                empty.set()
                self.assertTrue(trying.wait(2))
                return original()
            request = Request({"action": "off"})
            def enqueue():
                empty.wait(2)
                trying.set()
                with server.queue_lock:
                    server.requests.put_nowait(request)
                    server.wake.set()
            producer = threading.Thread(target=enqueue)
            producer.start()
            with patch.object(server.requests, "get_nowait", side_effect=get_empty):
                self.assertEqual(list(server.pending()), [])
            producer.join(2)
            self.assertFalse(producer.is_alive())
            self.assertTrue(server.wake.wait(.5))
            self.assertEqual(list(server.pending()), [request])

    def test_slow_command_batch_reports_main_loop_progress_between_commands(self):
        self.controller.apply({"action": "off"})
        server = Mock()
        requests = [Request({"action": "off"}), Request({"action": "off"})]
        server.pending.return_value = iter(requests)
        server.wake.wait.side_effect = lambda _: self.stop.set()
        def slow_command(_):
            self.now += 1.2
            return "ok"
        times = []
        with patch.object(self.controller, "apply", side_effect=slow_command), \
             patch.dict(os.environ, {"WATCHDOG_USEC": "2000000", "NOTIFY_SOCKET": "@fake"}), \
             patch("hrx_fancontrol.control.service.notify", side_effect=lambda _: times.append(self.now)):
            self.controller.run(server)
        self.assertEqual(len(times), 2)
        self.assertAlmostEqual(times[1] - times[0], 1.2)
        self.assertTrue(all(request.done.is_set() for request in requests))

    def test_signal_wakeup_and_blocking_accept_shutdown(self):
        stop = StopEvent()
        self.addCleanup(stop.close)
        with StopSignals(stop):
            os.kill(os.getpid(), signal.SIGTERM)
            self.assertTrue(stop.is_set())
            self.assertTrue(stop.wake.wait(1))
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory:
            with CommandServer(directory) as server:
                thread = server.thread
            self.assertFalse(thread.is_alive())
            self.assertFalse(Path(directory, "control.sock").exists())


class JournalPerformanceTests(unittest.TestCase):
    def test_file_failure_still_reaches_journal(self):
        from hrx_fancontrol.reporting.logger import DailyWriter
        from hrx_fancontrol.config import load_config
        records = []
        with tempfile.TemporaryDirectory() as directory, \
             patch.object(DailyWriter, "write", side_effect=OSError("disk full")), \
             patch("sys.stderr", io.StringIO()):
            logger = AsyncLogger({**load_config()["logging"], "directory": directory}, records.append)
            logger.emit({"event": "shutdown"})
            logger.close()
        self.assertEqual(records, [{"event": "shutdown"}])
        self.assertEqual(logger.error, "disk full")
    def test_runtime_throttles_background_but_keeps_interactive_and_json_reports(self):
        from argparse import Namespace
        from hrx_fancontrol.config import load_config
        from hrx_fancontrol.control.control_runtime import run_control
        from hrx_fancontrol.control.executor import Executor
        from hrx_fancontrol.control.lifecycle import WriterLock
        from hrx_fancontrol.hardware.collection import collect
        from hrx_fancontrol.hardware.ipmi import CollectionCancelled
        from hrx_fancontrol.policy.thermal_engine import ThermalEngine
        from test_sensor_cache import SAMPLE
        for json_mode, tty in ((False, False), (True, False), (False, True)):
            with self.subTest(json=json_mode, tty=tty), tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory:
                config = load_config()
                config["logging"]["enabled"] = False
                now = [100.0]
                ipmi = Mock()
                args = Namespace(calibrate=None, service_recover=False, profile=None, json=json_mode, resume_control=False)
                def sample(*_args, **_kwargs):
                    value = collect(ipmi, ThermalEngine(config), config, source=SAMPLE)
                    value["bmc"].update(raw_bytes=fixtures.raw(1), read_at_monotonic=now[0])
                    return value
                def bounded_run(controller, server):
                    for now[0] in (100., 115., 130., 160.):
                        controller.next_poll = 0
                        controller.tick(server)
                    raise CollectionCancelled()
                output = io.StringIO()
                output.isatty = lambda: tty
                with patch("time.monotonic", side_effect=lambda: now[0]), \
                     patch("hrx_fancontrol.control.control_runtime.Executor", side_effect=lambda *a: Executor(*a, clock=lambda: now[0])), \
                     patch("hrx_fancontrol.control.control_runtime.ControlIPMI", return_value=ipmi), \
                     patch("hrx_fancontrol.control.control_runtime.machine_identity", return_value=fixtures.IDENTITY), \
                     patch("hrx_fancontrol.control.control_runtime.WriterLock", side_effect=lambda: WriterLock(directory)), \
                     patch("hrx_fancontrol.control.control_runtime.control_state_directory", return_value=Path(directory)), \
                     patch("hrx_fancontrol.control.control_runtime.collect", side_effect=sample), \
                     patch("hrx_fancontrol.control.service.ServiceController.run", bounded_run), \
                     patch("sys.stdout", output), patch("sys.stderr", io.StringIO()):
                    self.assertEqual(run_control(args, config), 0)
                ipmi.enable_sensor_cache.assert_called_once()
                self.assertEqual(ipmi.enable_sensor_cache.call_args.args[1], Path(directory))
                ipmi.close.assert_called_once()
                text = output.getvalue()
                if json_mode:
                    self.assertEqual(text.count('"source": "offline-snapshot"'), 4)
                elif tty:
                    self.assertEqual(text.count("Sensor              Celsius"), 4)
                else:
                    self.assertEqual(text.count("采样完成"), 2)
                    self.assertNotIn("Sensor              Celsius", text)
                    self.assertIn("控制服务已启动", text)

    def test_output_works_without_file_log_and_blocked_sink_cannot_block_producer(self):
        started, release = threading.Event(), threading.Event()
        records = []
        def slow(record):
            started.set()
            release.wait(5)
            records.append(record)
        logger = AsyncLogger({"enabled": False, "queue_capacity": 1}, output=slow)
        try:
            logger.emit({"event": "first"})
            self.assertTrue(started.wait(2))
            before = time.monotonic()
            logger.emit({"event": "second"})
            logger.emit({"event": "overflow"})
            self.assertLess(time.monotonic() - before, .2)
            self.assertEqual(logger.dropped, 1)
            before = time.monotonic()
            logger.close()
            self.assertLess(time.monotonic() - before, 3.5)
            self.assertIn("shutdown deadline", logger.error)
        finally:
            release.set()
            logger.close()
        self.assertFalse(logger.thread.is_alive())
        self.assertEqual([row["event"] for row in records], ["first", "second"])

    def test_short_journal_keeps_failure_details_without_full_tables(self):
        line = journal_line({"time": "now", "event": "summary", "message": "采样完成。",
                             "temperatures_c": {"PCH_Temp": 63}, "fans_rpm": {"FAN1_R_Speed": 3680},
                             "sampling": {"elapsed_seconds": 1.234},
                             "control": {"parameter": 15, "status": "verified"},
                             "issues": ["Inlet_Temp: BMC 温度告警"],
                             "errors": [{"operation": "目录查询", "kind": "timeout", "message": "读取超时"}]})
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn("1.234s", line)
        self.assertIn("读取超时", line)
        self.assertIn("目录查询", line)
        self.assertNotIn("PCH_Temp", line)


if __name__ == "__main__":
    unittest.main()
