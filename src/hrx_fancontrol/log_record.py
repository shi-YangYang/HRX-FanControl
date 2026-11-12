"""Compact, independent file-log snapshots; detailed CLI reports remain untouched."""
from __future__ import annotations

import re
from datetime import datetime

from .ipmi import DIAGNOSTIC_TAIL_CHARS


_REASONS = {
    "missing sensor": "缺少传感器读数",
    "missing installed fan": "缺少必需风扇读数",
    "stale sample": "采样已过期",
    "Stale sample or gap in sensor updates": "采样已过期或采样间隔过长",
    "zero reading is unverified or hardware is absent": "零值尚未确认有效，或硬件未安装",
    "temperature outside configured plausible range": "温度超出配置的有效范围",
    "fan stopped or below configured RPM floor": "风扇停转或转速低于配置下限",
    "expected RPM unit": "转速单位不是 RPM",
    "BMC threshold alarm": "BMC 温度告警",
    "configured emergency temperature": "达到自定紧急温度",
    "unconfigured thermal sensor": "温度传感器未配置",
    "duplicate sensor name": "传感器名称重复",
    "No sensor rows received": "未收到传感器数据行",
    "IPMI timeout": "读取超时",
    "IPMI diagnostic output": "命令返回诊断信息",
    "IPMI output parse failed": "读取结果无法解析",
    "Expected nine hexadecimal BMC status bytes; firmware layout unverified":
        "状态读回应为九个十六进制字节，固件布局尚未确认",
    "failure-count": "连续读取失败达到阈值",
    "no-valid-sample-timeout": "距有效温度采样的时间达到阈值",
}
_PREFIXES = {
    "unavailable or nonnumeric value: ": "读数不可用或不是数值：",
    "unavailable or unsupported status: ": "读数状态不可用或不支持：",
    "expected Celsius, received ": "温度单位不是摄氏度：",
    "BMC fan alarm: ": "BMC 风扇告警：",
    "BMC status: ": "状态读回：",
    "Duplicate sensor: ": "传感器重复：",
    "ipmitool exit ": "ipmitool 退出码：",
}


def _explain(text):
    if text in _REASONS:
        return _REASONS[text]
    for prefix, explanation in _PREFIXES.items():
        if text.startswith(prefix):
            return explanation + _explain(text[len(prefix):])
    match = re.fullmatch(r"Malformed sensor row (\d+): expected 10 pipe-separated columns", text)
    if match:
        return f"传感器第 {match[1]} 行格式错误：应有 10 个竖线分隔字段"
    # Preserve sensor names and unknown diagnostics instead of replacing them with "error".
    name, separator, reason = text.partition(": ")
    if separator:
        return name + separator + _explain(reason)
    return text


def _failed(command):
    return bool(command.get("failure_kind") or command.get("error") or
                command.get("stderr") or command.get("returncode") != 0)


def _command_errors(report):
    errors = []
    for command in report.get("commands", []):
        if not _failed(command):
            continue
        args = command.get("command", ())
        operation = "传感器读取" if args and args[-1] == "sensor" else "状态读回"
        kind = command.get("failure_kind") or (
            "timeout" if command.get("error") == "IPMI timeout" else "command-error")
        error = {"operation": operation, "kind": kind}
        if command.get("error"):
            error["message"] = _explain(command["error"])
        for key in ("elapsed_seconds", "timeout_seconds"):
            if command.get(key) is not None:
                error[key] = round(command[key], 3)
        if command.get("returncode") is not None:
            error["exit_code"] = command["returncode"]
        for key in ("stderr", "stdout_tail"):
            if command.get(key):
                error[key] = command[key][-DIAGNOSTIC_TAIL_CHARS:]
        errors.append(error)
    return errors


def _issues(report):
    collection = report["collection"]
    # Missing values derived from a failed whole-table read are not hardware evidence.
    unavailable = not collection["sensor_data_available"] and any(
        failure.startswith("sensor:") for failure in collection["failure_kinds"])
    derived = set()
    if unavailable:
        for group in ("sensors", "fans"):
            derived.update(f"{name}: {item['error']}" for name, item in report[group].items()
                           if item.get("error"))
    # Command diagnostics are retained in errors; do not repeat them as hardware issues.
    command_errors = {command["error"] for command in report.get("commands", [])
                      if command.get("error")}
    faults = list(report["faults"])
    for name, error in report["sensor_errors"].items():
        raw = f"{name}: {error}"
        # Only expected optional absence is quiet. Unknown/invalid optional data stays diagnosable.
        optional_absence = error.casefold() in (
            "missing sensor", "zero reading is unverified or hardware is absent",
            "unavailable or nonnumeric value: na", "unavailable or unsupported status: na")
        if raw not in faults and not optional_absence:
            faults.append(raw)
    issues = []
    for raw in (*faults, *report["emergencies"], *report.get("bmc_alarms", [])):
        if raw in derived or raw in command_errors or (
            raw.startswith("BMC status: ") and raw[len("BMC status: "):] in command_errors
        ):
            continue
        explanation = _explain(raw)
        name = raw.partition(": ")[0]
        sensor = report["sensors"].get(name)
        if sensor and sensor["observed_value"] is not None and (
            sensor["celsius"] is None or re.fullmatch(r"CPU\d+_DTS", name, re.IGNORECASE)
        ):
            explanation += f"（原始读数：{sensor['observed_value']}）"
        if explanation not in issues:
            issues.append(explanation)
    return issues


def _message(report, event):
    level = report["recommended_level"]
    recommendation = f"建议 L{level}" if level is not None else "暂无档位建议"
    state = report["safety_state"]
    recovery = report.get("recovery", {})
    if state == "waiting-for-sample":
        message = "等待有效传感器数据，暂无档位建议。"
        if report["collection"]["status"] == "failed":
            message = "读取失败，" + message
    elif state == "read-failure-degraded":
        reasons = report.get("read_failure", {}).get("trigger_reasons", [])
        message = f"{'、'.join(_explain(reason) for reason in reasons)}，启用读取故障保护，{recommendation}。"
    elif state == "read-failure-hold":
        message = f"读取失败，暂停降档，本轮{recommendation}。"
    elif state == "recovery":
        message = (f"恢复观察中，连续健康采样 {recovery['healthy_samples']}/"
                   f"{recovery['required_samples']} 次，继续保持，{recommendation}。")
    elif state in ("safe-mode", "emergency"):
        message = f"检测到传感器、风扇故障或紧急温度告警，保护{recommendation}，原因见 issues。"
    else:
        drivers = [name for name, sensor in report["sensors"].items()
                   if sensor["celsius"] is not None and sensor["held_level"] == level]
        reason = "、".join(drivers) + " 的温度策略" if level and level > 1 and drivers else "温度策略"
        message = f"采样完成，按{reason}{recommendation}。"
        if recovery.get("healthy_samples", 0) >= recovery.get("required_samples", float("inf")):
            message = f"连续健康采样 {recovery['healthy_samples']}/{recovery['required_samples']} 次，解除保持。" + message
    if report.get("bmc_alarms"):
        message += "存在 BMC 温度告警，详见 issues。"
    if event == "safety_alert":
        if report["collection"]["status"] == "in-progress":
            # The early assessment is not a committed poll or a recovery update.
            message = "警告：当前建议档位已达到 L5。采样尚未结束，本轮计数尚未结算。"
        else:
            message = "警告：当前建议档位已达到 L5。" + message
    return message


def format_log_record(report, event, elapsed_seconds=None):
    """Return a v2 record with no mutable objects shared with the detailed report."""
    record = {
        "schema_version": 2,
        "time": datetime.fromisoformat(report["timestamp"]).isoformat(timespec="seconds"),
        "event": event,
        "run_id": report["run_id"],
        "poll_id": report["poll_id"],
        "source": report["source"],
        "mode": report["mode"],
        "applied": report["applied"],
    }
    if event == "shutdown":
        reason = report["reason"]
        record.update(message="收到停止请求，采集结束。" if reason == "requested" else "单次采集结束。",
                      reason=reason, sampling={"counts": dict(report["collection_counts"])})
        return record

    collection = report["collection"]
    level = report["recommended_level"]
    record.update(
        message=_message(report, event), level=f"L{level}" if level is not None else None,
        temperatures_c={name: sensor["celsius"] for name, sensor in report["sensors"].items()
                        if not re.fullmatch(r"CPU\d+_DTS", name, re.IGNORECASE)},
        fans_rpm={name: fan["rpm"] if not fan["error"] or
                  fan["error"] == "fan stopped or below configured RPM floor" or
                  fan["error"].startswith("BMC fan alarm: ") else None
                  for name, fan in report["fans"].items()},
        sampling={"status": collection["status"],
                  "sensor_data_available": collection["sensor_data_available"],
                  "elapsed_seconds": round(elapsed_seconds, 3) if elapsed_seconds is not None else None,
                  "counts": dict(collection["counts"])},
    )
    if "control" in report:
        from copy import deepcopy
        record["control"] = deepcopy({key: value for key, value in report["control"].items() if key != "command"})
    issues = _issues(report)
    if issues:
        record["issues"] = issues
    recovery = report.get("recovery", {})
    if recovery.get("holding"):
        record["recovery"] = {key: recovery[key] for key in ("healthy_samples", "required_samples")}
    failure = report.get("read_failure", {})
    if failure.get("consecutive_failures") or failure.get("trigger_reasons"):
        record["read_failures"] = {
            "consecutive": failure["consecutive_failures"],
            "seconds_without_data": round(failure["no_valid_sample_seconds"], 3),
        }
        if failure["trigger_reasons"]:
            record["read_failures"]["triggers"] = list(failure["trigger_reasons"])
    errors = _command_errors(report)
    if errors:
        record["errors"] = errors
    health = report.get("logging_health", {})
    if health.get("dropped_records") or health.get("error"):
        record["logging_health"] = {key: value for key, value in health.items() if value}
    return record
