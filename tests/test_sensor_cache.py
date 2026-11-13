"""Cached metadata with real fake subprocesses; no physical IPMI access."""
import json
import os
from pathlib import Path
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from hrx_fancontrol.config import load_config
from hrx_fancontrol.hardware.collection import CollectionStats, collect
from hrx_fancontrol.hardware.ipmi import CollectionCancelled, ReadOnlyIPMI
from hrx_fancontrol.hardware.sampler import CacheError, csv_table, selected_names, validate_dump
from hrx_fancontrol.hardware.sensors import parse_sensors
from hrx_fancontrol.policy.thermal_engine import ThermalEngine
from hrx_fancontrol.reporting.log_record import format_log_record

SAMPLE = (Path(__file__).resolve().parents[1] / "examples/hr650x-observed-sensors.txt").read_text()


def sdr_fixture(text):
    """Framed full-sensor metadata; readings/conversions still belong to ipmitool."""
    records = []
    for index, line in enumerate(text.splitlines(), 1):
        name = line.split("|")[0].strip().encode("ascii")
        body = bytearray(43)
        body[42] = 0xc0 | len(name)
        body.extend(name)
        records.append(index.to_bytes(2, "little") + bytes((0x51, 1, len(body))) + body)
    return b"".join(records)


PROGRAM = r'''
import json, os, sys, time
from pathlib import Path
root = Path(os.environ['HRX_CACHE_FAKE'])
args = sys.argv[1:]
with (root / 'calls').open('a') as out:
    out.write(json.dumps(args) + '\n')
mode = (root / 'mode').read_text()
if args == ['-I', 'open', 'raw', '0x0a', '0x20']:
    if mode == 'unsupported':
        print('Unsupported command', file=sys.stderr); sys.exit(1)
    if mode == 'info-timeout':
        time.sleep(10)
    count_file = root / 'repository-count'
    count = int(count_file.read_text()) if count_file.exists() else len((root / 'sample').read_text().splitlines())
    if mode == 'empty': count = 0
    generation = int((root / 'generation').read_text())
    info = bytes([0x51]) + count.to_bytes(2, 'little') + b'\x00\x00' + generation.to_bytes(4, 'little') + b'\x00'*5
    print(' '.join(f'{b:02x}' for b in info)); sys.exit(0)
if args == ['-I', 'open', 'raw', '0x2e', '0x31']:
    print('20 0f 0f 0f 0f 0f 0f 0f 0f'); sys.exit(0)
if args == ['-I', 'open', 'sensor']:
    print((root / 'sample').read_text(), end=''); sys.exit(0)
if args[:4] == ['-I', 'open', 'sdr', 'dump']:
    if mode == 'dump-error':
        print('Cannot dump SDR', file=sys.stderr); sys.exit(1)
    if mode == 'dump-timeout':
        time.sleep(10)
    Path(args[4]).write_bytes((root / 'sdr').read_bytes()); sys.exit(0)
assert args[:3] == ['-I', 'open', '-S'] and args[4:7] == ['-c', 'sensor', 'get'], args
assert Path(args[3]).is_file()
if mode == 'get-sleep':
    (root / 'ready').write_text(str(os.getpid()))
    print('Locating sensor record...', flush=True)
    time.sleep(10)
names = args[7:]
rows = [list(map(str.strip, line.split('|'))) for line in (root / 'sample').read_text().splitlines()]
chosen = [row for row in rows if row[0] in names]
if mode == 'missing': chosen.pop()
if mode == 'duplicate': chosen.append(chosen[0])
if mode == 'bad-status': chosen[0][3] = 'unknown'
if mode == 'get-diagnostic': print('Sensor disappeared', file=sys.stderr)
if mode == 'generation-race':
    (root / 'generation').write_text(str(int((root / 'generation').read_text()) + 1))
    for row in chosen:
        if row[0] == 'PCH_Temp': row[1] = '99'
print('Locating sensor record...')
for row in chosen: print(','.join(row))
sys.exit(255 if any(row[1] == 'na' for row in chosen) else 0)
'''


class SensorCacheTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="hrx-sampler-")
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        for name, value in (("sample", SAMPLE), ("generation", "1"), ("mode", "ok")):
            (self.root / name).write_text(value)
        (self.root / "sdr").write_bytes(sdr_fixture(SAMPLE))
        script = self.root / "ipmitool"
        script.write_text(f"#!{sys.executable}\n" + PROGRAM)
        script.chmod(0o755)
        environment = patch.dict(os.environ, {"PATH": str(self.root) + os.pathsep + os.environ.get("PATH", ""),
                                               "HRX_CACHE_FAKE": str(self.root)})
        environment.start()
        self.addCleanup(environment.stop)
        self.config = load_config()
        self.ipmi = ReadOnlyIPMI(timeout=20, status_timeout=3)
        self.ipmi.enable_sensor_cache((*self.config["sensors"], *self.config["fan"]["installed"]), self.root)
        self.addCleanup(self.ipmi.close)

    def mode(self, value):
        (self.root / "mode").write_text(value)

    def calls(self):
        return [json.loads(line) for line in (self.root / "calls").read_text().splitlines()]

    def test_warm_cache_preserves_live_values_thresholds_unknown_alarms_and_na_exit(self):
        first = self.ipmi.sensors()
        self.assertTrue(first.succeeded)
        self.assertEqual(first.reader["path"], "discovery")
        self.assertEqual(first.reader["sensors"], 43)
        cache = self.ipmi.sensor_reader.path
        self.assertEqual(cache.stat().st_mode & 0o777, 0o600)
        self.assertEqual(cache.parent.stat().st_mode & 0o777, 0o700)
        full_policy = collect(self.ipmi, ThermalEngine(self.config), self.config, source=SAMPLE)
        warm_policy = collect(self.ipmi, ThermalEngine(self.config), self.config)
        for key in ("sensors", "fans", "component_level", "recommended_level", "bmc_alarms", "faults", "emergencies"):
            self.assertEqual(warm_policy[key], full_policy[key], key)
        updated = []
        for line in SAMPLE.splitlines():
            fields = list(map(str.strip, line.split("|")))
            if fields[0] == "PCH_Temp":
                fields[1], fields[7] = "66.000", "82.000"
            if fields[0] == "NVME_Temp":
                fields[1:4] = ["76.000", "degrees C", "ucr"]
            updated.append(" | ".join(fields))
        (self.root / "sample").write_text("\n".join(updated) + "\n")
        report = collect(self.ipmi, ThermalEngine(self.config), self.config)
        self.assertEqual(report["collection"]["sensor_reader"]["path"], "cached")
        self.assertEqual(report["sensors"]["PCH_Temp"]["celsius"], 66)
        self.assertEqual(report["recommended_level"], 5)  # NVMe live BMC alarm, no curve.
        sample = self.ipmi.sensors()
        rows, errors = parse_sensors(sample.stdout)
        self.assertFalse(errors)
        self.assertEqual(rows["pch_temp"].upper_warning, 82)
        accepted = [item for item in sample.subcommands if item.get("accepted_unavailable")]
        self.assertEqual(len(accepted), 1)
        self.assertEqual(accepted[0]["returncode"], 255)
        self.assertEqual(accepted[0]["failure_kind"], "nonzero-exit")
        self.assertNotIn("captured_stdout", accepted[0])
        record = format_log_record(report, "summary")
        self.assertIsNotNone(record["sampling"]["elapsed_seconds"])
        self.assertGreater(record["sampling"]["timings"]["sensor_seconds"], 0)
        self.assertNotIn("errors", record)
        calls = self.calls()
        self.assertEqual(sum(args == ["-I", "open", "sensor"] for args in calls), 1)
        self.assertEqual(sum(args[2:4] == ["sdr", "dump"] for args in calls), 1)
        self.ipmi.close()
        self.assertFalse(cache.exists())

    def test_unconfigured_na_sensor_is_read_when_it_returns_as_temperature(self):
        initial = SAMPLE + "Unknown_Temp | na | | na | na | na | na | 70 | 75 | 80\n"
        (self.root / "sample").write_text(initial)
        (self.root / "sdr").write_bytes(sdr_fixture(initial))
        self.assertTrue(self.ipmi.sensors().succeeded)
        self.assertIn("Unknown_Temp", self.ipmi.sensor_reader.names)
        (self.root / "sample").write_text(initial.replace("Unknown_Temp | na | | na", "Unknown_Temp | 76 | degrees C | ucr"))
        current = collect(self.ipmi, ThermalEngine(self.config), self.config)
        self.assertEqual(current["collection"]["sensor_reader"]["path"], "cached")
        self.assertEqual(current["recommended_level"], 5)
        self.assertIn("Unknown_Temp: BMC threshold alarm", current["emergencies"])
        self.assertEqual(current["sensors"]["Unknown_Temp"]["celsius"], 76)

    def test_generation_change_refreshes_and_inflight_change_discards_old_values(self):
        self.assertTrue(self.ipmi.sensors().succeeded)
        (self.root / "generation").write_text("2")
        self.assertEqual(self.ipmi.sensors().reader["path"], "discovery")
        self.mode("generation-race")
        result = self.ipmi.sensors()
        self.assertTrue(result.succeeded)
        self.assertEqual(result.reader["path"], "full")
        self.assertIn("changed", result.reader["fallback_reason"])
        self.assertEqual(parse_sensors(result.stdout)[0]["pch_temp"].value, 63)
        self.assertIsNone(self.ipmi.sensor_reader.path)

    def test_malformed_or_diagnostic_get_never_becomes_a_valid_partial_sample(self):
        for mode in ("missing", "duplicate", "bad-status", "get-diagnostic"):
            with self.subTest(mode=mode):
                self.mode("ok")
                self.ipmi.sensor_reader.retry_at = 0
                self.assertTrue(self.ipmi.sensors().succeeded)
                self.mode(mode)
                result = self.ipmi.sensors()
                self.assertTrue(result.succeeded)  # Newly read full table is the fallback evidence.
                self.assertEqual(result.reader["path"], "full")
                self.assertIn("fallback_reason", result.reader)
                self.assertEqual(len(parse_sensors(result.stdout)[0]), 163)
                self.assertEqual(self.calls()[-1], ["-I", "open", "sensor"])

    def test_cache_corruption_fallback_and_dump_failure_reuses_confirmed_same_poll_full(self):
        initial = self.ipmi.sensors()
        self.assertTrue(initial.succeeded)
        self.assertEqual(initial.reader["path"], "discovery", initial.reader)
        self.ipmi.sensor_reader.path.write_bytes(b"corrupt")
        result = self.ipmi.sensors()
        self.assertTrue(result.succeeded)
        self.assertIn("changed on disk", result.reader["fallback_reason"])
        self.ipmi.sensor_reader.retry_at = 0
        self.mode("dump-error")
        before = len(self.calls())
        result = self.ipmi.sensors()
        self.assertTrue(result.succeeded)
        calls = self.calls()[before:]
        self.assertEqual(sum(args == ["-I", "open", "sensor"] for args in calls), 1)
        self.assertEqual(calls[-1], ["-I", "open", "raw", "0x0a", "0x20"])
        self.assertEqual(result.reader["path"], "full")

    def test_empty_repository_never_invokes_discovery_and_metadata_timeout_can_fallback(self):
        self.mode("empty")
        result = self.ipmi.sensors()
        self.assertFalse(result.succeeded)
        self.assertIn("empty", result.error)
        self.assertEqual(len(self.calls()), 1)
        self.ipmi.status_timeout = .2
        self.mode("info-timeout")
        result = self.ipmi.sensors()
        self.assertTrue(result.succeeded)
        self.assertEqual(result.reader["path"], "full")
        self.assertLess(result.elapsed_seconds, 2)
        self.assertTrue(any(command["failure_kind"] == "timeout" for command in result.subcommands))

    def test_total_deadline_and_cooldown_recover_without_repeating_dump(self):
        self.ipmi.timeout = 1.5
        self.mode("dump-timeout")
        started = time.monotonic()
        result = self.ipmi.sensors()
        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure_kind, "timeout")
        self.assertLess(time.monotonic() - started, 3)
        before = len(self.calls())
        result = self.ipmi.sensors()
        self.assertTrue(result.succeeded)
        self.assertFalse(any(args[2:4] == ["sdr", "dump"] for args in self.calls()[before:]))

    def test_unsupported_metadata_falls_back_and_periodic_refresh_rebuilds_cache(self):
        self.assertEqual(self.ipmi.sensors().reader["path"], "discovery")
        self.ipmi.sensor_reader.created -= 3601
        self.assertEqual(self.ipmi.sensors().reader["path"], "discovery")
        self.mode("unsupported")
        result = self.ipmi.sensors()
        self.assertTrue(result.succeeded)
        self.assertEqual(result.reader["path"], "full")
        self.assertIn("Unsupported", result.reader["fallback_reason"])
        self.mode("ok")
        cooldown = self.ipmi.sensors()
        self.assertEqual(cooldown.reader["fallback_reason"], result.reader["fallback_reason"])

    def test_dump_missing_oem_record_keeps_healthy_samples_and_enters_cooldown(self):
        # The repository has one non-sensor OEM record absent from this framed dump.
        (self.root / "repository-count").write_text(str(len(SAMPLE.splitlines()) + 1))
        engine, stats = ThermalEngine(self.config), CollectionStats()
        reports = [collect(self.ipmi, engine, self.config, stats=stats) for _ in range(5)]

        self.assertEqual([report["collection"]["status"] for report in reports], ["ok"] * 5)
        self.assertTrue(all(report["collection"]["sensor_data_valid"] for report in reports))
        self.assertEqual([report["recommended_level"] for report in reports], [1] * 5)
        self.assertEqual(reports[-1]["collection"]["counts"]["successful"], 5)
        self.assertEqual(reports[-1]["collection"]["counts"]["failed"], 0)
        self.assertEqual(reports[-1]["read_failure"]["trigger_reasons"], [])
        for report in reports:
            reader = report["collection"]["sensor_reader"]
            self.assertEqual(reader["path"], "full")
            self.assertIn("record count", reader["fallback_reason"])
        self.assertIsNone(self.ipmi.sensor_reader.path)
        self.assertGreater(self.ipmi.sensor_reader.retry_at, time.monotonic())
        calls = self.calls()
        self.assertEqual(sum(args == ["-I", "open", "sensor"] for args in calls), 5)
        self.assertEqual(sum(args[2:4] == ["sdr", "dump"] for args in calls), 1)
        self.assertFalse(any("get" in args for args in calls))

    def test_complete_inventory_rejects_a_sensor_table_with_missing_rows(self):
        # A complete SDR still contains the sensor omitted from the successful table output.
        (self.root / "repository-count").write_text(str(len(SAMPLE.splitlines())))
        (self.root / "sample").write_text("\n".join(SAMPLE.splitlines()[:-1]) + "\n")
        result = self.ipmi.sensors()
        self.assertFalse(result.succeeded)
        self.assertEqual(result.failure_kind, "parse")
        self.assertIn("SDR inventory does not match", result.error)
        self.assertEqual(result.stdout, "")
        self.assertIsNone(self.ipmi.sensor_reader.path)

    def test_cancellation_during_cached_get_is_not_a_partial_sample_and_reaps_child(self):
        self.assertTrue(self.ipmi.sensors().succeeded)
        self.mode("get-sleep")
        stop = self.ipmi.stop = threading.Event()
        errors = []
        def run():
            try:
                self.ipmi.sensors()
            except Exception as exc:
                errors.append(exc)
        worker = threading.Thread(target=run)
        worker.start()
        try:
            deadline = time.monotonic() + 3
            ready = self.root / "ready"
            while not (ready.exists() and ready.stat().st_size) and time.monotonic() < deadline:
                time.sleep(.01)
            self.assertTrue(ready.exists() and ready.stat().st_size)
            pid = int(ready.read_text())
            stop.set()
            worker.join(2)
            self.assertFalse(worker.is_alive())
            self.assertEqual(len(errors), 1)
            self.assertIsInstance(errors[0], CollectionCancelled)
            with self.assertRaises(ProcessLookupError):
                os.kill(pid, 0)
        finally:
            stop.set()
            worker.join(3)

    def test_parser_rejects_unexplained_nonzero_and_truncated_inventory(self):
        names, count = selected_names(SAMPLE, set())
        self.assertIn("NVME_Temp", names)
        csv = "Locating sensor record...\nPCH_Temp,63.000,degrees C,ok,na,na,na,83.000,85.000,105.000\n"
        with self.assertRaises(CacheError):
            csv_table(csv, ("PCH_Temp",), 255)
        valid = sdr_fixture(SAMPLE)
        validate_dump(valid, count, count)
        for broken in (valid[:-1], valid + valid[:60], valid[:5] + b"\0"):
            with self.assertRaises(CacheError):
                validate_dump(broken, count, count)
        named = bytearray(sdr_fixture("PCH_Temp | 63\n"))
        named[47] = 0xd0  # Claims a 16-byte name beyond this record's actual payload.
        with self.assertRaises(CacheError):
            validate_dump(named, 1, 1)

    def test_option_like_sensor_name_never_reaches_cached_subprocess(self):
        table = SAMPLE + "-I | 76 | degrees C | ucr | na | na | na | 70 | 75 | 80\n"
        (self.root / "sample").write_text(table)
        (self.root / "sdr").write_bytes(sdr_fixture(table))
        result = self.ipmi.sensors()
        self.assertTrue(result.succeeded)
        self.assertEqual(result.reader["path"], "full")
        self.assertFalse(any("get" in args for args in self.calls()))
        self.assertEqual(sum(args == ["-I", "open", "sensor"] for args in self.calls()), 1)
        with patch.object(self.ipmi, "_execute") as execute:
            with self.assertRaises(ValueError):
                self.ipmi.cached_sensors(self.root / "inventory", ("-H",), 1)
            execute.assert_not_called()


if __name__ == "__main__":
    unittest.main()
