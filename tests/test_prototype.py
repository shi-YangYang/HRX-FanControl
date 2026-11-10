import io
import json
import subprocess
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

from hrx_fancontrol.config import ConfigError, load_config
from hrx_fancontrol.hysteresis import Hysteresis
from hrx_fancontrol.ipmi import CommandResult, ReadOnlyIPMI, parse_control_status
from hrx_fancontrol.logger import AsyncLogger, DailyWriter
from hrx_fancontrol.main import collect, main, render
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

    def test_pch_dominates_and_startup_holds_then_recovers(self):
        report = self.update(0)
        self.assertEqual(report["component_level"], 3)
        self.assertEqual(report["sensors"]["CPU1_Temp"]["requested_level"], 1)
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 3)
        self.assertEqual(report["policy_target_percent"], 60)
        report = self.healthy()
        self.assertEqual(report["policy_target_percent"], 30)
        self.assertEqual(report["safety_state"], "normal")
        self.assertFalse(report["applied"])

    def test_component_curves_and_fractional_boundaries(self):
        for curve_name, thresholds in self.config["curves"].items():
            for index, threshold in enumerate(thresholds, 2):
                with self.subTest(curve=curve_name, threshold=threshold):
                    self.assertEqual(Hysteresis().update(threshold, thresholds, 0, self.config["hysteresis"]), index)
                    self.assertEqual(Hysteresis().update(threshold - 0.1, thresholds, 0, self.config["hysteresis"]), index - 1)

    def test_missing_critical_sensor_holds_and_recovers_after_delay(self):
        self.healthy()
        missing = dict(self.readings)
        missing.pop("pch_temp")
        report = self.update(70, missing)
        self.assertEqual(report["recommended_level"], 5)
        self.assertIn("PCH_Temp", report["sensor_errors"])
        for now in range(80, 140, 10):
            self.assertEqual(self.update(now)["recommended_level"], 5)
        self.assertEqual(self.update(140)["recommended_level"], 3)

    def test_stale_data_and_gaps_are_not_cooldown_evidence(self):
        self.healthy()
        report = self.update(100, sampled_at=60)
        self.assertEqual(report["recommended_level"], 5)
        self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=50)
        self.update(110)
        report = self.update(180)
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 3)
        self.assertEqual(report["safety_state"], "safe-mode")

    def test_emergency_bypasses_hold_and_honors_bmc_thresholds(self):
        self.healthy()
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=80)
        self.assertEqual(self.update(70)["safety_state"], "emergency")
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=62, upper_warning=61)
        self.assertIn("PCH_Temp: BMC threshold alarm", self.update(80)["emergencies"])
        self.readings["pch_temp"] = replace(self.readings["pch_temp"], value=999, status="cr")
        self.assertEqual(self.update(90)["recommended_level"], 5)

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
    @patch("hrx_fancontrol.ipmi.subprocess.run")
    def test_only_allowlisted_reads_and_timeout(self, run):
        run.return_value = subprocess.CompletedProcess([], 0, "20 32 32 32 32 32 32 32 32", "")
        ipmi = ReadOnlyIPMI(3)
        ipmi.sensors()
        ipmi.control_status()
        self.assertEqual([call.args[0] for call in run.call_args_list], [
            ("ipmitool", "-I", "open", "sensor"),
            ("ipmitool", "-I", "open", "raw", "0x2e", "0x31")])
        with self.assertRaises(ValueError):
            ipmi._read(("raw", "0x2e", "0x30", "0", "0", "15"))
        run.side_effect = subprocess.TimeoutExpired("ipmitool", 3, output=b"partial")
        result = ipmi.sensors()
        self.assertEqual(result.error, "IPMI timeout")
        self.assertEqual(result.stdout, "")

    def test_status_bytes_are_not_percentages(self):
        self.assertEqual(parse_control_status("20 3c 3c 3c 3c 3c 3c 3c 3c")[1], "0x3c")
        for text in ("", "20 3c", "20 gg 32 32 32 32 32 32 32"):
            with self.assertRaises(ValueError):
                parse_control_status(text)

    def test_alert_precedes_readback_and_status_failure_holds(self):
        c = load_config()
        c["safety"]["recovery_delay_seconds"] = 0
        engine = ThermalEngine(c)
        order = []

        class FakeIPMI:
            def sensors(self):
                return CommandResult(("sensor",), SAMPLE.replace("60.000", "80.000"), "", 0, None, 0.1)

            def control_status(self):
                order.append("status")
                return CommandResult(("raw",), "", "", 0, None, 0.1)

        report = collect(FakeIPMI(), engine, c, on_alert=lambda _: order.append("alert"))
        self.assertEqual(order, ["alert", "status"])
        self.assertEqual(report["recommended_level"], 5)
        self.assertTrue(any("BMC status" in f for f in report["faults"]))

    @patch("hrx_fancontrol.ipmi.subprocess.run")
    def test_nonzero_result_is_not_success(self, run):
        run.return_value = subprocess.CompletedProcess([], 1, SAMPLE, "BMC unavailable")
        c = load_config()
        report = collect(ReadOnlyIPMI(), ThermalEngine(c), c)
        self.assertEqual(report["safety_state"], "safe-mode")
        self.assertEqual(report["recommended_level"], 5)
        self.assertTrue(report["faults"])

    @patch("hrx_fancontrol.ipmi.subprocess.run")
    def test_failed_status_cannot_lower_previous_recommendation(self, run):
        c = load_config()
        c["safety"]["safe_level"] = 2
        c["safety"]["recovery_delay_seconds"] = 0
        engine = ThermalEngine(c)
        engine.safety.previous_level = 5
        run.side_effect = [subprocess.CompletedProcess([], 0, SAMPLE, ""),
                           subprocess.CompletedProcess([], 1, "", "readback failed")]
        report = collect(ReadOnlyIPMI(), engine, c)
        self.assertEqual(report["recommended_level"], 5)


class LoggingAndCLITest(unittest.TestCase):
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

    @patch("hrx_fancontrol.ipmi.subprocess.run", side_effect=AssertionError("offline mode started IPMI"))
    def test_offline_cli_end_to_end_without_hardware(self, run):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "sample.txt"
            path.write_text(SAMPLE)
            output = io.StringIO()
            with redirect_stdout(output):
                result = main(["--once", "--input", str(path), "--json", "--no-log"])
            self.assertEqual(result, 0)
            report = json.loads(output.getvalue())
            self.assertEqual(report["component_level"], 1)
            self.assertEqual(report["recommended_level"], 5)
            self.assertNotIn("recommended_percent", report)
            self.assertEqual(report["policy_target_percent"], 60)
            self.assertEqual(report["calibration"], {
                "status": "unverified", "ipmi_argument": None, "expected_rpm": None})
            self.assertIsNone(report["bmc"]["percent"])
            self.assertIn("uncalibrated", render(report))
            self.assertEqual(report["commands"], [])
            self.assertFalse(report["applied"])
            run.assert_not_called()

    @patch("hrx_fancontrol.ipmi.subprocess.run")
    def test_actual_cli_collection_is_read_only(self, run):
        run.side_effect = [subprocess.CompletedProcess([], 0, SAMPLE, ""),
                           subprocess.CompletedProcess([], 0, "20 32 32 32 32 32 32 32 32", "")]
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--once", "--no-log", "--json"])
        self.assertEqual(result, 0)
        self.assertEqual(run.call_count, 2)
        # Even a plausible readback must not be decoded into a percentage or RPM prediction.
        report = json.loads(output.getvalue())
        self.assertEqual(report["bmc"]["raw_bytes"], ["0x20"] + ["0x32"] * 8)
        self.assertIsNone(report["bmc"]["percent"])
        self.assertEqual(report["calibration"], {
            "status": "unverified", "ipmi_argument": None, "expected_rpm": None})
        self.assertEqual(report["fans"]["FAN1_R_Speed"]["rpm"], 3335)
        self.assertTrue(all("0x30" not in call.args[0] for call in run.call_args_list))


if __name__ == "__main__":
    unittest.main()
