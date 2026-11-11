"""Communication failures hold recommendations without suppressing real hardware alarms."""
import json
import threading
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hrx_fancontrol.collection import CollectionStats, collect
from hrx_fancontrol.config import load_config
from hrx_fancontrol.ipmi import CollectionCancelled, CommandResult
from hrx_fancontrol.main import render
from hrx_fancontrol.safety import Safety
from hrx_fancontrol.sensors import parse_sensors
from hrx_fancontrol.thermal_engine import ThermalEngine


SAMPLE = (Path(__file__).resolve().parents[1] / "examples/hr650x-sensors.txt").read_text()
STATUS = "20 0f 0f 0f 0f 0f 0f 0f 0f"


class ReadFailurePolicyTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.engine = ThermalEngine(self.config)
        self.stats = CollectionStats()
        self.alerts = []
        self.calls = []
        self.now = 0

    def poll(self, now, *, sensor="good", status="good", text=SAMPLE, stop=None):
        self.now = now
        calls = self.calls

        class FakeIPMI:
            def sensors(self):
                calls.append("sensor")
                if sensor == "timeout":
                    return CommandResult(("sensor",), "", "", None, "IPMI timeout", 20,
                                         failure_kind="timeout")
                if sensor == "parse":
                    return CommandResult(("sensor",), text + "\nbroken\n", "", 0, None, 0.1)
                return CommandResult(("sensor",), text, "", 0, None, 0.1)

            def control_status(self):
                calls.append("status")
                if status == "cancel":
                    stop.set()
                if status == "timeout":
                    return CommandResult(("status",), "", "", None, "IPMI timeout", 3,
                                         failure_kind="timeout")
                if status == "parse":
                    return CommandResult(("status",), "broken", "", 0, None, 0.1)
                return CommandResult(("status",), STATUS, "", 0, None, 0.1)

        def alert(report):
            calls.append("alert")
            self.alerts.append(json.loads(json.dumps(report)))

        with patch("hrx_fancontrol.collection.time.monotonic", return_value=now):
            return collect(FakeIPMI(), self.engine, self.config, on_alert=alert, stop=stop, stats=self.stats)

    def warm(self):
        for now in (0, 15, 30, 45, 60):
            report = self.poll(now)
        self.assertEqual(report["recommended_level"], 1)
        self.alerts.clear()
        self.calls.clear()

    def test_fifth_failure_degrades_and_continuous_recovery_releases(self):
        self.warm()
        for count, now in enumerate((75, 110, 145, 180, 215), 1):
            report = self.poll(now, sensor="timeout")
            self.assertEqual(report["read_failure"]["consecutive_failures"], count)
            self.assertEqual(report["recommended_level"], 1 if count < 5 else 3)
            self.assertEqual(report["safety_state"], "read-failure-hold" if count < 5 else "read-failure-degraded")
            self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])
            self.assertTrue(all(fan["rpm"] is None for fan in report["fans"].values()))
            self.assertFalse(report["applied"])
            self.assertEqual(report["calibration"]["status"], "unverified")
        self.assertEqual(report["read_failure"]["trigger_reasons"], ["failure-count"])
        self.assertEqual(report["read_failure"]["no_valid_sample_seconds"], 155)
        self.assertIn("Read failures: 5/5", render(report))
        self.assertIn("failure-count", render(report))
        # The optional raw status succeeded in every failed poll; it must not refresh temperatures.
        for now in (230, 245, 260, 275):
            report = self.poll(now)
            self.assertEqual(report["read_failure"]["consecutive_failures"], 0)
            self.assertEqual(report["recommended_level"], 3)
            self.assertEqual(report["safety_state"], "recovery")
        report = self.poll(290)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["read_failure"]["trigger_reasons"], [])

    def test_success_resets_streak_but_a_new_failure_restarts_healthy_hold(self):
        self.warm()
        self.poll(75, sensor="timeout")
        recovered = self.poll(90)
        self.assertEqual(recovered["read_failure"]["consecutive_failures"], 0)
        self.assertEqual(recovered["safety_state"], "recovery")
        self.poll(105)
        failed = self.poll(120, sensor="parse")
        self.assertEqual(failed["read_failure"]["consecutive_failures"], 1)
        self.assertEqual(failed["recommended_level"], 1)
        for now in (135, 150, 165, 180):
            self.assertEqual(self.poll(now)["safety_state"], "recovery")
        self.assertEqual(self.poll(195)["safety_state"], "normal")

    def test_299_and_300_seconds_without_samples_trigger_independently_of_count(self):
        self.config["safety"]["read_failure_count"] = 100
        self.warm()
        report = self.poll(359, sensor="timeout")
        self.assertEqual(report["read_failure"]["no_valid_sample_seconds"], 299)
        self.assertEqual(report["recommended_level"], 1)
        report = self.poll(360, sensor="timeout")
        self.assertEqual(report["read_failure"]["consecutive_failures"], 2)
        self.assertEqual(report["read_failure"]["no_valid_sample_seconds"], 300)
        self.assertEqual(report["recommended_level"], 3)
        self.assertEqual(report["read_failure"]["trigger_reasons"], ["no-valid-sample-timeout"])

    def test_first_fresh_sample_after_long_silence_cannot_bypass_recovery(self):
        self.warm()
        report = self.poll(360)
        self.assertEqual(report["read_failure"]["consecutive_failures"], 0)
        self.assertEqual(report["read_failure"]["no_valid_sample_seconds"], 0)
        self.assertEqual(report["recommended_level"], 3)
        self.assertEqual(report["read_failure"]["trigger_reasons"], ["no-valid-sample-timeout"])
        self.assertTrue(report["collection"]["sensor_data_available"])
        self.assertEqual(self.alerts, [])
        for now in (375, 390, 405):
            self.assertEqual(self.poll(now)["recommended_level"], 3)
        self.assertEqual(self.poll(420)["recommended_level"], 1)

    def test_status_failure_keeps_temperatures_and_immediate_temperature_increases(self):
        self.warm()
        for count, now in enumerate((75, 90, 105, 120, 135), 1):
            report = self.poll(now, status="timeout")
            self.assertEqual(report["read_failure"]["consecutive_failures"], count)
            self.assertEqual(report["read_failure"]["no_valid_sample_seconds"], 0)
            self.assertEqual(report["sensors"]["PCH_Temp"]["celsius"], 60)
            self.assertEqual(report["recommended_level"], 1 if count < 5 else 3)
        hotter = SAMPLE.replace("60.000", "75.000")
        report = self.poll(150, status="parse", text=hotter)
        self.assertEqual(report["recommended_level"], 4)
        self.assertEqual(report["component_level"], 4)

    def test_both_commands_fail_once_and_cancel_never_commits_policy(self):
        self.warm()
        report = self.poll(75, sensor="timeout", status="timeout")
        self.assertEqual(report["read_failure"]["consecutive_failures"], 1)
        self.assertEqual(report["collection"]["counts"]["failed"], 1)
        self.assertEqual(report["collection"]["counts"]["timed_out"], 1)
        stop = threading.Event()
        with self.assertRaises(CollectionCancelled):
            self.poll(90, status="cancel", stop=stop)
        self.assertEqual(self.engine.safety.consecutive_failures, 1)
        self.assertEqual(self.engine.safety.last_valid_sample_at, 60)
        self.assertEqual(self.engine.last_update, 75)
        self.assertEqual(self.stats.cancelled, 1)

    def test_prior_l2_l4_l5_and_startup_are_not_lowered_by_failure(self):
        for temperature, expected in ((65, 3), (75, 4), (80, 5)):
            with self.subTest(temperature=temperature):
                self.setUp()
                self.warm()
                report = self.poll(75, text=SAMPLE.replace("60.000", f"{temperature}.000"))
                old_level = report["recommended_level"]
                for now in (90, 105, 120, 135):
                    self.assertEqual(self.poll(now, sensor="timeout")["recommended_level"], old_level)
                self.assertEqual(self.poll(150, sensor="timeout")["recommended_level"], expected)
        self.setUp()
        for now in (20, 55, 90, 125):
            self.assertIsNone(self.poll(now, sensor="timeout")["recommended_level"])
        self.assertEqual(self.poll(160, sensor="timeout")["recommended_level"], 3)

    def test_known_hardware_faults_still_protect_before_readback(self):
        for sample, expected_state in (
            (SAMPLE.replace("27.000     | degrees C  | ok", "27.000     | degrees C  | unc"), "emergency"),
            (SAMPLE.replace("3335.000", "0.000"), "safe-mode"),
            ("\n".join(line for line in SAMPLE.splitlines() if not line.startswith("PCH_Temp")), "safe-mode"),
        ):
            with self.subTest(state=expected_state):
                self.setUp()
                self.warm()
                report = self.poll(75, text=sample, status="timeout")
                self.assertEqual(report["recommended_level"], 5)
                self.assertEqual(report["safety_state"], expected_state)
                self.assertEqual(report["read_failure"]["consecutive_failures"], 1)
                self.assertEqual(self.calls[:3], ["sensor", "alert", "status"])
                self.assertEqual(self.alerts[0]["recommended_level"], 5)

    def test_advisory_bmc_alarm_is_not_a_failure_or_healthy_recovery(self):
        self.warm()
        alarm = SAMPLE.replace("60.000     | degrees C  | ok", "60.000     | degrees C  | unc")
        report = self.poll(75, text=alarm)
        self.assertEqual(self.calls, ["sensor", "status"])
        self.assertEqual(self.alerts, [])
        self.assertEqual(report["collection"]["status"], "ok")
        self.assertEqual(report["collection"]["counts"]["failed"], 0)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["safety_state"], "normal")
        self.assertFalse(report["recovery"]["holding"])
        self.assertEqual(report["bmc_alarms"], ["PCH_Temp: BMC threshold alarm"])
        self.assertEqual(report["emergencies"], [])

        self.poll(90, text=SAMPLE.replace("3335.000", "0.000"))
        for count, now in enumerate((105, 120), 1):
            self.assertEqual(self.poll(now)["recovery"]["healthy_samples"], count)
        report = self.poll(135, text=alarm)
        self.assertEqual(report["recommended_level"], 5)
        self.assertEqual(report["recovery"]["healthy_samples"], 0)
        self.assertEqual(report["read_failure"]["consecutive_failures"], 0)
        for count, now in enumerate((150, 165, 180, 195), 1):
            report = self.poll(now)
            self.assertEqual(report["recommended_level"], 5)
            self.assertEqual(report["recovery"]["healthy_samples"], count)
        self.assertEqual(self.poll(210)["recommended_level"], 1)

        # Partial output from a rejected table cannot supply a BMC alarm.
        report = self.poll(225, text=alarm, sensor="parse")
        self.assertEqual(report["bmc_alarms"], [])
        self.assertEqual(report["emergencies"], [])

    def test_missing_critical_sensor_does_not_discard_known_higher_demand(self):
        self.config["safety"]["safe_level"] = 2
        sample = "\n".join(line for line in SAMPLE.replace("60.000", "75.000").splitlines()
                           if not line.startswith("CPU1_Temp"))
        report = self.poll(0, text=sample)
        self.assertIsNone(report["component_level"])
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 4)
        self.assertEqual(report["recommended_level"], 4)
        self.assertEqual(report["safety_state"], "safe-mode")
        self.assertEqual(self.alerts, [])
        self.assertEqual(report["recovery"]["healthy_samples"], 0)
        # A rejected table cannot provide the same temperature evidence.
        self.setUp()
        report = self.poll(0, text=sample, sensor="parse")
        self.assertIsNone(report["recommended_level"])
        self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])

    def test_failed_status_cannot_supply_downshift_evidence(self):
        self.warm()
        self.poll(75, text=SAMPLE.replace("60.000", "65.000"))
        for now in (90, 105, 120, 135):
            self.assertEqual(self.poll(now)["component_level"], 2)
        # The existing cooldown would expire now, but this poll's readback fails.
        report = self.poll(150, status="timeout")
        self.assertEqual(report["component_level"], 2)
        self.assertEqual(report["recommended_level"], 2)
        for now in (165, 180, 195, 210):
            self.assertEqual(self.poll(now)["component_level"], 2)
        self.assertEqual(self.poll(225)["recommended_level"], 1)

    def test_stale_sample_never_refreshes_last_valid_or_uses_rpm(self):
        self.warm()
        readings, _ = parse_sensors(SAMPLE)
        report = self.engine.update(readings, 100, 60)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["read_failure"]["consecutive_failures"], 1)
        self.assertEqual(report["read_failure"]["no_valid_sample_seconds"], 40)
        self.assertIsNone(report["sensors"]["PCH_Temp"]["celsius"])
        self.assertTrue(all(fan["rpm"] is None for fan in report["fans"].values()))
        # A curved sensor's BMC alarm remains visible without overriding the curve.
        readings["pch_temp"] = replace(readings["pch_temp"], value=61, upper_warning=60)
        report = self.engine.update(readings, 110, 110)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["safety_state"], "recovery")
        self.assertEqual(report["bmc_alarms"], ["PCH_Temp: BMC threshold alarm"])
        self.assertEqual(report["recovery"]["healthy_samples"], 0)

    def test_startup_waits_without_a_fake_level_then_accepts_first_good_sample(self):
        for now in (0, 15, 30, 45):
            report = self.poll(now, sensor="timeout")
            self.assertIsNone(report["component_level"])
            self.assertIsNone(report["recommended_level"])
            self.assertIsNone(report["policy_target_percent"])
            self.assertEqual(report["safety_state"], "waiting-for-sample")
            self.assertFalse(report["recovery"]["holding"])
            self.assertEqual(report["recovery"]["healthy_samples"], 0)
            text = render(report)
            self.assertIn("Waiting for valid sensor data", text)
            for misleading in ("Component demand L1", "recommendation L1", "LNone", "None%"):
                self.assertNotIn(misleading, text)
        first = self.poll(60)
        self.assertEqual(first["recommended_level"], 1)
        self.assertEqual(first["safety_state"], "normal")
        self.assertEqual(first["read_failure"]["consecutive_failures"], 0)
        self.assertFalse(first["recovery"]["holding"])

    def test_startup_fallback_needs_five_completed_healthy_polls(self):
        for now in (0, 15, 30, 45, 60):
            report = self.poll(now, sensor="timeout", status="timeout")
        self.assertIsNone(report["component_level"])
        self.assertEqual(report["recommended_level"], 3)
        self.assertEqual(report["read_failure"]["consecutive_failures"], 5)
        for count, now in enumerate((75, 76, 77, 78), 1):
            report = self.poll(now)
            self.assertEqual(report["recommended_level"], 3)
            self.assertEqual(report["recovery"]["healthy_samples"], count)
            self.assertIn(f"Recovery: {count}/5", render(report))
        report = self.poll(79)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["safety_state"], "normal")
        self.assertEqual(report["recovery"], dict(healthy_samples=5, required_samples=5, holding=False))

    def test_no_history_time_limit_still_protects_without_waiting_for_five_failures(self):
        self.config["safety"]["read_failure_count"] = 100
        self.assertIsNone(self.poll(0, sensor="timeout")["recommended_level"])
        self.assertIsNone(self.poll(299, sensor="timeout")["recommended_level"])
        report = self.poll(300, sensor="timeout")
        self.assertEqual(report["recommended_level"], 3)
        self.assertEqual(report["read_failure"]["trigger_reasons"], ["no-valid-sample-timeout"])

    def test_first_sensor_data_is_usable_even_if_status_fails(self):
        report = self.poll(0, status="timeout")
        self.assertEqual(report["component_level"], 1)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["recovery"]["healthy_samples"], 0)
        report = self.poll(15, text=SAMPLE.replace("60.000", "75.000"), status="timeout")
        self.assertEqual(report["recommended_level"], 4)
        self.assertEqual(report["recovery"]["healthy_samples"], 0)

    def test_recovery_restarts_after_failures_or_gaps_and_cancel_does_not_commit(self):
        self.warm()
        self.poll(75, text=SAMPLE.replace("3335.000", "0.000"))
        for count, now in enumerate((90, 105), 1):
            self.assertEqual(self.poll(now)["recovery"]["healthy_samples"], count)
        failed = self.poll(120, status="timeout")
        self.assertEqual(failed["recommended_level"], 5)
        self.assertEqual(failed["recovery"]["healthy_samples"], 0)
        self.poll(135)
        self.poll(150)
        self.assertEqual(self.poll(181)["recovery"]["healthy_samples"], 1)
        for count, now in enumerate((196, 211, 226), 2):
            report = self.poll(now)
            self.assertEqual(report["recommended_level"], 5)
            self.assertEqual(report["recovery"]["healthy_samples"], count)
        stop = threading.Event()
        with self.assertRaises(CollectionCancelled):
            self.poll(235, status="cancel", stop=stop)
        self.assertEqual(self.engine.safety.recovery_status()["healthy_samples"], 4)
        self.assertEqual(self.engine.last_update, 226)
        report = self.poll(241)
        self.assertEqual(report["recommended_level"], 1)
        self.assertFalse(report["recovery"]["holding"])

    def test_five_healthy_samples_do_not_replace_component_cooldown_seconds(self):
        self.warm()
        hot = SAMPLE.replace("60.000", "80.000").replace(
            "27.000     | degrees C  | ok", "27.000     | degrees C  | unc")
        self.poll(75, text=hot)
        for count, now in enumerate((90, 91, 92, 93, 94), 1):
            report = self.poll(now)
            self.assertEqual(report["recovery"]["healthy_samples"], count)
            self.assertEqual(report["recommended_level"], 5)
        self.assertEqual(report["safety_state"], "normal")
        for now in (105, 120, 135, 149):
            self.assertEqual(self.poll(now)["component_level"], 5)
        self.assertEqual(self.poll(150)["recommended_level"], 4)

    def test_configured_threshold_and_level_are_used(self):
        settings = dict(self.config["safety"], read_failure_count=2, read_failure_level=4,
                        read_failure_timeout_seconds=600, recovery_success_count=1)
        safety = Safety(settings)
        self.assertEqual(safety.update(1, [], [], 0), (1, "normal"))
        self.assertEqual(safety.update(1, [], [], 15, read_failed=True, valid_sample=False)[0], 1)
        self.assertEqual(safety.update(1, [], [], 30, read_failed=True, valid_sample=False)[0], 4)
        self.assertEqual(safety.update(1, [], [], 45), (1, "normal"))


if __name__ == "__main__":
    unittest.main()
