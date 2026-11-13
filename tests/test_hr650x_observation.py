"""Regression cases from the owner's HR650X / BMC 11.99 sensor output."""
import io
import json
import unittest
from contextlib import redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from hrx_fancontrol.config import load_config
from hrx_fancontrol.hardware.ipmi import CommandResult, ReadOnlyIPMI
from hrx_fancontrol.main import main
from hrx_fancontrol.hardware.sensors import parse_sensors
from hrx_fancontrol.policy.thermal_engine import ThermalEngine


OBSERVED = (Path(__file__).resolve().parents[1] / "examples/hr650x-observed-sensors.txt").read_text()
# Read separately, before `sensor`, in the same supplied terminal transcript.
OBSERVED_STATUS = "20 0f 0f 0f 0f 0f 0f 0f 0f"


class HR650XObservationTest(unittest.TestCase):
    @patch.object(ReadOnlyIPMI, "_execute")
    def test_real_snapshot_identifies_six_fans_without_false_missing_alarms(self, execute):
        execute.side_effect = [CommandResult(("sensor",), OBSERVED, "", 0, None, 0.1),
                               CommandResult(("raw",), OBSERVED_STATUS, "", 0, None, 0.1)]
        output = io.StringIO()
        with redirect_stdout(output):
            result = main(["--once", "--json", "--no-log"])
        report = json.loads(output.getvalue())
        self.assertEqual(result, 0)
        self.assertEqual(report["faults"], [])
        self.assertEqual(report["emergencies"], [])
        expected_rpm = dict(zip((f"FAN{i}_R_Speed" for i in range(1, 7)),
                                [3680, 3680, 3680, 3680, 3450, 3335]))
        self.assertEqual({name: fan["rpm"] for name, fan in report["fans"].items()}, expected_rpm)
        self.assertAlmostEqual(sum(expected_rpm.values()) / 6, 3584.1666667)
        self.assertEqual(set(report["sensor_errors"]), {"NVME_Temp", "RAID_Temp", "PSU2_Temp"})
        self.assertEqual(report["sensors"]["PCH_Temp"]["celsius"], 63)
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 1)
        self.assertEqual(report["sensors"]["Outlet_Temp"]["requested_level"], 1)
        for name in ("CPU1_Temp", "CPU2_Temp", "NIC_Temp", "PSU1_Temp"):
            self.assertEqual(report["sensors"][name]["requested_level"], 1)
        self.assertEqual(report["sensors"]["CPU1_DTS"]["celsius"], 45)
        self.assertIsNone(report["sensors"]["CPU1_DTS"]["requested_level"])
        self.assertEqual(report["component_level"], 1)
        self.assertEqual(report["recommended_level"], 1)  # First valid snapshot is sufficient.
        self.assertEqual(report["bmc"]["raw_bytes"], ["0x20"] + ["0x0f"] * 8)
        self.assertIsNone(report["bmc"]["percent"])
        self.assertEqual(report["calibration"], {
            "status": "unverified", "ipmi_argument": None, "expected_rpm": None})
        self.assertFalse(report["applied"])
        self.assertEqual([call.args[0] for call in execute.call_args_list], [
            ("ipmitool", "-I", "open", "sensor"),
            ("ipmitool", "-I", "open", "raw", "0x2e", "0x31")])

    def test_installed_rear_fan_becoming_na_is_still_a_fault(self):
        readings, errors = parse_sensors(OBSERVED)
        self.assertEqual(errors, [])
        self.assertEqual(readings["pch_temp"].upper_warning, 83)
        self.assertEqual(readings["pch_temp"].upper_critical, 85)
        self.assertEqual(readings["pch_temp"].upper_nonrecoverable, 105)
        engine = ThermalEngine(load_config())
        for now in range(0, 61, 10):
            report = engine.update(readings, now, now)
        self.assertEqual(report["recommended_level"], 1)
        # An expected tachometer with blank units must not be ignored like optional NA rows.
        readings["fan6_r_speed"] = replace(readings["fan6_r_speed"], value=None, unit="",
                                           status="na", error="unavailable or nonnumeric value: na")
        report = engine.update(readings, 70, 70)
        self.assertEqual(report["recommended_level"], 3)
        self.assertEqual(report["safety_state"], "safe-mode")
        self.assertTrue(any("FAN6_R_Speed" in fault for fault in report["faults"]))

    def test_heat_excursion_holds_at_63_until_pch_cools_to_62(self):
        readings, _ = parse_sensors(OBSERVED)
        engine = ThermalEngine(load_config())
        for now in range(0, 61, 10):
            report = engine.update(readings, now, now)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["policy_target_percent"], 15)

        hot = dict(readings)
        hot["pch_temp"] = replace(hot["pch_temp"], value=65)
        hot["outlet_temp"] = replace(hot["outlet_temp"], value=52)
        report = engine.update(hot, 70, 70)
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 2)
        self.assertEqual(report["sensors"]["Outlet_Temp"]["requested_level"], 3)
        self.assertEqual(report["policy_target_percent"], 30)

        # Outlet can cool down at 44 C; PCH holds L2 at the observed 63 C.
        for now in range(80, 140, 10):
            report = engine.update(readings, now, now)
            self.assertEqual(report["recommended_level"], 3)
        report = engine.update(readings, 140, 140)
        self.assertEqual(report["sensors"]["PCH_Temp"]["requested_level"], 2)
        self.assertEqual(report["sensors"]["Outlet_Temp"]["requested_level"], 2)
        self.assertEqual(report["policy_target_percent"], 20)
        for now in range(150, 210, 10):
            self.assertEqual(engine.update(readings, now, now)["recommended_level"], 2)
        report = engine.update(readings, 210, 210)
        self.assertEqual(report["sensors"]["Outlet_Temp"]["requested_level"], 1)
        self.assertEqual(report["recommended_level"], 2)

        cooler = dict(readings)
        cooler["pch_temp"] = replace(cooler["pch_temp"], value=62)
        for now in range(220, 280, 10):
            self.assertEqual(engine.update(cooler, now, now)["recommended_level"], 2)
        report = engine.update(cooler, 280, 280)
        self.assertEqual(report["recommended_level"], 1)
        self.assertEqual(report["policy_target_percent"], 15)
        self.assertFalse(report["applied"])

    def test_pch_curve_and_bmc_alarm_are_independent(self):
        readings, _ = parse_sensors(OBSERVED)
        config = load_config()
        for temperature, expected_level, expected_percent in (
            (63, 1, 15), (65, 2, 20), (70, 3, 30), (75, 4, 45),
            (80, 5, 60), (82.9, 5, 60), (83, 5, 60)
        ):
            with self.subTest(temperature=temperature):
                sample = dict(readings)
                sample["pch_temp"] = replace(sample["pch_temp"], value=temperature)
                report = ThermalEngine(config).update(sample, 0, 0)
                self.assertEqual(report["recommended_level"], expected_level)
                self.assertEqual(report["policy_target_percent"], expected_percent)
                self.assertEqual(report["safety_state"], "normal")
                self.assertEqual(report["bmc_alarms"], ["PCH_Temp: BMC threshold alarm"] if temperature >= 83 else [])
                self.assertEqual(report["emergencies"], [])
                self.assertFalse(report["recovery"]["holding"])

        # A lower BMC limit is still reported; the curve alone determines the level.
        config["sensors"]["pCh_TeMp"] = config["sensors"].pop("PCH_Temp")
        readings["pch_temp"] = replace(readings["pch_temp"], value=74, upper_warning=73)
        report = ThermalEngine(config).update(readings, 0, 0)
        self.assertEqual(report["recommended_level"], 3)
        self.assertIn("PCH_Temp: BMC threshold alarm", report["bmc_alarms"])
        self.assertEqual(report["emergencies"], [])
        self.assertFalse(report["recovery"]["holding"])

    def test_pch_alarm_cools_down_without_an_extra_recovery_hold(self):
        readings, _ = parse_sensors(OBSERVED)
        engine = ThermalEngine(load_config())
        hot = {**readings, "pch_temp": replace(readings["pch_temp"], value=83)}
        report = engine.update(hot, 0, 0)
        self.assertTrue(report["bmc_alarms"])
        self.assertFalse(report["recovery"]["holding"])
        for now in (15, 45, 74):
            report = engine.update(readings, now, now)
            self.assertEqual(report["recommended_level"], 5)
            self.assertEqual(report["recovery"]["healthy_samples"], 0)
        report = engine.update(readings, 75, 75)
        self.assertEqual(report["recommended_level"], 4)
        self.assertEqual(report["safety_state"], "normal")
        self.assertFalse(report["recovery"]["holding"])

    def test_uncurved_bmc_alarm_protects_and_requires_five_healthy_samples(self):
        readings, _ = parse_sensors(OBSERVED)
        engine = ThermalEngine(load_config())
        hot = {**readings, "inlet_temp": replace(readings["inlet_temp"], value=42)}
        report = engine.update(hot, 0, 0)
        self.assertEqual(report["recommended_level"], 5)
        self.assertIn("Inlet_Temp: BMC threshold alarm", report["bmc_alarms"])
        self.assertIn("Inlet_Temp: BMC threshold alarm", report["emergencies"])
        for count, now in enumerate((15, 30, 45, 60), 1):
            report = engine.update(readings, now, now)
            self.assertEqual(report["recommended_level"], 5)
            self.assertEqual(report["recovery"]["healthy_samples"], count)
        report = engine.update(readings, 75, 75)
        self.assertEqual(report["recommended_level"], 1)
        self.assertFalse(report["recovery"]["holding"])

    def test_missing_bmc_limits_status_alarms_and_manual_override(self):
        readings, _ = parse_sensors(OBSERVED)
        for name, value, expected in (("pch_temp", 74, 3), ("inlet_temp", 42, 1)):
            for limits, status, alarm in (
                ((None, None, None), "ok", False),
                ((None, value, None), "ok", True),
                ((None, None, value), "ok", True),
                ((None, None, None), "cr", True),
            ):
                with self.subTest(name=name, limits=limits, status=status):
                    sample = dict(readings)
                    sample[name] = replace(sample[name], value=value, status=status,
                                           upper_warning=limits[0], upper_critical=limits[1],
                                           upper_nonrecoverable=limits[2])
                    report = ThermalEngine(load_config()).update(sample, 0, 0)
                    protecting = alarm and name == "inlet_temp"
                    self.assertEqual(bool(report["bmc_alarms"]), alarm)
                    self.assertEqual(bool(report["emergencies"]), protecting)
                    self.assertEqual(report["recommended_level"], 5 if protecting else expected)
                    self.assertEqual(report["recovery"]["holding"], protecting)
        config = load_config()
        self.assertTrue(all(policy["emergency_celsius"] is None for policy in config["sensors"].values()))
        config["sensors"]["PCH_Temp"]["emergency_celsius"] = 63
        report = ThermalEngine(config).update(readings, 0, 0)
        self.assertEqual(report["emergencies"], ["PCH_Temp: configured emergency temperature"])
        self.assertEqual(report["bmc_alarms"], [])
        self.assertEqual(report["recommended_level"], 5)


if __name__ == "__main__":
    unittest.main()
