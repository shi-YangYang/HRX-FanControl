import io
import json
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hrx_fancontrol.config import ConfigError, load_config
from hrx_fancontrol.hysteresis import Hysteresis
from hrx_fancontrol.ipmi import CommandResult, ReadOnlyIPMI, parse_control_status
from hrx_fancontrol.logger import AsyncLogger, DailyWriter, transition_key
from hrx_fancontrol.main import L5_ALERT, collect, main, render
from hrx_fancontrol.sensors import parse_sensors, validate_temperature
from hrx_fancontrol.thermal_engine import ThermalEngine


SAMPLE = (Path(__file__).resolve().parents[1] / "examples/hr650x-sensors.txt").read_text()


class ProfileTest(unittest.TestCase):
    def profile(self, text):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "profile.yaml"
            path.write_text(text)
            return load_config(path)

    def test_reject_unsafe_or_malformed_profiles(self):
        for text in (
            "controller: {enabled: true}", "controller: {mode: automatic}",
            "ipmi: {interface: lanplus}", "controller: {poll_interval_seconds: 0}",
            "controller: {poll_interval_seconds: .nan}", "controller: {typo: 1}",
            "safety: {fail_safe_enabled: false}", "safety: {safe_level: 6}",
            "safety: {sensor_timeout_seconds: 5}", "logging: {retention_days: 1.5}",
            "logging: {backup_enabled: true}", "logging: {enabled: 'false'}",
            "curves: {pch: [56, 60, 59, 70]}", "curves: {pch: [56, 60, 70]}",
            "fan: {levels: [15, 25, 35, 45, 100]}", "fan: {installed: [FAN1, fan1]}",
            "sensors: {PCH_Temp: {curve: unknown}}", "controller: {}\ncontroller: {}",
            "sensors: {PCH_Temp: {minimum: 200}}", "[]", "",
            "ipmi: {command_timeout_seconds: 30}", "ipmi: {status_timeout_seconds: 30}",
            "ipmi: {status_timeout_seconds: .nan}", "ipmi: {status_timeout_seconds: true}",
            "ipmi: {status_timeout_seconds: 0}", "ipmi: {status_timeout_seconds: 10}",
            "safety: {read_failure_count: 0}", "safety: {read_failure_count: true}",
            "safety: {read_failure_count: 1.5}", "safety: {read_failure_timeout_seconds: 0}",
            "safety: {read_failure_timeout_seconds: .nan}", "safety: {read_failure_timeout_seconds: true}",
            "safety: {read_failure_level: 0}", "safety: {read_failure_level: 6}",
            "safety: {read_failure_level: true}", "safety: {read_failure_level: 3.5}",
            "safety: {startup_level: 3}", "safety: {recovery_delay_seconds: 60}",
            "safety: {recovery_success_count: 0}", "safety: {recovery_success_count: -1}",
            "safety: {recovery_success_count: true}", "safety: {recovery_success_count: 2.5}",
            "safety: {recovery_success_count: .nan}",
        ):
            with self.subTest(text=text), self.assertRaises(ConfigError):
                self.profile(text)

    def test_partial_override_and_new_component(self):
        c = self.profile("logging: {retention_days: 3}\ncurves: {gpu: [40, 50, 60, 70]}\n"
                         "sensors:\n  GPU_Temp: {curve: gpu, critical: true, emergency_celsius: 80, "
                         "minimum: 0, maximum: 120, reject_zero: false}\n")
        self.assertEqual(c["logging"]["retention_days"], 3)
        self.assertEqual(c["sensors"]["GPU_Temp"]["curve"], "gpu")
        self.assertIn("PCH_Temp", c["sensors"])

    def test_collection_defaults_and_existing_observe_profile(self):
        for c in (load_config(), self.profile("ipmi:\n  command_timeout_seconds: 20\n")):
            self.assertEqual(c["controller"]["poll_interval_seconds"], 15)
            self.assertEqual(c["ipmi"]["command_timeout_seconds"], 20)
            self.assertEqual(c["ipmi"]["status_timeout_seconds"], 3)
            self.assertEqual(c["safety"]["sensor_timeout_seconds"], 30)
            self.assertEqual(c["safety"]["recovery_success_count"], 5)
            self.assertNotIn("startup_level", c["safety"])
            self.assertNotIn("recovery_delay_seconds", c["safety"])
            self.assertEqual(c["safety"]["safe_level"], 5)
            self.assertEqual(c["safety"]["read_failure_count"], 5)
            self.assertEqual(c["safety"]["read_failure_timeout_seconds"], 300)
            self.assertEqual(c["safety"]["read_failure_level"], 3)
        self.assertEqual(self.profile("safety: {recovery_success_count: 2}")["safety"]["recovery_success_count"], 2)


class ParserTest(unittest.TestCase):
    def test_complete_table_and_exclusions(self):
        readings, errors = parse_sensors(SAMPLE)
        c = load_config()
        self.assertFalse(errors)
        self.assertEqual(len(readings), 22)
        for name in ("NVME_Temp", "RAID_Temp", "PSU2_Temp"):
            self.assertIsNotNone(validate_temperature(readings[name.casefold()], c["sensors"][name]))
        self.assertIsNone(validate_temperature(readings["pch_temp"], c["sensors"]["PCH_Temp"]))

    def test_malformed_empty_duplicate_and_nonfinite(self):
        for text in ("", "PCH_Temp | 60 | degrees C", "diagnostic text"):
            self.assertTrue(parse_sensors(text)[1])
        readings, errors = parse_sensors(SAMPLE + SAMPLE.splitlines()[4] + "\n")
        self.assertTrue(errors)
        self.assertEqual(readings["pch_temp"].error, "duplicate sensor name")
        for value in ("nan", "inf", "-inf", "na", "disabled"):
            row = f"PCH_Temp | {value} | degrees C | ok | na | na | na | na | na | na"
            self.assertIsNotNone(parse_sensors(row)[0]["pch_temp"].error)

    def test_alarm_is_still_a_valid_temperature(self):
        readings, _ = parse_sensors("PCH_Temp | 80 | degrees C | cr | na | na | na | 65 | 70 | 80")
        reading = readings["pch_temp"]
        self.assertIsNone(reading.error)
        self.assertEqual(reading.upper_critical, 70)
        self.assertEqual(reading.value, 80)


class HysteresisTest(unittest.TestCase):
    def setUp(self):
        c = load_config()
        self.settings = c["hysteresis"]
        self.curve = c["curves"]["pch"]

    def test_pch_instant_up_inclusive_margin_and_continuous_delay(self):
        h = Hysteresis()
        self.assertEqual(h.update(70, self.curve, 0, self.settings), 3)
        for t, value in ((10, 69), (20, 67.1), (30, 67), (89, 67)):
            self.assertEqual(h.update(value, self.curve, t, self.settings), 3)
        self.assertEqual(h.update(67, self.curve, 90, self.settings), 2)
        self.assertEqual(h.update(80, self.curve, 91, self.settings), 5)

    def test_rebound_and_invalid_sample_reset_timer(self):
        h = Hysteresis(level=3)
        h.update(67, self.curve, 0, self.settings)
        h.update(67.1, self.curve, 59, self.settings)
        h.update(67, self.curve, 60, self.settings)
        self.assertEqual(h.update(67, self.curve, 119, self.settings), 3)
        h.invalidate()
        self.assertEqual(h.update(67, self.curve, 121, self.settings), 3)
        self.assertEqual(h.update(67, self.curve, 181, self.settings), 2)

    def test_pch_returns_to_l1_at_62_but_holds_at_63_celsius(self):
        h = Hysteresis()
        self.assertEqual(h.update(65, self.curve, 0, self.settings), 2)
        self.assertEqual(h.update(63, self.curve, 10, self.settings), 2)
        self.assertIsNone(h.below_since)
        self.assertEqual(h.update(62.1, self.curve, 20, self.settings), 2)
        self.assertIsNone(h.below_since)
        self.assertEqual(h.update(62, self.curve, 30, self.settings), 2)
        self.assertEqual(h.update(62, self.curve, 89, self.settings), 2)
        self.assertEqual(h.update(62, self.curve, 90, self.settings), 1)


class EngineTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.readings, _ = parse_sensors(SAMPLE)
        # Exercise independent PCH demand above the observed low-load L1 baseline.
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=70)
        self.engine = ThermalEngine(self.config)

    def update(self, now, readings=None, sampled_at=None, errors=()):
        return self.engine.update(self.readings if readings is None else readings, now,
                                  now if sampled_at is None else sampled_at, errors)

    def healthy(self):
        for now in range(0, 61, 10):
            report = self.update(now)
        return report

    def test_first_valid_sample_decides_without_startup_hold(self):
        report = self.update(0)
        self.assertEqual(report["component_level"], 3)
        self.assertEqual(report["sensors"]["CPU1_Temp"]["requested_level"], 1)
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 3)
        self.assertEqual(report["policy_target_percent"], 30)
        self.assertEqual(report["safety_state"], "normal")
        report = self.healthy()
        self.assertEqual(report["policy_target_percent"], 30)
        self.assertEqual(report["safety_state"], "normal")
        self.assertFalse(report["applied"])

        low, _ = parse_sensors(SAMPLE)
        engine = ThermalEngine(self.config)
        for now in (0, 15, 30, 45, 59):
            report = engine.update(low, now, now)
            self.assertEqual(report["recommended_level"], 1)
            self.assertEqual(report["safety_state"], "normal")
        self.assertEqual(engine.update(low, 60, 60)["recommended_level"], 1)
        self.assertEqual(ThermalEngine(self.config).update(low, 75, 75)["recommended_level"], 1)

        for temperature, expected in ((75, 4), (80, 5)):
            hot = {**low, "pch_temp": replace(low["pch_temp"], value=temperature)}
            report = ThermalEngine(self.config).update(hot, 0, 0)
            self.assertEqual(report["recommended_level"], expected)
        stopped = {**low, "fan1_r_speed": replace(low["fan1_r_speed"], value=0)}
        report = ThermalEngine(self.config).update(stopped, 0, 0)
        self.assertEqual(report["recommended_level"], 5)
        self.assertEqual(report["safety_state"], "safe-mode")

    def test_component_curves_and_fractional_boundaries(self):
        for curve_name, thresholds in self.config["curves"].items():
            for index, threshold in enumerate(thresholds, 2):
                with self.subTest(curve=curve_name, threshold=threshold):
                    self.assertEqual(Hysteresis().update(threshold, thresholds, 0, self.config["hysteresis"]), index)
                    self.assertEqual(Hysteresis().update(threshold - 0.1, thresholds, 0, self.config["hysteresis"]), index - 1)

    def test_missing_critical_sensor_recovers_after_five_healthy_samples(self):
        self.healthy()
        missing = dict(self.readings)
        missing.pop("pch_temp")
        report = self.update(70, missing)
        self.assertEqual(report["recommended_level"], 5)
        self.assertIn("PCH_Temp", report["sensor_errors"])
        for now in (80, 90, 100, 110):
            self.assertEqual(self.update(now)["recommended_level"], 5)
        self.assertEqual(self.update(120)["recommended_level"], 3)

    def test_stale_data_and_gaps_are_not_cooldown_evidence(self):
        self.healthy()
        report = self.update(100, sampled_at=60)
        self.assertEqual(report["recommended_level"], 3)
        self.assertEqual(report["safety_state"], "read-failure-hold")
        self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=50)
        self.update(110)
        report = self.update(180)
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 3)
        self.assertEqual(report["safety_state"], "recovery")

    def test_uncurved_emergency_and_curved_bmc_alarms(self):
        self.healthy()
        self.readings["inlet_temp"] = replace(self.readings["inlet_temp"], upper_warning=27)
        self.assertEqual(self.update(70)["safety_state"], "emergency")
        self.readings["inlet_temp"] = replace(self.readings["inlet_temp"], upper_warning=None)
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=62, upper_warning=61)
        report = self.update(80)
        self.assertIn("PCH_Temp: BMC threshold alarm", report["bmc_alarms"])
        self.assertEqual(report["emergencies"], [])
        self.assertEqual(report["recovery"]["healthy_samples"], 0)
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=999, status="cr")
        report = self.update(90)
        self.assertEqual(report["recommended_level"], 5)
        self.assertIn("PCH_Temp: BMC threshold alarm", report["bmc_alarms"])
        self.assertEqual(report["safety_state"], "safe-mode")

    def test_fan_faults_unknown_thermal_and_trends(self):
        self.update(0)
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=71)
        report = self.update(10)
        self.assertEqual(report["sensors"]["PCH_Temp"]["trend_celsius_per_minute"], 6)
        for key in ("fan1_r_speed", "fan2_r_speed"):
            self.readings[key] = replace(self.readings[key], value=0)
        self.readings.pop("fan3_r_speed")
        self.readings["gpu_temp"] = replace(self.readings["pch_temp"], name="GPU_Temp")
        report = self.update(20)
        self.assertTrue(any("FAN1" in f for f in report["faults"]))
        self.assertTrue(any("FAN3" in f for f in report["faults"]))
        self.assertTrue(any("GPU_Temp" in f for f in report["faults"]))
        self.assertEqual(report["recommended_level"], 5)


class IPMITest(unittest.TestCase):
    @patch.object(ReadOnlyIPMI, "_execute")
    def test_only_allowlisted_reads_and_separate_budgets(self, execute):
        ipmi = ReadOnlyIPMI(20, 3)
        ipmi.sensors()
        ipmi.control_status()
        self.assertEqual([call.args[0] for call in execute.call_args_list], [
            ("ipmitool", "-I", "open", "sensor"),
            ("ipmitool", "-I", "open", "raw", "0x2e", "0x31")])
        self.assertEqual([call.args[1] for call in execute.call_args_list], [20, 3])
        with self.assertRaises(ValueError):
            ipmi._read(("raw", "0x2e", "0x30", "0", "0", "15"))
        self.assertEqual(execute.call_count, 2)

    def test_status_bytes_are_not_percentages(self):
        self.assertEqual(parse_control_status("20 3c 3c 3c 3c 3c 3c 3c 3c")[1], "0x3c")
        for text in ("", "20 3c", "20 gg 32 32 32 32 32 32 32"):
            with self.assertRaises(ValueError):
                parse_control_status(text)

    def test_alert_precedes_readback_and_status_failure_holds(self):
        c = load_config()
        engine = ThermalEngine(c)
        order = []

        class FakeIPMI:
            def sensors(self):
                alarm = SAMPLE.replace("60.000     | degrees C  | ok", "80.000     | degrees C  | unc")
                return CommandResult(("sensor",), alarm, "", 0, None, 0.1)

            def control_status(self):
                order.append("status")
                return CommandResult(("raw",), "", "", 0, None, 0.1)

        report = collect(FakeIPMI(), engine, c, on_alert=lambda _: order.append("alert"))
        self.assertEqual(order, ["alert", "status"])
        self.assertEqual(report["recommended_level"], 5)
        self.assertTrue(any("BMC status" in f for f in report["faults"]))

    @patch.object(ReadOnlyIPMI, "_execute")
    def test_nonzero_result_is_not_success(self, execute):
        execute.return_value = CommandResult(("sensor",), SAMPLE, "BMC unavailable", 1,
                                             "ipmitool exit 1", 0.1)
        c = load_config()
        report = collect(ReadOnlyIPMI(), ThermalEngine(c), c)
        self.assertEqual(report["safety_state"], "waiting-for-sample")
        self.assertIsNone(report["recommended_level"])
        self.assertIsNone(report["policy_target_percent"])
        self.assertIsNone(report["component_level"])
        self.assertTrue(report["faults"])
        self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])
        self.assertTrue(all(fan["rpm"] is None for fan in report["fans"].values()))

    @patch.object(ReadOnlyIPMI, "_execute")
    def test_failed_status_cannot_lower_previous_recommendation(self, execute):
        c = load_config()
        c["safety"]["safe_level"] = 2
        engine = ThermalEngine(c)
        engine.safety.previous_level = 5
        execute.side_effect = [CommandResult(("sensor",), SAMPLE, "", 0, None, 0.1),
                               CommandResult(("raw",), "", "readback failed", 1, "ipmitool exit 1", 0.1)]
        report = collect(ReadOnlyIPMI(), engine, c)
        self.assertEqual(report["recommended_level"], 5)


class LoggingAndCLITest(unittest.TestCase):
    def test_all_curve_and_uncurved_sources_emit_same_l5_notice(self):
        cases = (("CPU1_Temp", 80, False), ("PCH_Temp", 80, False),
                 ("DIMMG1_Temp", 70, False), ("CPU1_VR_Temp", 100, False),
                 ("NIC_Temp", 85, False), ("PSU1_Temp", 60, False),
                 ("Outlet_Temp", 65, False), ("Inlet_Temp", 42, True))
        with tempfile.TemporaryDirectory() as directory:
            snapshot = Path(directory) / "sample.txt"
            for name, temperature, bmc_alarm in cases:
                with self.subTest(sensor=name):
                    rows = []
                    for line in SAMPLE.splitlines():
                        fields = [field.strip() for field in line.split("|")]
                        if fields[0] == name:
                            fields[1] = str(temperature)
                            if bmc_alarm:
                                fields[7] = str(temperature)
                        rows.append(" | ".join(fields))
                    snapshot.write_text("\n".join(rows))
                    output, errors = io.StringIO(), io.StringIO()
                    with redirect_stdout(output), redirect_stderr(errors):
                        result = main(["--once", "--input", str(snapshot), "--no-log", "--json"])
                    report = json.loads(output.getvalue())
                    self.assertEqual(result, 2)
                    self.assertEqual(report["recommended_level"], 5)
                    self.assertEqual(bool(report["bmc_alarms"]), bmc_alarm)
                    self.assertEqual(errors.getvalue(), L5_ALERT + "\n")
                    self.assertNotIn(L5_ALERT, render(report))
                    self.assertFalse(report["applied"])

    def test_l5_notice_once_per_entry_in_real_sampling_loop(self):
        stop, clock, records, polls = threading.Event(), [0.0], [], [0]
        hot = SAMPLE.replace("60.000", "80.000")
        changed_alarm = hot.replace("80.000     | degrees C  | ok", "80.000     | degrees C  | unc")
        cpu_hot = SAMPLE.replace("44.000", "80.000")
        samples = [hot, changed_alarm] + [SAMPLE] * 5 + [cpu_hot]

        def execute(command, timeout):
            if command[-1] == "sensor":
                text = samples[polls[0]]
                polls[0] += 1
                return CommandResult(command, text, "", 0, None, 0.1, timeout)
            return CommandResult(command, "20 0f 0f 0f 0f 0f 0f 0f 0f", "", 0, None, 0.1, timeout)

        def wait(delay):
            clock[0] += delay
            if polls[0] == len(samples):
                stop.set()

        output, errors = io.StringIO(), io.StringIO()
        with patch("hrx_fancontrol.main.threading.Event", return_value=stop), \
                patch("hrx_fancontrol.main.AsyncLogger") as logger, \
                patch("hrx_fancontrol.main.time.monotonic", side_effect=lambda: clock[0]), \
                patch.object(stop, "wait", side_effect=wait), \
                patch.object(ReadOnlyIPMI, "_execute", side_effect=execute), \
                redirect_stdout(output), redirect_stderr(errors):
            logger.return_value.dropped, logger.return_value.error = 0, None
            logger.return_value.emit.side_effect = lambda record: records.append(json.loads(json.dumps(record)))
            result = main(["--json"])
        reports = [json.loads(line) for line in output.getvalue().splitlines()]
        self.assertEqual(result, 0)  # Requested stop.
        self.assertEqual([report["recommended_level"] for report in reports], [5] * 6 + [4, 5])
        self.assertEqual(errors.getvalue().splitlines(), [L5_ALERT, L5_ALERT])
        self.assertNotIn(L5_ALERT, "\n".join(render(report) for report in reports))
        alerts = [record for record in records if record["event"] == "safety_alert"]
        self.assertEqual([record["poll_id"] for record in alerts], [1, 8])
        self.assertTrue(all(record["collection"]["status"] == "in-progress" for record in alerts))
        self.assertTrue(all("recovery" not in record for record in alerts))
        self.assertTrue(reports[1]["bmc_alarms"])
        self.assertTrue(any(record.get("poll_id") == 2 and record["event"] == "transition"
                            and record["bmc_alarms"] for record in records))
        self.assertFalse(any(record["applied"] for record in records))

    @patch("hrx_fancontrol.ipmi.subprocess.Popen", side_effect=AssertionError("offline mode started IPMI"))
    def test_advisory_bmc_alarm_remains_diagnostic_without_notification(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "sample.txt"
            snapshot.write_text(SAMPLE.replace("60.000     | degrees C  | ok",
                                               "60.000     | degrees C  | unc"))
            profile = root / "profile.yaml"
            profile.write_text(f"logging:\n  directory: {root / 'logs'}\n")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                result = main(["--once", "--input", str(snapshot), "--config", str(profile), "--json"])
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            message = "PCH_Temp: BMC threshold alarm"
            self.assertEqual(report["bmc_alarms"], [message])
            self.assertEqual(report["faults"], [])
            self.assertEqual(report["emergencies"], [])
            self.assertEqual(report["recommended_level"], 1)
            self.assertEqual(report["safety_state"], "normal")
            self.assertEqual(report["collection"]["status"], "ok")
            self.assertFalse(report["recovery"]["holding"])
            self.assertNotIn(message, render(report))
            self.assertEqual(errors.getvalue(), "")
            records = [json.loads(line) for path in (root / "logs").glob("*.jsonl")
                       for line in path.read_text().splitlines()]
            self.assertEqual([record["event"] for record in records], ["transition", "shutdown"])
            self.assertEqual(records[0]["collection"]["status"], "ok")
            self.assertTrue(all(record["bmc_alarms"] == [message] for record in records[:-1]))
            self.assertNotEqual(transition_key(report), transition_key({**report, "bmc_alarms": []}))
            self.assertFalse(any(record["applied"] for record in records))
            popen.assert_not_called()

    def test_daily_retention_including_restart_and_other_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in ("hrx-2026-11-03.jsonl", "hrx-2026-11-04.jsonl", "keep.txt", "hrx-invalid.jsonl"):
                (root / name).write_text("keep\n")
            writer = DailyWriter(root, 7)
            writer.write({"event": "summary"}, datetime(2026, 11, 10, tzinfo=timezone.utc))
            self.assertFalse((root / "hrx-2026-11-03.jsonl").exists())
            self.assertTrue((root / "hrx-2026-11-04.jsonl").exists())
            self.assertTrue((root / "keep.txt").exists())
            self.assertTrue((root / "hrx-invalid.jsonl").exists())
            writer.write({"event": "summary"}, datetime(2026, 11, 11, tzinfo=timezone.utc))
            self.assertFalse((root / "hrx-2026-11-04.jsonl").exists())

    def test_async_logger_flushes_and_reports_disk_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            settings = {**load_config()["logging"], "directory": directory}
            logger = AsyncLogger(settings)
            logger.emit({"event": "test"})
            logger.close()
            records = [json.loads(line) for p in Path(directory).glob("*.jsonl") for line in p.read_text().splitlines()]
            self.assertEqual(records, [{"event": "test"}])
            self.assertIsNone(logger.error)
            with patch.object(DailyWriter, "write", side_effect=OSError("disk full")), redirect_stderr(io.StringIO()):
                logger = AsyncLogger(settings)
                logger.emit({"event": "test"})
                logger.close()
            self.assertEqual(logger.error, "disk full")

    @patch("hrx_fancontrol.ipmi.subprocess.Popen", side_effect=AssertionError("offline mode started IPMI"))
    def test_offline_cli_end_to_end_without_hardware(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text(SAMPLE)
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["--once", "--input", str(path), "--json", "--no-log"])
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["component_level"], 1)
            self.assertEqual(report["recommended_level"], 1)
            self.assertEqual(report["safety_state"], "normal")
            self.assertNotIn("recommended_percent", report)
            self.assertEqual(report["policy_target_percent"], 15)
            self.assertEqual(report["calibration"], {
                "status": "unverified", "ipmi_argument": None, "expected_rpm": None})
            self.assertIsNone(report["bmc"]["percent"])
            self.assertIn("uncalibrated", render(report))
            self.assertEqual(report["commands"], [])
            self.assertFalse(report["applied"])
            popen.assert_not_called()

    @patch("hrx_fancontrol.ipmi.subprocess.Popen", side_effect=AssertionError("offline mode started IPMI"))
    def test_offline_invalid_data_reports_null_recommendation_and_logs_it(self, popen):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            snapshot = root / "sample.txt"
            snapshot.write_text("malformed sensor table")
            config = root / "profile.yaml"
            config.write_text(f"logging:\n  directory: {root / 'logs'}\n")
            output, errors = io.StringIO(), io.StringIO()
            with redirect_stdout(output), redirect_stderr(errors):
                result = main(["--once", "--input", str(snapshot), "--config", str(config), "--json"])
            self.assertEqual(result, 2)
            report = json.loads(output.getvalue())
            for field in ("component_level", "recommended_level", "policy_target_percent"):
                self.assertIsNone(report[field])
            self.assertEqual(report["safety_state"], "waiting-for-sample")
            self.assertIn("Waiting for valid sensor data", render(report))
            self.assertEqual(errors.getvalue(), "")
            self.assertTrue(report["faults"])
            self.assertIn("Diagnostic:", render(report))
            records = [json.loads(line) for path in (root / "logs").glob("*.jsonl")
                       for line in path.read_text().splitlines()]
            self.assertEqual([record["event"] for record in records], ["transition", "shutdown"])
            self.assertTrue(all(record["recommended_level"] is None for record in records[:-1]))
            self.assertFalse(any(record["applied"] for record in records))
            popen.assert_not_called()

    @patch.object(ReadOnlyIPMI, "_execute")
    def test_actual_cli_collection_is_read_only(self, execute):
        execute.side_effect = [CommandResult(("sensor",), SAMPLE, "", 0, None, 0.1),
                               CommandResult(("raw",), "20 32 32 32 32 32 32 32 32", "", 0, None, 0.1)]
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--once", "--no-log", "--json"])
        self.assertEqual(result, 0)
        self.assertEqual(execute.call_count, 2)
        # Even a plausible readback must not be decoded into a percentage or RPM prediction.
        report = json.loads(output.getvalue())
        self.assertEqual(report["bmc"]["raw_bytes"], ["0x20"] + ["0x32"] * 8)
        self.assertIsNone(report["bmc"]["percent"])
        self.assertEqual(report["calibration"], {
            "status": "unverified", "ipmi_argument": None, "expected_rpm": None})
        self.assertEqual(report["fans"]["FAN1_R_Speed"]["rpm"], 3335)
        self.assertEqual([call.args[0] for call in execute.call_args_list], [
            ("ipmitool", "-I", "open", "sensor"),
            ("ipmitool", "-I", "open", "raw", "0x2e", "0x31")])


if __name__ == "__main__":
    unittest.main()
