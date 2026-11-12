"""Physical-control hazards tested with fake IPMI, clocks, and temporary files only."""
import copy
import io
import json
import os
import tempfile
import threading
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

from hrx_fancontrol.calibration import load_profile, profile_ranges, publish_profile, validate_profile
from hrx_fancontrol.calibration_run import CalibrationAborted, run_calibration
from hrx_fancontrol.collection import CollectionStats, collect
from hrx_fancontrol.config import load_config
from hrx_fancontrol.control_runtime import fallback, run_control
from hrx_fancontrol.executor import ExecutionFailure, Executor, ExternalOverride, OwnershipUnverified
from hrx_fancontrol.identity import machine_identity, validate_control_config
from hrx_fancontrol.ipmi import CollectionCancelled, CommandResult, ReadOnlyIPMI
from hrx_fancontrol.lifecycle import OVERRIDE_EXIT_STATUS, ControlSuspended, Ownership, WriterLock
from hrx_fancontrol.main import main, render
from hrx_fancontrol.protocol import ControlIPMI, FANS, LEVEL_BYTES, POLICY_LABELS, matches_level
from hrx_fancontrol.safety import Safety
from hrx_fancontrol.thermal_engine import ThermalEngine

IDENTITY = {"product": "ThinkSystem HR650X", "machine_uuid": "12345678-1234-1234-1234-123456789abc",
            "manufacturer_id": 8774, "product_id": 503, "firmware": "11.99"}
RPMS = {1: [3680, 3680, 3680, 3680, 3450, 3335], 2: [4485, 4485, 4485, 4485, 4350, 4140],
        3: [5900, 5900, 5900, 5900, 5700, 5600], 4: [8300, 8300, 8300, 8300, 8100, 8000],
        5: [10600, 10600, 10600, 10600, 10400, 10200]}
SAMPLE = (Path(__file__).resolve().parents[1] / "examples/hr650x-sensors.txt").read_text()


def raw(level, header="0x20"):
    return [header] + [f"0x{LEVEL_BYTES[level]:02x}"] * 6 + ["0xff", "0x00"]


def stamp(seconds):
    return (datetime(2026, 11, 11, tzinfo=timezone.utc) + timedelta(seconds=seconds)).isoformat()


def profile():
    data = {"schema_version": 1, "created_at": stamp(0), "identity": dict(IDENTITY),
            "fan_names": FANS[:], "policy_labels": POLICY_LABELS[:], "levels": {}}
    for index, level in enumerate((5, 4, 3, 2, 1)):
        start = 1 + index * 100
        samples = [{"sampled_elapsed": start + 15 + sample * 15, "timestamp": stamp(start + 15 + sample * 15),
                    "rpm": dict(zip(FANS, RPMS[level])), "raw_bytes": raw(level)} for sample in range(3)]
        data["levels"][str(level)] = {"byte": LEVEL_BYTES[level], "write_completed_elapsed": start,
                                     "samples": samples}
    ranges = profile_ranges(data["levels"])
    for level, item in data["levels"].items():
        item["rpm_ranges"] = ranges[level]
    return data


def report(level=1, at=100, recommended=1, status="ok"):
    return {"timestamp": stamp(at), "recommended_level": recommended, "component_level": recommended,
            "collection": {"status": status, "sensor_data_valid": status == "ok"},
            "bmc": {"raw_bytes": raw(level) if status == "ok" else None},
            "fans": {name: {"rpm": rpm, "error": None} for name, rpm in zip(FANS, RPMS[level])},
            "sampled_at_monotonic": at, "faults": [], "emergencies": [], "bmc_alarms": [], "recovery": {"holding": False}}


def status_result(level):
    return CommandResult(("status",), " ".join(byte[2:] for byte in raw(level)), "", 0, None, .1)


def success():
    return CommandResult(("fake",), "", "", 0, None, .1)


class EvidenceTests(unittest.TestCase):
    def test_realistic_adjacent_rpm_bands_and_reserved_header_bits(self):
        data = validate_profile(profile(), IDENTITY, load_config())
        self.assertTrue(matches_level(raw(1, "0x0c"), 1))
        self.assertFalse(matches_level(raw(1, "0x21"), 1))
        for name in FANS:
            self.assertLess(data["levels"]["1"]["rpm_ranges"][name][1], data["levels"]["2"]["rpm_ranges"][name][0])

    def test_strict_evidence_rejects_invalid_samples_bindings_and_forged_ranges(self):
        modifications = [lambda p: p["identity"].update(firmware="12.00"),
                         lambda p: p["levels"]["1"].update(byte=21),
                         lambda p: p["levels"]["1"]["samples"].pop(),
                         lambda p: p["levels"]["1"]["samples"][0].update(sampled_elapsed=401),
                         lambda p: p["levels"]["1"]["samples"][1].update(sampled_elapsed=416),
                         lambda p: p["levels"]["1"]["samples"][0]["rpm"].update(FAN1_R_Speed=float("nan")),
                         lambda p: p["levels"]["1"]["samples"][0].update(raw_bytes=raw(2)),
                         lambda p: p["levels"]["1"]["rpm_ranges"].update(FAN1_R_Speed=[1, 99999])]
        for change in modifications:
            with self.subTest(change=change):
                data = profile()
                change(data)
                with self.assertRaises(ValueError):
                    validate_profile(data, IDENTITY, load_config())

    def test_publish_is_atomic_and_never_overwrites(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            publish_profile(path, profile())
            original = path.read_bytes()
            with self.assertRaises(FileExistsError):
                publish_profile(path, profile())
            self.assertEqual(path.read_bytes(), original)
            self.assertEqual(load_profile(path, IDENTITY, load_config()), profile())
            self.assertEqual(list(Path(tmp).iterdir()), [path])

    def test_protocol_does_not_convert_policy_percent_to_hex_digits(self):
        ipmi = ControlIPMI()
        with patch.object(ipmi, "_execute", return_value=success()) as execute:
            for level in range(1, 6):
                ipmi.set_level(level)
                self.assertEqual(execute.call_args[0][0][-1], f"0x{LEVEL_BYTES[level]:02x}")
            with self.assertRaises(ValueError):
                ipmi.set_level(True)
        with self.assertRaises(ValueError):
            ReadOnlyIPMI()._read(("raw", "0x2e", "0x30"))

    def test_identity_checks_local_linux_product_uuid_and_firmware(self):
        def bmc_info(product_id=503, manufacturer_id=8774, firmware="11.99"):
            fields = {"Manufacturer ID": manufacturer_id,
                      "Product ID": f"{product_id} (0x{product_id:04x})" if type(product_id) is int else product_id,
                      "Firmware Revision": firmware}
            return CommandResult(("fake",),
                                 "".join(f"{key} : {value}\n" for key, value in fields.items() if value is not None),
                                 "", 0, None, .1)

        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "product_name").write_text(IDENTITY["product"])
            (root / "product_uuid").write_text(IDENTITY["machine_uuid"])
            ipmi = Mock()
            with patch("hrx_fancontrol.identity.platform.system", return_value="Linux"):
                for manufacturer_id, product_id in ((8774, 502), (8774, 503), (8774, 501), (8774, 504), (1, 503)):
                    with self.subTest(manufacturer_id=manufacturer_id, product_id=product_id):
                        ipmi.identity_info.return_value = bmc_info(product_id, manufacturer_id)
                        self.assertEqual(machine_identity(ipmi, root),
                                         {**IDENTITY, "manufacturer_id": manufacturer_id, "product_id": product_id})
                for invalid in ({"manufacturer_id": None}, {"manufacturer_id": ""}, {"manufacturer_id": "unknown"},
                                {"product_id": None}, {"product_id": ""}, {"product_id": "unknown"},
                                {"firmware": None}, {"firmware": ""}):
                    with self.subTest(invalid=invalid):
                        ipmi.identity_info.return_value = bmc_info(**invalid)
                        with self.assertRaises(ValueError):
                            machine_identity(ipmi, root)
                ipmi.identity_info.return_value = bmc_info()
                (root / "product_name").write_text("HR630X")
                with self.assertRaises(ValueError):
                    machine_identity(ipmi, root)
                (root / "product_name").write_text(IDENTITY["product"])
                for invalid_uuid in ("invalid", "00000000-0000-0000-0000-000000000000",
                                     "ffffffff-ffff-ffff-ffff-ffffffffffff"):
                    with self.subTest(uuid=invalid_uuid):
                        (root / "product_uuid").write_text(invalid_uuid)
                        with self.assertRaises(ValueError):
                            machine_identity(ipmi, root)
                (root / "product_uuid").write_text(IDENTITY["machine_uuid"])
            with patch("hrx_fancontrol.identity.platform.system", return_value="Darwin"):
                with self.assertRaises(ValueError):
                    machine_identity(ipmi, root)


class ActuatorTests(unittest.TestCase):
    def setUp(self):
        self.now = 100
        self.stop = threading.Event()
        self.ipmi = Mock()
        self.ipmi.set_level.return_value = success()
        self.owner = Mock()
        self.events = []
        self.executor = Executor(self.ipmi, profile(), self.owner,
                                 lambda event, data: self.events.append((event, data)), self.stop,
                                 clock=lambda: self.now)
        self.ipmi.control_status.side_effect = lambda: status_result(self.executor.target or 1)

    def test_no_recommendation_does_not_assume_l1(self):
        self.executor.update(report(recommended=None))
        self.ipmi.set_level.assert_not_called()

    def test_write_immediately_reads_bmc_once_without_profile_or_rpm(self):
        self.executor.profile = None
        self.executor.request(2)
        self.ipmi.control_status.assert_called_once_with()
        self.assertTrue(self.executor.snapshot()["verified"])
        self.assertFalse(self.executor.snapshot()["rpm_calibrated"])
        self.assertEqual(self.executor.confirmations, 1)
        self.executor.update(report(2, 115, recommended=2))
        self.ipmi.set_level.assert_called_once_with(2)

    def test_old_or_expired_report_cannot_confirm_write_or_deduplicate(self):
        self.ipmi.control_status.side_effect = None
        self.ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
        self.executor.request(2)
        for at in (50, 99, 100):
            self.executor.observe(report(2, at))
            self.assertFalse(self.executor.snapshot()["verified"])
        self.now = 140
        self.executor.observe(report(2, 101))
        self.assertFalse(self.executor.snapshot()["verified"])
        self.executor.observe(report(2, 140))
        self.assertTrue(self.executor.snapshot()["verified"])
        self.now = 200
        self.executor.update(report(2, 140, recommended=3))
        self.assertEqual(self.ipmi.set_level.call_count, 2)

    def test_late_completed_poll_cannot_bypass_confirmation_deadline(self):
        self.ipmi.control_status.side_effect = None
        self.ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
        self.executor.request(2)
        self.now = 209
        with self.assertRaises(ExecutionFailure):
            self.executor.check_readback(report(2, 209, recommended=2))
        self.assertFalse(self.executor.snapshot()["verified"])
        self.owner.confirm.assert_not_called()

    def test_early_l5_uses_no_same_poll_old_rpm_and_interrupts_low_verification(self):
        self.executor.request(1)
        self.now = 115
        self.executor.early_l5(report(1, 110, recommended=5))
        self.executor.observe(report(5, 110, recommended=5))
        self.assertEqual(self.executor.confirmations, 1)
        self.assertEqual([call.args[0] for call in self.ipmi.set_level.call_args_list], [1, 5])

    def test_collected_fan_fault_executes_l3(self):
        config = load_config()
        self.ipmi.sensors.return_value = CommandResult(
            ("sensor",), SAMPLE.replace("3335.000", "0.000"), "", 0, None, .1)
        alerts = []
        with patch("hrx_fancontrol.collection.time.monotonic", return_value=self.now):
            data = collect(self.ipmi, ThermalEngine(config), config, on_alert=alerts.append)
        self.assertEqual(data["recommended_level"], 3)
        self.assertEqual(alerts, [])
        self.executor.update(data)
        self.ipmi.set_level.assert_called_once_with(3)

    def test_already_matching_physical_setting_does_not_rewrite(self):
        for at in (100, 115, 130):
            self.executor.update(report(1, at))
        self.ipmi.set_level.assert_not_called()
        self.assertTrue(self.executor.snapshot()["verified"])

    def test_transient_collection_failures_do_not_become_execution_failures(self):
        self.executor.request(1)
        for index in range(4):
            self.executor.update(report(1, 115 + index * 15, status="failed"))
        self.ipmi.set_level.assert_called_once_with(1)
        self.assertTrue(self.executor.snapshot()["verified"])
        self.executor.update(report(1, 200, recommended=3, status="failed"))
        self.assertEqual(self.ipmi.set_level.call_args.args, (3,))

    def test_successful_but_stale_collections_use_read_failure_thresholds_not_rpm_mismatches(self):
        # These are the actual accepted control defaults. A process/scheduler stall
        # can still deliver a successful command after the freshness deadline.
        config = load_config()
        validate_control_config(config)
        for poll_count, initial_gap, trigger in ((5, 15, "failure-count"),
                                                  (1, 300, "no-valid-sample-timeout")):
            with self.subTest(trigger=trigger):
                self.now = 100
                engine = ThermalEngine(config)
                ipmi = Mock()
                ipmi.set_level.return_value = success()
                executor = Executor(ipmi, profile(), Mock(), lambda *_: None, self.stop, lambda: self.now)
                ipmi.sensors.return_value = CommandResult(("sensor",), SAMPLE, "", 0, None, .1)
                ipmi.control_status.side_effect = lambda: CommandResult(
                    ("status",), " ".join(byte[2:] for byte in raw(executor.target or 1)), "", 0, None, .1)
                with patch("hrx_fancontrol.collection.time.monotonic", side_effect=lambda: self.now):
                    initial = collect(ipmi, engine, config)
                    self.assertTrue(initial["collection"]["sensor_data_valid"])
                    executor.request(1)

                    def late_sensor_result():
                        self.now += config["safety"]["sensor_timeout_seconds"] + 1
                        return CommandResult(("sensor",), SAMPLE, "", 0, None, .1)
                    ipmi.sensors.side_effect = late_sensor_result
                    for index in range(poll_count):
                        self.now += initial_gap if index == 0 else 15
                        stale = collect(ipmi, engine, config)
                        self.assertEqual(stale["collection"]["status"], "failed")
                        self.assertEqual(stale["collection"]["failure_kinds"], ["sensor:stale"])
                        self.assertFalse(stale["collection"]["sensor_data_valid"])
                        executor.update(stale)
                        self.assertEqual(executor.mismatches, 0)
                        self.assertTrue(executor.snapshot()["verified"])
                        if index < poll_count - 1:
                            self.assertEqual(executor.target, 1)
                    self.assertEqual(stale["read_failure"]["consecutive_failures"], poll_count)
                    self.assertIn(trigger, stale["read_failure"]["trigger_reasons"])
                    self.assertEqual([call.args[0] for call in ipmi.set_level.call_args_list], [1, 3])

    def test_valid_bmc_confirms_setting_independently_of_sensor_validity(self):
        stale = report(2, recommended=3)
        stale["collection"]["sensor_data_valid"] = False
        self.executor.update(stale)
        self.ipmi.set_level.assert_called_once_with(3)
        stale["bmc"]["raw_bytes"] = raw(3)
        for at in (115, 130, 145):
            stale["sampled_at_monotonic"] = at
            self.executor.observe(stale)
        self.assertTrue(self.executor.snapshot()["verified"])
        self.assertEqual(self.executor.confirmations, 1)

    def test_startup_failures_preserve_higher_or_unknown_physical_settings_until_recovery(self):
        config = load_config()
        validate_control_config(config)
        for physical in (4, 5, None, "unknown"):
            with self.subTest(physical=physical):
                self.now = 100
                engine = ThermalEngine(config)
                ipmi = Mock()
                ipmi.set_level.return_value = success()
                ipmi.sensors.return_value = CommandResult(("sensor",), "", "", None, "IPMI timeout", 20)
                if physical is None:
                    ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
                else:
                    status = raw(physical) if isinstance(physical, int) else ["0x20"] + ["0x10"] * 8
                    ipmi.control_status.return_value = CommandResult(
                        ("status",), " ".join(byte[2:] for byte in status), "", 0, None, .1)
                executor = Executor(ipmi, profile(), Mock(), lambda *_: None, self.stop, lambda: self.now)
                with patch("hrx_fancontrol.collection.time.monotonic", side_effect=lambda: self.now):
                    for count in range(1, 6):
                        data = collect(ipmi, engine, config)
                        executor.update(data)
                        self.now += 15
                    self.assertEqual(data["read_failure"]["consecutive_failures"], 5)
                    self.assertEqual(data["recommended_level"], 3)
                    ipmi.set_level.assert_not_called()
                    self.assertFalse(executor.snapshot()["verified"])
                    if not isinstance(physical, int):
                        self.assertIsNone(executor.target)
                        # An actual L5 demand may still act without a known prior setting.
                        executor.update(report(1, self.now, recommended=5, status="failed"))
                        ipmi.set_level.assert_called_once_with(5)
                        continue
                    self.assertEqual(executor.target, physical)
                    rows = []
                    for line in SAMPLE.splitlines():
                        fields = [part.strip() for part in line.split("|")]
                        if fields[0] in FANS:
                            fields[1] = str(RPMS[physical][FANS.index(fields[0])])
                        rows.append(" | ".join(fields))
                    ipmi.sensors.return_value = CommandResult(("sensor",), "\n".join(rows), "", 0, None, .1)
                    for count in range(1, 6):
                        data = collect(ipmi, engine, config)
                        executor.update(data)
                        self.now += 15
                        if count < 5:
                            self.assertTrue(data["recovery"]["holding"])
                            self.assertEqual(executor.target, physical)
                            ipmi.set_level.assert_not_called()
                    self.assertFalse(data["recovery"]["holding"])
                    ipmi.set_level.assert_called_once_with(1)

    def test_recovery_discovers_prior_l5_before_allowing_any_downshift(self):
        config = load_config()
        validate_control_config(config)
        engine = ThermalEngine(config)
        ipmi = Mock()
        ipmi.set_level.return_value = success()
        ipmi.sensors.return_value = CommandResult(("sensor",), "", "", None, "IPMI timeout", 20)
        ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
        executor = Executor(ipmi, profile(), Mock(), lambda *_: None, self.stop, lambda: self.now)
        with patch("hrx_fancontrol.collection.time.monotonic", side_effect=lambda: self.now):
            for count in range(5):
                executor.update(collect(ipmi, engine, config))
                self.now += 15
            self.assertIsNone(executor.target)
            ipmi.set_level.assert_not_called()
            rows = []
            for line in SAMPLE.splitlines():
                fields = [part.strip() for part in line.split("|")]
                if fields[0] in FANS:
                    fields[1] = str(RPMS[5][FANS.index(fields[0])])
                rows.append(" | ".join(fields))
            ipmi.sensors.return_value = CommandResult(("sensor",), "\n".join(rows), "", 0, None, .1)
            ipmi.control_status.return_value = CommandResult(
                ("status",), " ".join(byte[2:] for byte in raw(5)), "", 0, None, .1)
            for count in range(1, 6):
                data = collect(ipmi, engine, config)
                executor.update(data)
                self.now += 15
                self.assertEqual(data["recovery"]["healthy_samples"], count)
                if count < 5:
                    self.assertTrue(data["recovery"]["holding"])
                    self.assertEqual(executor.target, 5)
                    ipmi.set_level.assert_not_called()
            self.assertFalse(data["recovery"]["holding"])
            ipmi.set_level.assert_called_once_with(1)

    def test_first_healthy_sample_without_recovery_can_follow_the_curve(self):
        self.executor.update(report(5, recommended=1))
        self.ipmi.set_level.assert_called_once_with(1)

    def test_deadline_allows_inflight_read_but_starts_no_new_read_after_90_seconds(self):
        for started in (189, 190):
            with self.subTest(started=started):
                self.now = 100
                ipmi = Mock()
                ipmi.set_level.return_value = success()
                ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
                executor = Executor(ipmi, None, Mock(), Mock(), self.stop, lambda: self.now)
                executor.request(2)
                ipmi.control_status.reset_mock()
                def delayed_read():
                    self.now += 3
                    return status_result(2)
                ipmi.control_status.side_effect = delayed_read
                self.now = started
                if started == 189:
                    self.assertTrue(executor.readback())
                    self.assertEqual(self.now, 192)
                    ipmi.control_status.assert_called_once()
                else:
                    with self.assertRaises(ExecutionFailure):
                        executor.readback()
                    ipmi.control_status.assert_not_called()

    def test_expired_identical_raw_cannot_skip_a_new_write(self):
        self.ipmi.control_status.side_effect = None
        self.ipmi.control_status.return_value = status_result(2)
        self.executor.readback()  # A cached read, without an adopted target.
        self.now = 140
        self.executor.update(report(2, 100, recommended=2))
        self.ipmi.set_level.assert_called_once_with(2)
        self.assertTrue(self.executor.snapshot()["verified"])  # New post-write read only.

    def test_pending_mismatch_waits_for_matching_bmc_and_ignores_rpm(self):
        self.ipmi.control_status.side_effect = None
        self.ipmi.control_status.return_value = status_result(1)
        self.executor.request(2)
        self.now = 110
        self.executor.observe(report(1, self.now))
        self.assertFalse(self.executor.snapshot()["verified"])
        data = report(2, 115)
        data["fans"] = report(1)["fans"]
        self.now = 115
        self.executor.observe(data)
        self.assertTrue(self.executor.snapshot()["verified"])
        with self.assertRaises(ExternalOverride):
            self.executor.observe(report(1, 120))

    def test_cancelled_write_stays_unknown_without_another_command(self):
        self.ipmi.set_level.side_effect = CollectionCancelled()
        with self.assertRaises(CollectionCancelled):
            self.executor.request(2)
        self.assertEqual(self.executor.status, "write-in-flight-unknown")
        self.ipmi.set_level.assert_called_once_with(2)

    def test_fresh_readback_mismatch_is_not_hidden_by_failed_temperature_read(self):
        self.executor.request(2)
        partial = report(1, 120, status="failed")
        partial["bmc"]["raw_bytes"] = raw(1)
        with self.assertRaises(ExternalOverride):
            self.executor.observe(partial)

    def test_fallback_is_bounded_when_no_reads_succeed(self):
        self.ipmi.control_status.side_effect = None
        self.ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
        def wait(seconds):
            self.now += seconds
        result = fallback(self.executor, lambda: report(1, self.now, status="failed"), wait,
                          lambda *_: None, clock=lambda: self.now)
        self.assertFalse(result)
        self.ipmi.set_level.assert_called_once_with(3)
        self.owner.before_write.assert_called_once_with(3, fallback=True)


class FinalSampleFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        validate_control_config(self.config)
        self.now = 100
        self.engine = ThermalEngine(self.config)
        self.stats = CollectionStats()
        self.ipmi = Mock()
        self.ipmi.set_level.return_value = success()
        self.executor = Executor(self.ipmi, profile(), Mock(), lambda *_: None,
                                 threading.Event(), lambda: self.now)
        self.clock = patch("hrx_fancontrol.collection.time.monotonic", side_effect=lambda: self.now)
        self.clock.start()
        self.addCleanup(self.clock.stop)

    def poll(self, at, *, temperature=60, rpm_level=1, delay=0, raw_level=None):
        self.now = at
        lines = []
        for line in SAMPLE.replace("60.000", f"{temperature}.000").splitlines():
            parts = line.split("|")
            if parts[0].strip() in FANS:
                parts[1] = f" {RPMS[rpm_level][FANS.index(parts[0].strip())]}.000 "
            lines.append("|".join(parts))
        self.ipmi.sensors.return_value = CommandResult(("sensor",), "\n".join(lines), "", 0, None, .1)

        def status():
            # Model scheduling latency after an immediate, successful sensor read.
            self.now += delay
            level = raw_level or self.executor.target or 1
            return CommandResult(("status",), " ".join(byte[2:] for byte in raw(level)), "", 0, None, .1)

        self.ipmi.control_status.side_effect = status
        return collect(self.ipmi, self.engine, self.config, on_alert=self.executor.early_l5, stats=self.stats)

    def test_late_status_expires_once_keeps_raw_and_obeys_five_or_300_without_lowering(self):
        for initial_level, temperature in ((1, 60), (4, 75), (5, 80)):
            for polls, first_gap, reason in ((5, 15, "failure-count"),
                                              (1, 269, "no-valid-sample-timeout")):
                with self.subTest(initial_level=initial_level, reason=reason):
                    self.engine = ThermalEngine(self.config)
                    self.stats = CollectionStats()
                    self.ipmi.set_level.reset_mock()
                    self.executor = Executor(self.ipmi, profile(), Mock(), lambda *_: None,
                                             threading.Event(), lambda: self.now)
                    self.poll(100, temperature=temperature, rpm_level=initial_level)
                    if self.executor.target is None:
                        self.executor.request(initial_level)
                    for index in range(polls):
                        data = self.poll(self.now + (first_gap if index == 0 else 15), delay=31)
                        self.assertFalse(data["collection"]["sensor_data_valid"])
                        self.assertTrue(data["collection"]["sensor_data_available"])
                        self.assertEqual(data["collection"]["status"], "failed")
                        self.assertEqual(data["collection"]["failure_kinds"], ["sensor:stale"])
                        self.assertEqual(data["collection"]["counts"], dict(
                            started=index + 2, completed=index + 2, successful=1,
                            failed=index + 1, timed_out=0, cancelled=0))
                        self.assertEqual([command["returncode"] for command in data["commands"]], [0, 0])
                        self.assertEqual(data["bmc"]["raw_bytes"], raw(initial_level))
                        self.assertEqual(data["sensors"]["PCH_Temp"]["observed_value"], 60)
                        self.assertIsNone(data["sensors"]["PCH_Temp"]["celsius"])
                        self.assertTrue(all(fan["rpm"] is None for fan in data["fans"].values()))
                        self.assertFalse(any("missing installed fan" in fault for fault in data["faults"]))
                        self.assertEqual(data["read_failure"]["consecutive_failures"], index + 1)
                        self.assertEqual(self.engine.safety.last_valid_sample_at, 100)
                        self.executor.update(data)
                        self.assertEqual(self.executor.mismatches, 0)
                        self.assertTrue(self.executor.snapshot()["verified"])
                        if index < polls - 1:
                            self.assertEqual(self.executor.target, initial_level)
                    self.assertIn(reason, data["read_failure"]["trigger_reasons"])
                    self.assertEqual(self.executor.target, max(initial_level, 3))
                    self.assertEqual([call.args[0] for call in self.ipmi.set_level.call_args_list],
                                     [1, 3] if initial_level == 1 else [initial_level])

    def test_late_status_cannot_commit_downshift_or_advance_recovery(self):
        self.poll(100, temperature=65, rpm_level=2)
        self.executor.request(2)
        for at in (115, 130, 145, 160):
            self.executor.update(self.poll(at, rpm_level=2))
        # The provisional low sample at 175 would complete the 60-second timer.
        data = self.poll(175, rpm_level=2, delay=31)
        self.executor.update(data)
        self.assertEqual(data["recommended_level"], 2)
        self.assertEqual(data["recovery"]["healthy_samples"], 0)
        self.assertEqual(self.engine.states["PCH_Temp"].level, 2)
        self.assertIsNone(self.engine.states["PCH_Temp"].below_since)
        self.assertEqual(self.engine.previous, {})
        self.assertEqual(self.engine.safety.last_valid_sample_at, 160)
        self.assertEqual(self.executor.confirmations, 1)
        for count, at in enumerate((221, 236, 251, 266, 281), 1):
            data = self.poll(at, rpm_level=2)
            self.executor.update(data)
            self.assertEqual(data["recovery"]["healthy_samples"], count)
            self.assertEqual(data["recommended_level"], 2 if count < 5 else 1)
        self.assertFalse(data["recovery"]["holding"])
        self.assertEqual([call.args[0] for call in self.ipmi.set_level.call_args_list], [2, 1])

    def test_early_fresh_l5_survives_expiration_and_is_not_rewritten(self):
        self.poll(100)
        self.executor.request(1)
        for index in range(2):
            data = self.poll(self.now + 15, temperature=80, delay=31)
            self.executor.update(data)
            self.assertEqual(data["recommended_level"], 5)
            self.assertEqual(data["read_failure"]["consecutive_failures"], index + 1)
            self.assertEqual(data["recovery"]["healthy_samples"], 0)
            self.assertFalse(data["collection"]["sensor_data_valid"])
            self.assertEqual(self.engine.safety.last_valid_sample_at, 100)
            self.assertEqual(self.executor.confirmations, 1)
        self.assertEqual([call.args[0] for call in self.ipmi.set_level.call_args_list], [1, 5])

    def test_expired_temperature_does_not_hide_independent_positive_readback_mismatch(self):
        self.poll(100)
        self.executor.request(2)
        data = self.poll(115, delay=31, raw_level=1)
        self.assertFalse(data["collection"]["sensor_data_valid"])
        self.assertEqual(data["bmc"]["raw_bytes"], raw(1))
        with self.assertRaises(ExternalOverride):
            self.executor.update(data)
        self.assertEqual(self.executor.mismatches, 0)


class LifecycleTests(unittest.TestCase):
    def test_control_without_a_target_renders_waiting(self):
        config = load_config()
        ipmi = Mock()
        ipmi.sensors.return_value = CommandResult(("sensor",), "", "", None, "IPMI timeout", 20)
        ipmi.control_status.return_value = CommandResult(("status",), " ".join(byte[2:] for byte in raw(1)), "", 0, None, .1)
        data = collect(ipmi, ThermalEngine(config), config)
        executor = Executor(ipmi, profile(), Mock(), lambda *_: None, threading.Event())
        data.update(mode="automatic", control=executor.snapshot())
        text = render(data)
        self.assertIn("Control target waiting for valid data", text)
        self.assertNotIn("LNone", text)
        executor.raw = raw(2)
        data["control"] = executor.snapshot()
        self.assertIn("BMC raw bytes: " + " ".join(raw(2)), render(data))
        self.assertEqual(data["bmc"]["raw_bytes"], raw(1))  # Original sampling evidence stays intact.

    def test_calibration_events_have_chinese_messages(self):
        from argparse import Namespace
        for aborted in (False, True):
            with self.subTest(aborted=aborted), tempfile.TemporaryDirectory() as tmp:
                args = Namespace(calibrate=Path(tmp) / "p.json", auto=False,
                                 service_recover=False, profile=None, json=True)
                config = load_config()
                config["logging"]["enabled"] = False
                def calibration(executor, identity, config, path, collect_next, wait, emit):
                    emit("calibration_level", {"target_level": 5, "status": "measured"})
                    if aborted:
                        raise CalibrationAborted("Higher thermal demand")
                    emit("calibration_complete", {"status": "published", "path": str(path)})
                with patch("hrx_fancontrol.control_runtime.machine_identity", return_value=IDENTITY), \
                     patch("hrx_fancontrol.control_runtime.WriterLock", side_effect=lambda: WriterLock(Path(tmp) / "run")), \
                     patch("hrx_fancontrol.control_runtime.control_state_directory", side_effect=lambda: (Path(tmp) / "run")), \
                     patch("hrx_fancontrol.control_runtime.run_calibration", side_effect=calibration), \
                     patch("sys.stdout", new_callable=io.StringIO) as output:
                    self.assertEqual(run_control(args, config), int(aborted))
                messages = {item["event"]: item["message"] for item in map(json.loads, output.getvalue().splitlines())}
                self.assertEqual(messages["calibration_level"], "本档校准采样完成。")
                if aborted:
                    self.assertEqual(messages["calibration_aborted"], "校准已中止，保留最后设置，未生成完整资料。")
                else:
                    self.assertEqual(messages["calibration_complete"], "五档校准完成，资料已保存。")

    def test_lock_marker_ownership_normal_stop_and_one_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"INVOCATION_ID": "one", "SERVICE_RESULT": "signal"}):
            with WriterLock(tmp):
                with self.assertRaises(ValueError):
                    with WriterLock(tmp):
                        pass
                owner = Ownership(tmp, IDENTITY, profile(), "run")
                self.assertIsNone(Ownership.recover(tmp, IDENTITY, profile()))
                owner.before_write(2)
                with patch.dict(os.environ, {"INVOCATION_ID": "other"}):
                    self.assertIsNone(Ownership.recover(tmp, IDENTITY, profile()))
                with patch.dict(os.environ, {"SERVICE_RESULT": "success"}):
                    self.assertIsNone(Ownership.recover(tmp, IDENTITY, profile()))
                self.assertIsNotNone(Ownership.recover(tmp, IDENTITY, profile()))
                owner.before_write(3, fallback=True)
                self.assertIsNone(Ownership.recover(tmp, IDENTITY, profile()))
                with self.assertRaises(ValueError):
                    owner.before_write(3, fallback=True)
                owner.finish()
                self.assertIsNone(Ownership.recover(tmp, IDENTITY, profile()))
            with WriterLock(tmp):
                pass

    def test_cli_rejects_offline_or_once_control_and_allows_no_profile(self):
        invalid = [["--resume-control"], ["--resume-control", "--service-recover", "--profile", "p.json"], ["--auto", "--profile", "p.json", "--once"],
                   ["--calibrate", "p.json", "--once", "--input", "sample"],
                   ["--manual-level", "2", "--calibrate", "p.json"], ["--profile", "p.json"]]
        with patch("hrx_fancontrol.protocol.ControlIPMI.set_level") as write, patch("sys.stderr", new_callable=io.StringIO):
            for argv in invalid:
                with self.assertRaises(SystemExit):
                    main(argv)
            write.assert_not_called()

    def test_cli_service_and_recovery_accept_no_profile(self):
        with patch("hrx_fancontrol.control_runtime.run_control", return_value=0) as run:
            for argv in (["--serve"], ["--auto"], ["--service-recover"],
                         ["--serve", "--profile", "/missing/old.json"]):
                self.assertEqual(main(argv), 0)
            self.assertEqual(run.call_count, 4)

    def test_normal_stop_during_write_keeps_last_setting_and_disarms_service_marker(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            publish_profile(path, profile())
            args = Namespace(calibrate=None, auto=True,  service_recover=False,
                             profile=Path(tmp) / "missing-profile.json", json=True)
            config = load_config()
            config["logging"]["enabled"] = False
            ipmi = Mock()
            ipmi.set_level.side_effect = CollectionCancelled()
            with patch("hrx_fancontrol.control_runtime.machine_identity", return_value=IDENTITY), \
                 patch("hrx_fancontrol.control_runtime.ControlIPMI", return_value=ipmi), \
                 patch("hrx_fancontrol.control_runtime.WriterLock", side_effect=lambda: WriterLock(Path(tmp) / "run")), \
                 patch("hrx_fancontrol.control_runtime.control_state_directory", side_effect=lambda: (Path(tmp) / "run")), \
                 patch("hrx_fancontrol.control_runtime.collect", return_value=report(1, recommended=2)), \
                 patch("sys.stdout", new_callable=io.StringIO):
                self.assertEqual(run_control(args, config), 0)
            ipmi.set_level.assert_called_once_with(2)
            self.assertFalse(json.loads((Path(tmp) / "run" / "owner.json").read_text())["active"])

    def test_normal_stop_cleanup_failure_returns_clean_stop_code_and_prevents_recovery(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            publish_profile(path, profile())
            args = Namespace(calibrate=None, auto=True,  service_recover=False,
                             profile=path, json=True)
            config = load_config()
            config["logging"]["enabled"] = False
            ipmi = Mock()
            ipmi.set_level.side_effect = CollectionCancelled()
            with patch("hrx_fancontrol.control_runtime.machine_identity", return_value=IDENTITY), \
                 patch("hrx_fancontrol.control_runtime.ControlIPMI", return_value=ipmi), \
                 patch("hrx_fancontrol.control_runtime.WriterLock", side_effect=lambda: WriterLock(Path(tmp) / "run")), \
                 patch("hrx_fancontrol.control_runtime.control_state_directory", side_effect=lambda: (Path(tmp) / "run")), \
                 patch("hrx_fancontrol.control_runtime.collect", return_value=report(1, recommended=2)), \
                 patch.object(Ownership, "finish", side_effect=OSError("disk unavailable")), \
                 patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(run_control(args, config), 78)
            ipmi.set_level.assert_called_once_with(2)
            with patch.dict(os.environ, {"INVOCATION_ID": "one", "SERVICE_RESULT": "exit-code",
                                         "EXIT_CODE": "exited", "EXIT_STATUS": "78"}):
                self.assertIsNone(Ownership.recover(Path(tmp) / "run", IDENTITY, profile()))

    def test_unexpected_control_exception_attempts_only_one_l3(self):
        from argparse import Namespace
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "p.json"
            publish_profile(path, profile())
            args = Namespace(calibrate=None, auto=True,  service_recover=False,
                             profile=path, json=True)
            config = load_config()
            config["logging"]["enabled"] = False
            ipmi = Mock()
            ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
            ipmi.set_level.side_effect = [success(), CommandResult(("write",), "", "", 1, "write failed", .1)]
            def crash(executor, *_):
                executor.request(5)
                raise RuntimeError("unexpected runtime failure")
            with patch("hrx_fancontrol.control_runtime.machine_identity", return_value=IDENTITY), \
                 patch("hrx_fancontrol.control_runtime.ControlIPMI", return_value=ipmi), \
                 patch("hrx_fancontrol.control_runtime.WriterLock", side_effect=lambda: WriterLock(Path(tmp) / "run")), \
                 patch("hrx_fancontrol.control_runtime.control_state_directory", side_effect=lambda: (Path(tmp) / "run")), \
                 patch("hrx_fancontrol.control_runtime.collect", return_value=report(1, recommended=2)), \
                 patch.object(Executor, "update", crash), \
                 patch("sys.stdout", new_callable=io.StringIO) as output, patch("sys.stderr", new_callable=io.StringIO):
                self.assertEqual(run_control(args, config), 1)
            self.assertEqual([call.args[0] for call in ipmi.set_level.call_args_list], [5, 3])
            messages = {item["event"]: item["message"] for item in map(json.loads, output.getvalue().splitlines())}
            self.assertEqual(messages["control_failed"], "控制发生异常，停止常规调速。")
            self.assertEqual(messages["fallback_failed"], "异常兜底 L3 失败，当前物理状态未确认。")
            marker = json.loads((Path(tmp) / "run" / "owner.json").read_text())
            self.assertTrue(marker["fallback_attempted"])
            self.assertFalse(marker["active"])


class WizardTests(unittest.TestCase):
    def interrupted_calibration(self, after_level, interrupt, error=CalibrationAborted):
        now = [1.0]
        measured = []
        interrupted_reads = []
        ipmi = Mock()
        ipmi.set_level.return_value = success()
        executor = Executor(ipmi, None, Mock(), lambda *_: None, threading.Event(), lambda: now[0])
        ipmi.control_status.side_effect = lambda: status_result(executor.target or 1)
        def collect_next():
            now[0] += .1
            if after_level in measured:
                interrupted_reads.append(now[0])
                return interrupt(executor, now[0], len(interrupted_reads))
            return report(executor.target or 1, now[0])
        def wait(seconds):
            now[0] += seconds
        def emit(event, data):
            if event == "calibration_level":
                measured.append(data["target_level"])
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            with self.assertRaises(error):
                run_calibration(executor, IDENTITY, load_config(), path, collect_next, wait, emit,
                                clock=lambda: now[0])
            self.assertFalse(path.exists())
        return [call.args[0] for call in ipmi.set_level.call_args_list], interrupted_reads

    def test_waiting_to_enter_l2_immediately_honors_new_l4_demand(self):
        writes, reads = self.interrupted_calibration(3, lambda executor, at, count: report(3, at, recommended=4))
        self.assertEqual(writes, [5, 4, 3, 4])
        self.assertEqual(len(reads), 1)

    def test_waiting_to_enter_l1_honors_read_failure_floor_without_rechecking_old_raw(self):
        safety = Safety(load_config()["safety"])
        def failed_read(executor, at, count):
            if count == 1:
                safety.update(2, [], [], at - 15, sampled_at=at - 15)
            demand, _ = safety.update(None, [], [], at, read_failed=True, valid_sample=False)
            data = report(2, at, recommended=demand, status="failed")
            # Independently successful status reflects L2 BEFORE the new L3 write.
            data["bmc"]["raw_bytes"] = raw(2)
            return data
        writes, reads = self.interrupted_calibration(2, failed_read)
        self.assertEqual(writes, [5, 4, 3, 2, 3])
        self.assertEqual(len(reads), 5)

    def test_early_l5_readback_is_checked_against_the_latest_actual_target(self):
        def early_alarm(executor, at, count):
            executor.early_l5(report(3, at, recommended=5))
            return report(5, at, recommended=5)
        writes, reads = self.interrupted_calibration(3, early_alarm)
        self.assertEqual(writes, [5, 4, 3, 5])
        self.assertEqual(len(reads), 1)

    def test_post_write_readback_mismatch_is_not_hidden_by_failed_sensor_read(self):
        now = [1.0]
        ipmi = Mock()
        ipmi.set_level.return_value = success()
        executor = Executor(ipmi, None, Mock(), lambda *_: None, threading.Event(), lambda: now[0])
        ipmi.control_status.side_effect = lambda: status_result(executor.target or 1)
        def collect_next():
            if executor.target is None:
                return report(1, now[0])
            data = report(1, now[0], status="failed")
            data["bmc"]["raw_bytes"] = raw(1)
            return data
        def wait(seconds):
            now[0] += seconds
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            with self.assertRaises(ExternalOverride):
                run_calibration(executor, IDENTITY, load_config(), path, collect_next, wait,
                                lambda *_: None, clock=lambda: now[0])
            self.assertFalse(path.exists())
        ipmi.set_level.assert_called_once_with(5)
        self.assertLess(now[0], 20)

    def test_live_high_to_low_calibration_publishes_only_new_stable_samples(self):
        now = [1.0]
        stop = threading.Event()
        ipmi = Mock()
        ipmi.set_level.return_value = success()
        executor = Executor(ipmi, None, Mock(), lambda *_: None, stop, lambda: now[0])
        ipmi.control_status.side_effect = lambda: status_result(executor.target or 1)
        def collect_next():
            now[0] += .1
            return report(executor.target or 1, now[0])
        def wait(seconds):
            now[0] += seconds
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            data = run_calibration(executor, IDENTITY, load_config(), path, collect_next, wait,
                                   lambda *_: None, clock=lambda: now[0])
            self.assertEqual([call.args[0] for call in ipmi.set_level.call_args_list], [5, 4, 3, 2, 1])
            self.assertEqual(load_profile(path, IDENTITY, load_config()), data)

    def test_initial_failures_and_cancel_do_not_publish_or_write(self):
        now = [1.0]
        executor = Executor(Mock(), None, Mock(), lambda *_: None, threading.Event(), lambda: now[0])
        def wait(seconds):
            now[0] += seconds
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "calibration.json"
            with self.assertRaises(CalibrationAborted):
                run_calibration(executor, IDENTITY, load_config(), path,
                                lambda: report(status="failed"), wait, lambda *_: None, clock=lambda: now[0])
            executor.ipmi.set_level.assert_not_called()
            self.assertFalse(path.exists())
            executor.stop.set()
            with self.assertRaises(CollectionCancelled):
                run_calibration(executor, IDENTITY, load_config(), path,
                                lambda: report(at=now[0]), wait, lambda *_: None, clock=lambda: now[0])
            self.assertFalse(path.exists())


class ExternalOverrideTests(unittest.TestCase):
    def executor(self):
        ipmi = Mock()
        ipmi.set_level.return_value = success()
        ipmi.control_status.return_value = status_result(1)
        executor = Executor(ipmi, profile(), Mock(), lambda *_: None, threading.Event(), lambda: 100)
        executor.update(report(1))  # Adopt and confirm the existing physical setting.
        return executor, ipmi

    def test_override_precedes_target_change_even_with_failed_temperature(self):
        for target, failed, mode_changed in ((2, False, False), (5, False, False),
                                           (None, True, False), (1, False, True)):
            with self.subTest(target=target, failed=failed, mode=mode_changed):
                executor, ipmi = self.executor()
                data = report(2, 115, recommended=target, status="failed" if failed else "ok")
                data["bmc"]["raw_bytes"] = raw(1, "0x21") if mode_changed else raw(2)
                with self.assertRaises(ExternalOverride):
                    executor.update(data)
                self.assertEqual(executor.status, "external-override")
                with self.assertRaises(ExternalOverride):
                    executor.request(5)
                ipmi.set_level.assert_not_called()

    def test_write_guard_and_fallback_recheck_external_state(self):
        for use_fallback in (False, True):
            with self.subTest(fallback=use_fallback):
                executor, ipmi = self.executor()
                ipmi.control_status.return_value = status_result(2)
                with self.assertRaises(ExternalOverride):
                    if use_fallback:
                        fallback(executor, Mock(), Mock(), Mock())
                    else:
                        executor.request(5)
                ipmi.set_level.assert_not_called()

    def test_new_write_clears_old_confirmation_and_read_failure_is_not_override(self):
        executor, ipmi = self.executor()
        ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
        executor.request(2)
        self.assertIsNone(executor.confirmed_target)
        executor.observe(report(1, 115))
        self.assertFalse(executor.snapshot()["verified"])
        self.assertFalse(executor.yielded)
        ipmi.set_level.assert_called_once_with(2)

    def test_fallback_returns_on_first_match_with_or_without_profile(self):
        for calibrated in (False, True):
            executor, ipmi = self.executor()
            executor.profile = profile() if calibrated else None
            ipmi.control_status.side_effect = [status_result(1), status_result(3)]
            collect_next, wait = Mock(), Mock()
            self.assertTrue(fallback(executor, collect_next, wait, Mock()))
            ipmi.set_level.assert_called_once_with(3)
            collect_next.assert_not_called()
            wait.assert_not_called()

    def run_with(self, tmp, ipmi, *, mode="auto", collect_impl=None, logger=None, resume=False):
        from argparse import Namespace
        path = Path(tmp) / "p.json"
        if not path.exists():
            publish_profile(path, profile())
        args = Namespace(calibrate=Path(tmp) / "new.json" if mode == "calibrate" else None,
                         auto=mode == "auto",
                         service_recover=mode == "recovery", profile=None if mode == "calibrate" else path,
                         json=True, resume_control=resume)
        config = load_config()
        config["logging"]["enabled"] = False
        if logger is None:
            logger = Mock(error=None, dropped=0)
        def bounded_service(controller, server):
            for _ in range(10):
                if controller.state.data["mode"] != "automatic":
                    raise CollectionCancelled()
                controller.next_poll = 0
                controller.tick(server)
            raise AssertionError("Service test did not reach cancellation or suspension")
        from hrx_fancontrol import control_runtime
        factory = control_runtime.Executor
        test_clock = [100.0]
        collector = Mock(side_effect=collect_impl or CollectionCancelled())
        def timed_collect(*args, **kwargs):
            value = collector(*args, **kwargs)
            test_clock[0] = value["sampled_at_monotonic"]
            return value
        def fast_wait(seconds):
            test_clock[0] += seconds
        if factory is Executor:
            factory = lambda *args: Executor(*args, clock=lambda: test_clock[0])
        with patch("hrx_fancontrol.control_runtime.Executor", side_effect=factory), \
             patch("hrx_fancontrol.service.ServiceController.run", bounded_service), \
             patch("hrx_fancontrol.control_runtime.machine_identity", return_value=IDENTITY), \
             patch("hrx_fancontrol.control_runtime.ControlIPMI", return_value=ipmi), \
             patch("hrx_fancontrol.control_runtime.WriterLock", side_effect=lambda: WriterLock(Path(tmp) / "run")), \
             patch("hrx_fancontrol.control_runtime.control_state_directory", side_effect=lambda: (Path(tmp) / "run")), \
             patch("hrx_fancontrol.control_runtime.collect", side_effect=timed_collect), \
             patch("hrx_fancontrol.control_runtime.AsyncLogger", return_value=logger), \
             patch("hrx_fancontrol.control_runtime.format_log_record", side_effect=lambda data, *args: data), \
             patch("hrx_fancontrol.control_runtime.transition_key", return_value=()), \
             patch("hrx_fancontrol.control_runtime.fallback", side_effect=lambda ex, collect_next, wait, emit: fallback(ex, collect_next, fast_wait, emit, clock=lambda: test_clock[0])), \
             patch("sys.stdout", new_callable=io.StringIO) as output, \
             patch("sys.stderr", new_callable=io.StringIO):
            code = run_control(args, config)
        return code, [json.loads(line) for line in output.getvalue().splitlines()]

    def test_runtime_high_temperature_yields_once_and_requires_explicit_resume(self):
        for mode, initial in (("auto", 1),):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                ipmi = Mock()
                ipmi.set_level.return_value = success()
                calls = [0]
                def collect_impl(*args, **kwargs):
                    calls[0] += 1
                    if calls[0] == 1:
                        return report(initial)
                    preview = report(initial, recommended=5)
                    preview["bmc"]["raw_bytes"] = None
                    kwargs["on_alert"](preview)
                    ipmi.set_level.assert_not_called()  # Early alarm cannot preempt the raw read.
                    return report(3, 115, recommended=5)
                code, events = self.run_with(tmp, ipmi, mode=mode, collect_impl=collect_impl)
                self.assertEqual(code, 0)  # Service stays alive paused until the test cancels it.
                self.assertEqual(sum(event.get("event") == "external_override" for event in events), 1)
                marker = json.loads((Path(tmp) / "run/owner.json").read_text())
                self.assertTrue(marker["yielded"])
                self.assertFalse(marker["active"])
                self.assertEqual(self.run_with(tmp, ipmi)[0], 0)
                self.assertEqual(self.run_with(tmp, ipmi, resume=True)[0], 0)
                self.assertFalse(json.loads((Path(tmp) / "run/owner.json").read_text())["yielded"])
                ipmi.set_level.assert_not_called()

    def test_service_recovery_yield_blocks_automatic_start_even_if_persistence_fails(self):
        for disk_failed in (False, True):
            with self.subTest(disk_failed=disk_failed), tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ, {"INVOCATION_ID": "one", "SERVICE_RESULT": "signal"}, clear=True):
                directory = Path(tmp) / "run"
                with WriterLock(directory):
                    owner = Ownership(directory, IDENTITY, None, "old")
                    owner.before_write(2)
                    owner.confirm(2)
                ipmi = Mock()
                ipmi.control_status.return_value = status_result(1)
                save = Ownership._save
                def maybe_save(owner):
                    if disk_failed and owner.data.get("yielded"):
                        raise OSError("disk unavailable")
                    return save(owner)
                with patch.object(Ownership, "_save", maybe_save):
                    self.assertEqual(self.run_with(tmp, ipmi, mode="recovery")[0], OVERRIDE_EXIT_STATUS)
                    # Emulate systemd starting a fresh invocation after failed ExecStopPost.
                    with patch.dict(os.environ, {"INVOCATION_ID": "two"}):
                        self.assertEqual(self.run_with(tmp, ipmi, collect_impl=lambda *a, **kw: report(1))[0],
                                         OVERRIDE_EXIT_STATUS if disk_failed else 0)
                ipmi.set_level.assert_not_called()

    def test_yield_exit_survives_log_emit_close_and_marker_errors(self):
        with tempfile.TemporaryDirectory() as tmp:
            ipmi = Mock()
            logger = Mock(error=None, dropped=0)
            def emit(data):
                if data.get("event") in ("external_override", "shutdown"):
                    raise OSError("log unavailable")
            logger.emit.side_effect = emit
            logger.close.side_effect = RuntimeError("log close failed")
            polls = iter([report(1), report(2, 115)])
            with patch.object(Ownership, "relinquish", side_effect=OSError("disk unavailable")):
                code, _ = self.run_with(tmp, ipmi, logger=logger, collect_impl=lambda *a, **kw: next(polls))
            self.assertEqual(code, OVERRIDE_EXIT_STATUS)
            ipmi.set_level.assert_not_called()
            with patch.dict(os.environ, {"EXIT_CODE": "exited", "EXIT_STATUS": str(code),
                                         "SERVICE_RESULT": "exit-code", "INVOCATION_ID": "one"}):
                self.assertIsNone(Ownership.recover(Path(tmp) / "run", IDENTITY, profile()))

    def test_calibration_yields_before_raising_to_l5_without_publishing(self):
        helper = WizardTests()
        writes, reads = helper.interrupted_calibration(
            3, lambda executor, at, count: report(2, at, recommended=5), error=ExternalOverride)
        self.assertEqual(writes, [5, 4, 3])
        self.assertEqual(len(reads), 1)

    def test_resume_cannot_clear_other_identity_marker(self):
        for yielded in (False, True):
            with self.subTest(yielded=yielded), tempfile.TemporaryDirectory() as tmp:
                owner = Ownership(tmp, IDENTITY, profile(), "old")
                owner.confirm(2)
                if yielded:
                    owner.relinquish()
                previous = owner.path.read_bytes()
                other = Ownership(tmp, {**IDENTITY, "firmware": "other"}, profile(), "new")
                with self.assertRaises(ValueError):
                    other.check_start(resume=True)
                self.assertEqual(owner.path.read_bytes(), previous)

    def test_failed_yield_persistence_then_missing_status_never_reclaims_control(self):
        timeout = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
        for mode in ("auto", "calibrate", "recovery"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp, \
                 patch.dict(os.environ, {"INVOCATION_ID": "one", "SERVICE_RESULT": "signal"}, clear=True):
                directory = Path(tmp) / "run"
                with WriterLock(directory):
                    owner = Ownership(directory, IDENTITY, None, "old")
                    owner.confirm(2)
                previous = owner.path.read_bytes()
                ipmi = Mock()
                ipmi.control_status.return_value = status_result(1)
                save = Ownership._save
                def save_without_yield(owner):
                    if owner.data.get("yielded"):
                        raise OSError("disk unavailable")
                    return save(owner)
                with patch.object(Ownership, "_save", save_without_yield):
                    self.assertEqual(self.run_with(tmp, ipmi, mode="recovery")[0], OVERRIDE_EXIT_STATUS)
                self.assertEqual(owner.path.read_bytes(), previous)

                # Reproduce the failed acceptance: both poll status and write guard
                # are unavailable while high temperature asks for L5.
                ipmi.control_status.return_value = timeout
                for demand in (1, 5):
                    missing = report(2, recommended=demand, status="failed")
                    code, events = self.run_with(tmp, ipmi, mode=mode,
                                                 collect_impl=[missing, CollectionCancelled()])
                    aborted = mode == "recovery" or (mode == "calibrate" and demand == 5)
                    self.assertEqual(code, 1 if aborted else 0)
                    self.assertEqual(owner.path.read_bytes(), previous)  # Cancellation also retains the gate.
                    self.assertFalse(any(item.get("event") in ("external_override", "write_started") for item in events))
                    self.assertFalse((Path(tmp) / "new.json").exists())
                    ipmi.set_level.assert_not_called()

                # Once a read succeeds, the external setting is recognized, never overwritten.
                ipmi.control_status.return_value = status_result(1)
                self.assertEqual(self.run_with(tmp, ipmi, mode=mode,
                                               collect_impl=[report(1, recommended=5)])[0],
                                 0 if mode == "auto" else OVERRIDE_EXIT_STATUS)
                self.assertTrue(json.loads(owner.path.read_text())["yielded"])
                ipmi.set_level.assert_not_called()

    def test_inherited_baseline_requires_readback_but_not_fresh_rpm_to_allow_control(self):
        failures = (CommandResult(("status",), "", "", None, "IPMI timeout", 3),
                    CommandResult(("status",), "malformed", "", 0, None, .1))
        for failed in failures:
            for fallback_write in (False, True):
                with self.subTest(failed=failed.stdout, fallback=fallback_write):
                    ipmi = Mock()
                    ipmi.set_level.return_value = success()
                    ipmi.control_status.return_value = failed
                    executor = Executor(ipmi, profile(), Mock(), Mock(), threading.Event(), lambda: 100)
                    executor.restore_confirmed(2)
                    target = 3 if fallback_write else 5
                    with self.assertRaises(OwnershipUnverified):
                        executor.request(target, fallback=fallback_write)
                    self.assertTrue(executor.ownership_pending)
                    self.assertFalse(executor.yielded)
                    self.assertFalse(executor.snapshot()["verified"])
                    ipmi.set_level.assert_not_called()

                    # Fresh matching status grants ownership, not RPM verification.
                    ipmi.control_status.return_value = status_result(2)
                    executor.request(target, fallback=fallback_write)
                    self.assertFalse(executor.ownership_pending)
                    self.assertFalse(executor.snapshot()["verified"])
                    ipmi.set_level.assert_called_once_with(target)

    def test_explicit_resume_supersedes_active_record_left_by_failed_persistence(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory = Path(tmp) / "run"
            with WriterLock(directory):
                owner = Ownership(directory, IDENTITY, None, "old")
                owner.confirm(2)
            ipmi = Mock()
            ipmi.set_level.return_value = success()
            ipmi.control_status.return_value = status_result(5)
            code, _ = self.run_with(tmp, ipmi, resume=True,
                                    collect_impl=[report(1, recommended=5), CollectionCancelled()])
            self.assertEqual(code, 0)
            ipmi.set_level.assert_called_once_with(5)
            marker = json.loads(owner.path.read_text())
            self.assertFalse(marker["yielded"])
            self.assertFalse(marker["active"])

    def test_failed_confirmation_save_keeps_observed_state_for_fallback_override_check(self):
        with tempfile.TemporaryDirectory() as tmp:
            ipmi = Mock()
            ipmi.set_level.return_value = success()
            # The immediate L2 readback reaches the failed save; the fallback
            # guard then observes an external change to L1 before another write.
            ipmi.control_status.side_effect = [status_result(2), status_result(1)]
            save = Ownership._save
            failures = []

            def fail_first_confirmation(owner):
                if owner.data.get("confirmed_target") == 2 and not failures:
                    failures.append(2)
                    raise OSError("confirmation save unavailable")
                return save(owner)

            with patch.object(Ownership, "_save", fail_first_confirmation):
                code, events = self.run_with(tmp, ipmi, collect_impl=[report(1, recommended=2)])

            self.assertEqual(failures, [2])
            self.assertEqual(ipmi.control_status.call_count, 2)
            ipmi.set_level.assert_called_once_with(2)
            self.assertEqual(code, OVERRIDE_EXIT_STATUS)
            overrides = [item for item in events if item.get("event") == "external_override"]
            self.assertEqual(len(overrides), 1)
            self.assertFalse(overrides[0]["applied"])
            marker = json.loads((Path(tmp) / "run/owner.json").read_text())
            self.assertTrue(marker["yielded"])
            self.assertFalse(marker["fallback_attempted"])

    def test_failed_confirmation_save_does_not_open_inherited_ownership_gate(self):
        ipmi, owner = Mock(), Mock()
        ipmi.set_level.return_value = success()
        ipmi.control_status.return_value = status_result(2)
        owner.confirm.side_effect = OSError("confirmation save unavailable")
        executor = Executor(ipmi, profile(), owner, Mock(), threading.Event(), lambda: 100)
        executor.restore_confirmed(2)

        with self.assertRaises(OSError):
            executor.request(3, fallback=True)
        self.assertEqual(executor.confirmed_target, 2)
        self.assertTrue(executor.ownership_pending)
        ipmi.set_level.assert_not_called()
        owner.before_write.assert_not_called()

        ipmi.control_status.return_value = CommandResult(("status",), "", "", None, "IPMI timeout", 3)
        with self.assertRaises(OwnershipUnverified):
            executor.request(3, fallback=True)
        ipmi.set_level.assert_not_called()

        owner.confirm.side_effect = None
        ipmi.control_status.return_value = status_result(2)
        executor.request(3, fallback=True)
        self.assertFalse(executor.ownership_pending)
        self.assertFalse(executor.snapshot()["verified"])
        ipmi.set_level.assert_called_once_with(3)

    def test_runtime_modes_use_l3_fallback_and_keep_verification_honest(self):
        for mode in ("auto", "calibrate"):
            with self.subTest(mode=mode), tempfile.TemporaryDirectory() as tmp:
                now = [100]
                ipmi = Mock()
                ipmi.set_level.return_value = success()
                ipmi.control_status.side_effect = [status_result(2), status_result(2), status_result(3)]

                def crash(executor, *_):
                    executor.request(2)
                    raise RuntimeError("control failed")

                def collect_impl(*args, **kwargs):
                    now[0] += 15
                    return report(3, now[0], recommended=1)

                target = "hrx_fancontrol.control_runtime.run_calibration" if mode == "calibrate" else \
                         "hrx_fancontrol.executor.Executor.update"
                with patch(target, side_effect=crash, autospec=True), \
                     patch("hrx_fancontrol.control_runtime.Executor",
                           side_effect=lambda *args: Executor(*args, clock=lambda: now[0])):
                    code, events = self.run_with(tmp, ipmi, mode=mode, collect_impl=collect_impl)
                self.assertEqual(code, 1)  # Recovery does not erase the original execution failure.
                self.assertEqual([call.args[0] for call in ipmi.set_level.call_args_list], [2, 3])
                expected = "fallback_verified"
                recovery = next(item for item in events if item.get("event") == expected)
                self.assertEqual(recovery["control"]["target_level"], 3)
                self.assertIn("L3", recovery["message"])
                self.assertTrue(recovery["applied"])
                self.assertTrue(json.loads((Path(tmp) / "run/owner.json").read_text())["fallback_attempted"])

    def test_service_recovery_replaces_prior_l5_with_verified_l3(self):
        with tempfile.TemporaryDirectory() as tmp, \
             patch.dict(os.environ, {"INVOCATION_ID": "one", "SERVICE_RESULT": "signal"}, clear=True):
            directory = Path(tmp) / "run"
            with WriterLock(directory):
                owner = Ownership(directory, IDENTITY, None, "old")
                owner.before_write(5)
                owner.confirm(5)
            now = [100]
            ipmi = Mock()
            ipmi.set_level.return_value = success()
            ipmi.control_status.side_effect = [status_result(5), status_result(3)]

            def collect_impl(*args, **kwargs):
                now[0] += 15
                return report(3, now[0])

            with patch("hrx_fancontrol.control_runtime.Executor",
                       side_effect=lambda *args: Executor(*args, clock=lambda: now[0])):
                code, events = self.run_with(tmp, ipmi, mode="recovery", collect_impl=collect_impl)
            self.assertEqual(code, 0)
            ipmi.set_level.assert_called_once_with(3)
            verified = next(item for item in events if item.get("event") == "fallback_verified")
            self.assertTrue(verified["applied"])
            self.assertEqual(verified["control"]["stable_samples"], 1)
            marker = json.loads((directory / "owner.json").read_text())
            self.assertTrue(marker["fallback_attempted"])
            self.assertFalse(marker["active"])

    def test_uncalibrated_fallback_rejects_l5_readback(self):
        now = [100]
        ipmi = Mock()
        ipmi.set_level.return_value = success()
        executor = Executor(ipmi, None, Mock(), Mock(), threading.Event(), lambda: now[0])

        def wait(seconds):
            now[0] += seconds

        ipmi.control_status.return_value = status_result(5)
        self.assertFalse(fallback(executor, Mock(), wait, Mock(), lambda: now[0]))
        ipmi.set_level.assert_called_once_with(3)
        self.assertFalse(executor.snapshot()["verified"])
