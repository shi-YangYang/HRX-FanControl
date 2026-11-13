"""Bounded asynchronous JSONL logging with daily retention, without backups."""
from __future__ import annotations

import json
import queue
import re
import sys
import threading
from datetime import datetime, timedelta, timezone
from pathlib import Path


class DailyWriter:
    def __init__(self, directory, retention_days):
        self.directory = Path(directory)
        self.directory.mkdir(parents=True, exist_ok=True)
        self.retention_days = retention_days
        self.last_cleanup = None

    def write(self, record, now=None):
        now = now or datetime.now(timezone.utc)
        day = now.date()
        if self.last_cleanup != day:
            cutoff = day - timedelta(days=self.retention_days - 1)
            for path in self.directory.glob("hrx-*.jsonl"):
                match = re.fullmatch(r"hrx-(\d{4}-\d{2}-\d{2})\.jsonl", path.name)
                if not match or path.is_symlink():
                    continue
                try:
                    file_day = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                except ValueError:
                    continue
                if file_day < cutoff:
                    path.unlink()
            self.last_cleanup = day
        path = self.directory / f"hrx-{day.isoformat()}.jsonl"
        with path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False, allow_nan=False) + "\n")


class AsyncLogger:
    def __init__(self, settings, output=None):
        self.enabled = settings["enabled"]
        self.output = output
        self.active = self.enabled or output is not None
        self.dropped = 0
        self.error = None
        self.queue = queue.Queue(maxsize=settings["queue_capacity"])
        self.stopping = threading.Event()
        self.lock = threading.Lock()
        self.sentinel = object()
        if self.enabled:
            self.writer = DailyWriter(settings["directory"], settings["retention_days"])
        if self.active:
            self.thread = threading.Thread(target=self._work, name="hrx-log", daemon=True)
            self.thread.start()

    def emit(self, record):
        if self.active:
            with self.lock:
                if self.stopping.is_set():
                    self.dropped += 1
                    return
                try:
                    self.queue.put_nowait(record)
                except queue.Full:
                    self.dropped += 1

    def _work(self):
        while True:
            record = self.queue.get()
            try:
                if record is self.sentinel:
                    return
                for write in (self.writer.write if self.enabled else None, self.output):
                    if write is None:
                        continue
                    try:
                        write(record)
                    except (OSError, ValueError) as exc:
                        self.error = str(exc)
                        print(f"Logging failed: {exc}", file=sys.stderr, flush=True)
            finally:
                self.queue.task_done()
            with self.lock:
                if self.stopping.is_set() and self.queue.empty():
                    return

    def close(self):
        if self.active:
            with self.lock:
                if not self.stopping.is_set():
                    self.stopping.set()
                    try:
                        self.queue.put_nowait(self.sentinel)
                    except queue.Full:
                        pass  # The worker drains queued records, then observes stopping.
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                self.error = "Log writer did not finish within the shutdown deadline"


def journal_line(record):
    """One useful service line; full sensor tables stay in interactive/JSON output."""
    line = f"{record['time']} {record.get('message', record['event'])}"
    mode = {"automatic": "自动", "manual": "手动", "paused": "暂停", "suspended": "已让位"}.get(record.get("mode"))
    if mode:
        line += f" 模式={mode}"
    control = record.get("control", {})
    if control.get("parameter") is not None:
        line += f" 参数={control['parameter']}，状态={control.get('status', 'unknown')}"
    sampling = record.get("sampling", {})
    if sampling.get("elapsed_seconds") is not None:
        line += f" 采集={sampling['elapsed_seconds']:.3f}s"
    if record.get("issues"):
        line += " " + "；".join(record["issues"])
    if record.get("errors"):
        line += " " + "；".join(item.get("operation", "读取") + "：" + item.get("message", item["kind"])
                              for item in record["errors"])
    elif sampling.get("sensor_reader", {}).get("fallback_reason"):
        line += " 目录缓存回退：" + sampling["sensor_reader"]["fallback_reason"]
    if control.get("reason"):
        line += " " + control["reason"]
    return line


def transition_key(report):
    reader = report.get("collection", {}).get("sensor_reader", {})
    return (report["recommended_level"], report["safety_state"], tuple(report["faults"]),
            tuple(report["emergencies"]), tuple(report.get("bmc_alarms", [])),
            tuple(sorted(report["sensor_errors"].items())),
            tuple((name, sensor["requested_level"]) for name, sensor in report["sensors"].items()),
            tuple(report.get("bmc", {}).get("raw_bytes") or []),
            tuple(report.get("read_failure", {}).get("trigger_reasons", [])),
            report.get("recovery", {}).get("healthy_samples", 0),
            reader.get("path"), reader.get("fallback_reason"))
