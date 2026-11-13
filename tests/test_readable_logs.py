"""File-log contracts, independent snapshots, and retained diagnostic evidence."""
import copy
import json
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hrx_fancontrol.hardware.collection import CollectionStats, collect
from hrx_fancontrol.config import load_config
from hrx_fancontrol.hardware.ipmi import CommandResult, DIAGNOSTIC_TAIL_CHARS
from hrx_fancontrol.reporting.log_record import format_log_record
from hrx_fancontrol.reporting.logger import AsyncLogger, DailyWriter
from hrx_fancontrol.policy.thermal_engine import ThermalEngine


SAMPLE = (Path(__file__).resolve().parents[1] / "examples/hr650x-observed-sensors.txt").read_text()
STATUS = "20 0f 0f 0f 0f 0f 0f 0f 0f"


def with_value(name, value, status=None):
    rows = []
    for line in SAMPLE.splitlines():
        fields = [part.strip() for part in line.split("|")]
        if fields[0] == name:
            fields[1] = str(value)
            if status is not None:
                fields[3] = status
        rows.append(" | ".join(fields))
    return "\n".join(rows)


class ReadableLogTest(unittest.TestCase):
    def setUp(self):
        self.config = load_config()
        self.engine = ThermalEngine(self.config)
        self.stats = CollectionStats()

    def poll(self, now=0, text=SAMPLE, sensor=None, status=None, on_alert=None):
        ipmi = Mock()
        ipmi.sensors.return_value = sensor or CommandResult(("sensor",), text, "", 0, None, 0.123456, 20)
        ipmi.control_status.return_value = status or CommandResult(("raw",), STATUS, "", 0, None, 0.1, 3)
        with patch("hrx_fancontrol.hardware.collection.time.monotonic", return_value=now):
            return collect(ipmi, self.engine, self.config, stats=self.stats, on_alert=on_alert)

    @staticmethod
    def timeout(operation="sensor", **kwargs):
        return CommandResult((operation,), "", kwargs.get("stderr", ""), None, "IPMI timeout",
                             20.123456, 20, "timeout", stdout_tail=kwargs.get("stdout_tail", ""))

    def test_normal_snapshot_is_compact_and_preserves_detailed_report(self):
        report = self.poll()
        before = copy.deepcopy(report)
        record = format_log_record(report, "summary", 0.223456)
        self.assertEqual(report, before)
        self.assertEqual(record["schema_version"], 2)
        self.assertEqual(record["source"], "local-openipmi")
        self.assertEqual(record["level"], "L1")
        self.assertEqual(record["message"], "采样完成，按温度策略建议 L1。")
        self.assertTrue(record["time"].endswith("+00:00"))
        self.assertNotIn(".", record["time"])
        self.assertEqual(record["sampling"]["elapsed_seconds"], 0.223)
        self.assertEqual(record["sampling"]["counts"], self.stats.snapshot())
        self.assertEqual(record["temperatures_c"]["PCH_Temp"], 63)
        self.assertEqual(record["fans_rpm"]["FAN5_R_Speed"], 3450)
        self.assertEqual(len(record["fans_rpm"]), 6)
        for name in ("NVME_Temp", "RAID_Temp", "PSU2_Temp"):
            self.assertIsNone(record["temperatures_c"][name])
        for field in ("issues", "errors", "recovery", "read_failures", "commands", "bmc", "calibration"):
            self.assertNotIn(field, record)
        self.assertNotIn("CPU1_DTS", record["temperatures_c"])
        self.assertEqual(report["sensors"]["CPU1_DTS"]["observed_value"], 45)
        self.assertEqual(report["bmc"]["raw_bytes"][0], "0x20")
        self.assertEqual(report["calibration"]["status"], "unverified")
        self.assertLess(len(json.dumps(record, ensure_ascii=False).encode()),
                        len(json.dumps(report, ensure_ascii=False).encode()) / 2)

    def test_waiting_and_whole_table_failure_do_not_claim_fan_faults(self):
        record = format_log_record(self.poll(sensor=self.timeout()), "transition", 20.2)
        self.assertIsNone(record["level"])
        self.assertIn("等待有效传感器数据", record["message"])
        self.assertFalse(record["sampling"]["sensor_data_available"])
        self.assertEqual(record["sampling"]["status"], "failed")
        self.assertEqual(record["sampling"]["counts"]["timed_out"], 1)
        self.assertNotIn("issues", record)
        self.assertTrue(all(value is None for value in record["fans_rpm"].values()))
        self.assertEqual(record["read_failures"]["consecutive"], 1)
        self.assertEqual(record["errors"][0]["message"], "读取超时")
        self.assertEqual(record["errors"][0]["operation"], "传感器读取")
        self.assertNotIn("exit_code", record["errors"][0])

    def test_failure_hold_degradation_and_recovery_completion(self):
        self.poll()
        for now in range(1, 6):
            record = format_log_record(self.poll(now, sensor=self.timeout()), "transition", 20.2)
            self.assertEqual(record["level"], "L1" if now < 5 else "L3")
            self.assertEqual(record["recovery"]["healthy_samples"], 0)
            self.assertEqual(record["read_failures"]["consecutive"], now)
            self.assertIn("暂停降档" if now < 5 else "连续读取失败达到阈值", record["message"])
        self.assertEqual(record["read_failures"]["triggers"], ["failure-count"])
        for now in range(6, 11):
            record = format_log_record(self.poll(now), "transition", 0.2)
            if now < 10:
                self.assertEqual(record["recovery"], {"healthy_samples": now - 5, "required_samples": 5})
                self.assertEqual(record["read_failures"]["consecutive"], 0)
            else:
                self.assertNotIn("recovery", record)
                self.assertNotIn("read_failures", record)
                self.assertIn("5/5 次，解除保持", record["message"])

    def test_status_failure_preserves_fresh_temperatures_and_bounded_errors(self):
        record = format_log_record(self.poll(status=self.timeout("raw", stderr="x" * 3000,
                                                                stdout_tail="y" * 3000)), "transition", 20.4)
        self.assertEqual(record["temperatures_c"]["PCH_Temp"], 63)
        self.assertTrue(record["sampling"]["sensor_data_available"])
        self.assertEqual(record["sampling"]["status"], "failed")
        self.assertEqual(record["read_failures"]["seconds_without_data"], 0)
        error, = record["errors"]
        self.assertEqual(error["operation"], "状态读回")
        self.assertEqual(error["elapsed_seconds"], 20.123)
        self.assertEqual(error["timeout_seconds"], 20)
        self.assertEqual(len(error["stderr"]), DIAGNOSTIC_TAIL_CHARS)
        self.assertEqual(len(error["stdout_tail"]), DIAGNOSTIC_TAIL_CHARS)

    def test_bmc_and_real_missing_fan_evidence_are_retained(self):
        record = format_log_record(self.poll(text=with_value("PCH_Temp", 63, "unc")), "transition", 0.2)
        self.assertEqual(record["level"], "L1")
        self.assertEqual(record["issues"], ["PCH_Temp: BMC 温度告警"])
        self.assertIn("采样完成", record["message"])
        self.assertNotIn("正常", record["message"])
        self.assertNotIn("recovery", record)
        for value in ("na", "0"):
            with self.subTest(value=value):
                record = format_log_record(self.poll(1, text=with_value("FAN1_R_Speed", value)), "transition", 0.2)
                self.assertEqual(record["level"], "L3")
                self.assertEqual(record["fans_rpm"]["FAN1_R_Speed"], None if value == "na" else 0)
                self.assertTrue(any(issue.startswith("FAN1_R_Speed:") for issue in record["issues"]))

    def test_invalid_values_and_unknown_diagnostics_remain_visible(self):
        report = self.poll(text=with_value("PCH_Temp", "999"))
        report["faults"].append("firmware-specific fault 73")
        record = format_log_record(report, "transition", 0.2)
        self.assertIsNone(record["temperatures_c"]["PCH_Temp"])
        self.assertIn("PCH_Temp: 温度超出配置的有效范围（原始读数：999.0）", record["issues"])
        self.assertIn("firmware-specific fault 73", record["issues"])
        record = format_log_record(self.poll(1, text=with_value("NVME_Temp", "unknown-token")), "transition", 0.2)
        self.assertIn("NVME_Temp: 读数不可用或不是数值：unknown-token", record["issues"])

    def test_parse_failure_and_unknown_command_error_keep_diagnostics(self):
        report = self.poll(sensor=CommandResult(("sensor",), "broken row", "", 0, None, 0.1, 20),
                           status=CommandResult(("raw",), "", "vendor diagnostic", 17,
                                                "unknown vendor failure", 0.2, 3, "vendor-failure"))
        record = format_log_record(report, "transition", 0.3)
        self.assertEqual(record["errors"][0]["kind"], "parse")
        self.assertEqual(record["errors"][0]["stdout_tail"], "broken row")
        self.assertEqual(record["errors"][1]["message"], "unknown vendor failure")
        self.assertEqual(record["errors"][1]["exit_code"], 17)
        self.assertEqual(record["errors"][1]["stderr"], "vendor diagnostic")
        self.assertTrue(any("格式错误" in issue for issue in record["issues"]))
        self.assertFalse(any("FAN" in issue for issue in record["issues"]))

    def test_early_l5_snapshot_never_inherits_later_failure_or_committed_counts(self):
        records = []
        report = self.poll(text=with_value("PCH_Temp", "80"), status=self.timeout("raw"),
                           on_alert=lambda current: records.append(format_log_record(current, "safety_alert", 0.1)))
        early, = records
        self.assertIn("采样尚未结束", early["message"])
        self.assertEqual(early["sampling"]["status"], "in-progress")
        self.assertEqual(early["sampling"]["counts"]["completed"], 0)
        self.assertEqual(early["sampling"]["counts"]["started"], 1)
        self.assertNotIn("errors", early)
        self.assertNotIn("recovery", early)
        self.assertEqual(report["collection"]["status"], "failed")
        self.assertEqual(report["collection"]["counts"]["failed"], 1)

    def test_queued_record_is_independent_of_later_report_mutation(self):
        report = self.poll(sensor=self.timeout())
        report["logging_health"] = {"dropped_records": 2, "error": "disk full"}
        record = format_log_record(report, "transition", 20.2)
        expected = copy.deepcopy(record)
        started, release, written = threading.Event(), threading.Event(), []

        def write(queued):
            started.set()
            release.wait(2)
            written.append(copy.deepcopy(queued))

        with tempfile.TemporaryDirectory() as directory, patch.object(DailyWriter, "write", side_effect=write):
            logger = AsyncLogger({**self.config["logging"], "directory": directory})
            try:
                logger.emit(record)
                self.assertTrue(started.wait(2))
                report["collection"]["counts"]["failed"] = 99
                report["sensors"]["PCH_Temp"]["celsius"] = 100
                report["fans"].clear()
                report["commands"][0]["stderr"] = "changed"
                report["read_failure"]["trigger_reasons"].append("changed")
                report["logging_health"]["error"] = "changed"
                report["faults"].append("changed")
            finally:
                release.set()
                logger.close()
        self.assertEqual(written, [expected])

    def test_shutdown_counts_and_existing_log_are_preserved(self):
        report = {"timestamp": "2026-11-11T01:02:03.123456+00:00", "mode": "dry-run", "applied": False,
                  "source": "local-openipmi", "run_id": "run", "poll_id": 1, "reason": "requested",
                  "collection_counts": dict(started=1, completed=0, successful=0, failed=0, timed_out=0, cancelled=1)}
        record = format_log_record(report, "shutdown")
        self.assertEqual(record["sampling"]["counts"]["cancelled"], 1)
        self.assertNotIn("level", record)
        self.assertNotIn("temperatures_c", record)
        with tempfile.TemporaryDirectory() as directory:
            writer = DailyWriter(directory, 7)
            writer.write({"event": "old-summary", "recommended_level": 1})
            path, = Path(directory).glob("*.jsonl")
            original = path.read_bytes()
            writer.write(record)
            self.assertTrue(path.read_bytes().startswith(original))
            self.assertEqual(json.loads(path.read_text().splitlines()[1]), record)


if __name__ == "__main__":
    unittest.main()
