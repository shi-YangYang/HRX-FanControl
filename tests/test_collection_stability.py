"""Collection regressions use temporary fake executables, never a physical BMC."""
import io
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path
from unittest.mock import patch

from hrx_fancontrol.collection import CollectionStats, collect, poll_wait_seconds
from hrx_fancontrol.config import load_config
from hrx_fancontrol.ipmi import CollectionCancelled, CommandResult, DIAGNOSTIC_TAIL_CHARS, ReadOnlyIPMI
from hrx_fancontrol.main import main, render
from hrx_fancontrol.thermal_engine import ThermalEngine


SAMPLE = (Path(__file__).resolve().parents[1] / "examples/hr650x-sensors.txt").read_text()
STATUS = "20 0f 0f 0f 0f 0f 0f 0f 0f"


class FakeIPMIProgram(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        (self.root / "sample").write_text(SAMPLE)
        environment = patch.dict(os.environ, {
            "PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
            "HRX_FAKE_ROOT": str(self.root),
        })
        environment.start()
        self.addCleanup(environment.stop)

    def program(self, body):
        script = self.root / "ipmitool"
        script.write_text(f"#!{sys.executable}\n" +
                          "import json, os, signal, sys, time\nfrom pathlib import Path\n"
                          "root = Path(os.environ['HRX_FAKE_ROOT'])\n"
                          "args = sys.argv[1:]\n"
                          "assert args in (['-I', 'open', 'sensor'], ['-I', 'open', 'raw', '0x2e', '0x31'])\n"
                          "with (root / 'calls').open('a') as stream:\n"
                          "    stream.write(json.dumps(args) + '\\n')\n" + body)
        script.chmod(0o755)

    def wait_until(self, predicate):
        deadline = time.monotonic() + 5
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.01)
        self.fail("Timed out waiting for the fake IPMI process")

    def records(self, directory="logs"):
        return [json.loads(line) for path in (self.root / directory).glob("*.jsonl")
                for line in path.read_text().splitlines()]

    def assert_gone(self, pid):
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class ProcessLifecycleTest(FakeIPMIProgram):
    def test_exit_zero_parse_failures_keep_bounded_diagnostics_without_partial_readings(self):
        self.program("if args[-1] == 'sensor':\n"
                     "    print((root / 'sample').read_text() + 'bad' * 1000)\n"
                     "else:\n"
                     "    print('20 gg ' + 'ff ' * 1000)\n")
        c = load_config()
        report = collect(ReadOnlyIPMI(), ThermalEngine(c), c)
        self.assertEqual(report["collection"]["failure_kinds"], ["sensor:parse", "status:parse"])
        for command in report["commands"]:
            self.assertEqual(command["returncode"], 0)
            self.assertEqual(command["failure_kind"], "parse")
            self.assertEqual(len(command["stdout_tail"]), DIAGNOSTIC_TAIL_CHARS)
            self.assertGreater(command["stdout_bytes"], DIAGNOSTIC_TAIL_CHARS)
        self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])
        self.assertTrue(all(fan["rpm"] is None for fan in report["fans"].values()))
        self.assertIsNone(report["bmc"]["raw_bytes"])

    def test_timeout_preserves_only_bounded_diagnostics_and_reaps(self):
        self.program("(root / 'pid').write_text(str(os.getpid()))\n"
                     "sys.stdout.write('X' * 6000 + '\\n' + (root / 'sample').read_text())\n"
                     "sys.stdout.flush()\n"
                     "sys.stderr.write('E' * 6000); sys.stderr.flush()\n"
                     "time.sleep(30)\n")
        result = ReadOnlyIPMI(timeout=1).sensors()
        self.assertEqual(result.failure_kind, "timeout")
        self.assertEqual(result.error, "IPMI timeout")
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.timeout_seconds, 1)
        self.assertGreaterEqual(result.elapsed_seconds, 1)
        self.assertLess(result.elapsed_seconds, 3)
        self.assertEqual(result.stdout_bytes, len(("X" * 6000 + "\n" + SAMPLE).encode()))
        self.assertEqual(result.stderr_bytes, 6000)
        self.assertEqual(result.stdout_lines, 1 + len(SAMPLE.splitlines()))
        self.assertLessEqual(len(result.stdout_tail), DIAGNOSTIC_TAIL_CHARS)
        self.assertLessEqual(len(result.stderr), DIAGNOSTIC_TAIL_CHARS)
        self.assertTrue(result.stdout_tail.endswith(SAMPLE[-200:]))
        self.assertNotIn("stdout", result.summary())
        pid = int((self.root / "pid").read_text())
        self.assert_gone(pid)
        with self.assertRaises(ChildProcessError):
            os.waitpid(pid, os.WNOHANG)
        c = load_config()
        c["ipmi"]["read_control_status"] = False
        with patch.object(ReadOnlyIPMI, "sensors", return_value=result):
            report = collect(ReadOnlyIPMI(), ThermalEngine(c), c)
        self.assertEqual(report["collection"]["counts"]["timed_out"], 1)
        self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])
        self.assertTrue(all(fan["rpm"] is None for fan in report["fans"].values()))

    def test_nonzero_and_stderr_outputs_never_become_sensor_data(self):
        for exit_code, expected in ((7, "nonzero-exit"), (0, "diagnostic")):
            with self.subTest(exit_code=exit_code):
                self.program("sys.stdout.write((root / 'sample').read_text())\n"
                             "sys.stderr.write('diagnostic \\u6d4b\\u8bd5' * 800)\n"
                             f"sys.exit({exit_code})\n")
                c = load_config()
                c["ipmi"]["read_control_status"] = False
                report = collect(ReadOnlyIPMI(), ThermalEngine(c), c)
                command = report["commands"][0]
                self.assertEqual(command["failure_kind"], expected)
                self.assertLessEqual(len(command["stderr"]), DIAGNOSTIC_TAIL_CHARS)
                self.assertLessEqual(len(command["stdout_tail"]), DIAGNOSTIC_TAIL_CHARS)
                self.assertGreater(command["stderr_bytes"], DIAGNOSTIC_TAIL_CHARS)
                self.assertEqual(report["safety_state"], "waiting-for-sample")
                self.assertIsNone(report["recommended_level"])
                self.assertIsNone(report["component_level"])
                self.assertIsNone(report["policy_target_percent"])
                self.assertFalse(report["collection"]["sensor_data_available"])
                self.assertIsNone(report["sensors"]["PCH_Temp"]["observed_value"])
                self.assertTrue(all(fan["rpm"] is None for fan in report["fans"].values()))
                self.assertNotIn("Component demand L1", render(report))
                self.assertIn("Waiting for valid sensor data", render(report))

    def test_cancel_active_process_including_ignored_terminate(self):
        for ignore_term in (False, True):
            with self.subTest(ignore_term=ignore_term):
                ready = self.root / "ready"
                ready.unlink(missing_ok=True)
                self.program(("signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if ignore_term else "") +
                             "(root / 'ready').write_text(str(os.getpid()))\n"
                             "sys.stdout.write('PCH_Temp | '); sys.stdout.flush()\n"
                             "time.sleep(30)\n")
                stop, outcomes, children = threading.Event(), [], []
                real_popen = subprocess.Popen

                def spawn(*args, **kwargs):
                    child = real_popen(*args, **kwargs)
                    children.append(child)
                    return child

                def read():
                    try:
                        outcomes.append(ReadOnlyIPMI(stop=stop).sensors())
                    except CollectionCancelled as exc:
                        outcomes.append(exc)

                with patch("hrx_fancontrol.ipmi.subprocess.Popen", side_effect=spawn):
                    worker = threading.Thread(target=read, daemon=True)
                    worker.start()
                    try:
                        self.wait_until(lambda: ready.exists() and ready.stat().st_size > 0)
                        pid = int(ready.read_text())
                        self.assertEqual(os.getpgid(pid), pid)
                        started = time.monotonic()
                        stop.set()
                        worker.join(timeout=3)
                        self.assertFalse(worker.is_alive())
                        self.assertLess(time.monotonic() - started, 3)
                    finally:
                        stop.set()
                        worker.join(timeout=3)
                self.assertEqual(len(outcomes), 1)
                self.assertIsInstance(outcomes[0], CollectionCancelled)
                self.assertEqual(children[0].returncode, -signal.SIGKILL if ignore_term else -signal.SIGTERM)
                self.assert_gone(pid)

    @patch("hrx_fancontrol.ipmi.subprocess.Popen")
    def test_cancel_before_start_never_spawns(self, popen):
        stop = threading.Event()
        stop.set()
        with self.assertRaises(CollectionCancelled):
            ReadOnlyIPMI(stop=stop).sensors()
        popen.assert_not_called()


class CollectionStabilityTest(unittest.TestCase):
    def test_cancel_after_sensor_and_before_status_has_no_partial_evaluation(self):
        stop, alerts = threading.Event(), []
        c = load_config()
        engine, stats = ThermalEngine(c), CollectionStats()

        def interrupted_sensor():
            stop.set()
            return CommandResult(("sensor",), "PCH_Temp | ", "", 255, "ipmitool exit 255", 0.1)

        with patch.object(ReadOnlyIPMI, "sensors", side_effect=interrupted_sensor), \
                patch.object(ReadOnlyIPMI, "control_status") as status:
            with self.assertRaises(CollectionCancelled):
                collect(ReadOnlyIPMI(), engine, c, on_alert=alerts.append, stop=stop, stats=stats)
            status.assert_not_called()
        self.assertIsNone(engine.last_update)
        self.assertEqual(alerts, [])
        self.assertEqual(stats.snapshot(), dict(started=1, completed=0, successful=0, failed=0,
                                               timed_out=0, cancelled=1))

        stop.clear()
        hot = SAMPLE.replace("60.000     | degrees C  | ok", "80.000     | degrees C  | unc")
        with patch.object(ReadOnlyIPMI, "sensors", return_value=CommandResult(("sensor",), hot, "", 0, None, 0.1)), \
                patch.object(ReadOnlyIPMI, "control_status") as status:
            with self.assertRaises(CollectionCancelled):
                collect(ReadOnlyIPMI(), engine, c, on_alert=lambda _: stop.set(), stop=stop)
            status.assert_not_called()

    def test_collection_failure_waits_but_thermal_and_fan_alarms_do_not(self):
        c = load_config()
        for sample in (SAMPLE.replace("60.000     | degrees C  | ok", "80.000     | degrees C  | unc"),
                       SAMPLE.replace("3335.000", "0.000")):
            report = collect(None, ThermalEngine(c), c, source=sample)
            self.assertTrue(report["faults"] or report["emergencies"] or report["bmc_alarms"])
            self.assertEqual(report["collection"]["status"], "ok")
            self.assertEqual(poll_wait_seconds(report, 15, 8), 7)
        report = collect(None, ThermalEngine(c), c, source="malformed table")
        self.assertFalse(report["collection"]["sensor_data_available"])
        self.assertEqual(poll_wait_seconds(report, 15, 21), 15)

    def test_two_timeouts_in_one_poll_count_once(self):
        c = load_config()
        timeout = CommandResult(("sensor",), "", "", None, "IPMI timeout", 20, failure_kind="timeout")
        with patch.object(ReadOnlyIPMI, "_execute", return_value=timeout):
            report = collect(ReadOnlyIPMI(), ThermalEngine(c), c)
        self.assertEqual(report["collection"]["failure_kinds"], ["sensor:timeout", "status:timeout"])
        self.assertEqual(report["collection"]["counts"]["timed_out"], 1)
        self.assertEqual(report["collection"]["counts"]["failed"], 1)

    def test_cli_counts_every_poll_even_when_minute_logging_omits_reports(self):
        stop, clock, waits, records = threading.Event(), [0.0], [], []
        sensor_calls = [0]

        class MemoryLogger:
            dropped, error = 0, None

            def __init__(self, _settings):
                pass

            def emit(self, record):
                records.append(json.loads(json.dumps(record)))

            def close(self):
                pass

        def execute(command, timeout):
            if command[-1] == "sensor":
                sensor_calls[0] += 1
                if sensor_calls[0] == 1:
                    clock[0] += 20
                    return CommandResult(command, "", "", None, "IPMI timeout", 20,
                                         timeout, "timeout")
                clock[0] += 7
                return CommandResult(command, SAMPLE, "", 0, None, 7, timeout)
            clock[0] += 0.1
            return CommandResult(command, STATUS, "", 0, None, 0.1, timeout)

        def wait(delay):
            waits.append(delay)
            clock[0] += delay
            if len(waits) == 5:
                stop.set()

        output = io.StringIO()
        with patch("hrx_fancontrol.main.threading.Event", return_value=stop), \
                patch("hrx_fancontrol.main.AsyncLogger", MemoryLogger), \
                patch("hrx_fancontrol.main.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(stop, "wait", side_effect=wait), \
                patch.object(ReadOnlyIPMI, "_execute", side_effect=execute), \
                redirect_stdout(output), redirect_stderr(io.StringIO()):
            result = main(["--json"])
        reports = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 0)
        self.assertEqual([report["poll_id"] for report in reports], [1, 2, 3, 4, 5])
        self.assertEqual(len({report["run_id"] for report in reports}), 1)
        self.assertEqual(waits[0], 15)
        for delay in waits[1:]:
            self.assertAlmostEqual(delay, 7.9)
        persisted = [record for record in records if record["event"] in ("transition", "summary")]
        self.assertLess(len(persisted), len(reports))
        self.assertEqual(records[-1]["collection_counts"], dict(started=5, completed=5, successful=4,
                                                              failed=1, timed_out=1, cancelled=0))
        self.assertEqual(reports[-1]["collection"]["counts"], records[-1]["collection_counts"])
        self.assertFalse(any(record["event"] == "safety_alert" for record in records))
        first = persisted[0]
        self.assertEqual(first["collection"]["status"], "failed")
        self.assertFalse(any(record["applied"] for record in records))


class CLICancellationTest(FakeIPMIProgram):
    def cli(self, config):
        path = self.root / "profile.yaml"
        path.write_text(config)
        process = subprocess.Popen([sys.executable, "-m", "hrx_fancontrol", "--config", str(path), "--json"],
                                   cwd=self.root, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                                   text=True, start_new_session=True)

        def cleanup():
            if process.poll() is None:
                process.kill()
            process.communicate(timeout=3)

        self.addCleanup(cleanup)
        return process

    def test_sigint_during_sensor_and_sigterm_during_status_are_normal_shutdown(self):
        for sig, stage in ((signal.SIGINT, "sensor"), (signal.SIGTERM, "status")):
            with self.subTest(stage=stage):
                ready = self.root / "ready"
                ready.unlink(missing_ok=True)
                self.program(
                    ("if args[-1] == 'sensor':\n"
                     "    print((root / 'sample').read_text(), end=''); sys.exit(0)\n"
                     "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n" if stage == "status" else "") +
                    "(root / 'ready').write_text(str(os.getpid()))\n"
                    "sys.stdout.write('PCH_Temp | '); sys.stdout.flush()\n"
                    "time.sleep(30)\n")
                process = self.cli(f"logging:\n  directory: logs-{stage}\n")
                self.wait_until(lambda: ready.exists() and ready.stat().st_size > 0)
                pid = int(ready.read_text())
                started = time.monotonic()
                os.killpg(process.pid, sig)
                stdout, stderr = process.communicate(timeout=3)
                self.assertLess(time.monotonic() - started, 3)
                self.assertEqual(process.returncode, 0)
                self.assertEqual(stdout, "")
                self.assertEqual(stderr, "")
                self.assert_gone(pid)
                records = self.records(f"logs-{stage}")
                self.assertEqual([record["event"] for record in records], ["shutdown"])
                self.assertEqual(records[0]["reason"], "requested")
                self.assertEqual(records[0]["collection_counts"], dict(started=1, completed=0, successful=0,
                                                                     failed=0, timed_out=0, cancelled=1))

    def test_signal_interrupts_failure_wait_and_keeps_real_fault_evidence(self):
        self.program("if args[-1] == 'sensor':\n"
                     "    print((root / 'sample').read_text(), end=''); sys.exit(7)\n"
                     f"print({STATUS!r})\n")
        process = self.cli("logging:\n  directory: logs\n")

        def recorded_transition():
            return any('"event": "transition"' in path.read_text()
                       for path in (self.root / "logs").glob("*.jsonl"))

        self.wait_until(recorded_transition)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=3)
        self.assertEqual(process.returncode, 0)
        self.assertEqual(stderr, "")
        self.assertIn("ipmitool exit 7", stdout)
        reports = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(len(reports), 1)
        self.assertEqual(reports[0]["collection"]["status"], "failed")
        records = self.records()
        self.assertEqual([record["event"] for record in records], ["transition", "shutdown"])
        self.assertEqual(records[-1]["collection_counts"], dict(started=1, completed=1, successful=0,
                                                              failed=1, timed_out=0, cancelled=0))
        self.assertEqual(len((self.root / "calls").read_text().splitlines()), 2)


if __name__ == "__main__":
    unittest.main()
