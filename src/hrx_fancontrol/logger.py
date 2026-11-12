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
    def __init__(self, settings):
        self.enabled = settings["enabled"]
        self.dropped = 0
        self.error = None
        self.queue = queue.Queue(maxsize=settings["queue_capacity"])
        self.stopping = threading.Event()
        if self.enabled:
            self.writer = DailyWriter(settings["directory"], settings["retention_days"])
            self.thread = threading.Thread(target=self._work, name="hrx-log", daemon=True)
            self.thread.start()

    def emit(self, record):
        if self.enabled:
            try:
                self.queue.put_nowait(record)
            except queue.Full:
                self.dropped += 1

    def _work(self):
        while not self.stopping.is_set() or not self.queue.empty():
            try:
                record = self.queue.get(timeout=0.1)
            except queue.Empty:
                continue
            try:
                self.writer.write(record)
            except (OSError, ValueError) as exc:
                self.error = str(exc)
                print(f"Logging failed: {exc}", file=sys.stderr, flush=True)
            finally:
                self.queue.task_done()

    def close(self):
        if self.enabled:
            self.stopping.set()
            self.thread.join(timeout=3)
            if self.thread.is_alive():
                self.error = "Log writer did not finish within the shutdown deadline"


def transition_key(report):
    return (report["recommended_level"], report["safety_state"], tuple(report["faults"]),
            tuple(report["emergencies"]), tuple(report.get("bmc_alarms", [])),
            tuple(sorted(report["sensor_errors"].items())),
            tuple((name, sensor["requested_level"]) for name, sensor in report["sensors"].items()),
            tuple(report.get("bmc", {}).get("raw_bytes") or []),
            tuple(report.get("read_failure", {}).get("trigger_reasons", [])),
            report.get("recovery", {}).get("healthy_samples", 0))
