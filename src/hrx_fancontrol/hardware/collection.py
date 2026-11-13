"""Serial read-only collection, sample validity, and per-run collection accounting."""
from __future__ import annotations

import time
import uuid
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone

from .ipmi import CollectionCancelled, check_cancelled, parse_control_status
from .sensors import parse_sensors
from .sampler import SensorSample


def timestamp():
    return datetime.now(timezone.utc).isoformat()


@dataclass
class CollectionStats:
    run_id: str = field(default_factory=lambda: uuid.uuid4().hex)
    started: int = 0
    completed: int = 0
    successful: int = 0
    failed: int = 0
    timed_out: int = 0
    cancelled: int = 0

    def snapshot(self):
        counts = asdict(self)
        counts.pop("run_id")
        return counts


def _failure_kind(result):
    return result.failure_kind or ("timeout" if result.error == "IPMI timeout" else "command-error")


def collect(ipmi, engine, config, source=None, on_alert=None, stop=None, stats=None):
    """Collect one poll; cancellation never yields a synthetic failed report."""
    started = time.monotonic()
    check_cancelled(stop)
    stats = stats if stats is not None else CollectionStats()
    stats.started += 1
    poll_id = stats.started
    try:
        report = _collect(ipmi, engine, config, source, on_alert, stop, stats, poll_id)
    except CollectionCancelled:
        stats.cancelled += 1
        raise
    collection = report["collection"]
    failed = bool(collection["failure_kinds"])
    stats.completed += 1
    stats.failed += int(failed)
    stats.successful += int(not failed)
    stats.timed_out += int(any(kind.endswith(":timeout") for kind in collection["failure_kinds"]))
    collection.update(status="failed" if failed else "ok", counts=stats.snapshot(),
                      elapsed_seconds=time.monotonic() - started)
    return report


def _collect(ipmi, engine, config, source, on_alert, stop, stats, poll_id):
    sampled_at = time.monotonic()
    commands, errors, failures = [], [], []
    timings = {"sensor_seconds": 0.0, "status_seconds": 0.0}
    sensor_reader = {"path": "offline" if source is not None else "full"}
    if source is not None:
        text = source
    else:
        check_cancelled(stop)
        started = time.monotonic()
        result = ipmi.sensors()
        timings["sensor_seconds"] = time.monotonic() - started
        check_cancelled(stop)
        if isinstance(result, SensorSample):
            commands.extend(result.subcommands)
            sensor_reader = result.reader
        else:
            commands.append(result.summary())
        text = result.stdout if result.succeeded else ""
        if not result.succeeded:
            errors.append(result.error or f"IPMI sensor diagnostic: {result.stderr}")
            failures.append(f"sensor:{_failure_kind(result)}")
    if failures:
        readings = {}
    else:
        readings, parse_errors = parse_sensors(text)
        if parse_errors:
            # A malformed or incomplete table cannot establish a valid complete sample.
            readings = {}
            errors.extend(parse_errors)
            failures.append("sensor:parse")
            if source is None:
                commands[-1] = result.summary(parse_failed=True)
    check_cancelled(stop)
    assessment = engine.assess(readings, time.monotonic(), sampled_at, errors, read_failed=bool(failures))
    report = assessment.report
    report.update(
        timestamp=timestamp(), run_id=stats.run_id, poll_id=poll_id, sampled_at_monotonic=sampled_at,
        source="offline-snapshot" if source is not None else "local-openipmi", commands=commands,
        bmc={"raw_bytes": None, "mode": "unverified", "percent": None},
        collection={"status": "in-progress", "sensor_data_available": bool(readings),
                    # Command success does not make a stale table usable evidence.
                    # Reuse the policy assessment so control and policy agree on validity.
                    "sensor_data_valid": assessment.valid_sample,
                    "failure_kinds": failures, "counts": stats.snapshot(), "timings": timings,
                    "sensor_reader": sensor_reader},
    )
    check_cancelled(stop)
    # Notify a known L5 recommendation before the optional readback can delay it.
    if on_alert and report["recommended_level"] == 5:
        on_alert(report)
    check_cancelled(stop)
    if source is None and config["ipmi"]["read_control_status"]:
        started = time.monotonic()
        result = ipmi.control_status()
        timings["status_seconds"] = time.monotonic() - started
        check_cancelled(stop)
        commands.append(result.summary())
        try:
            if not result.succeeded:
                failures.append(f"status:{_failure_kind(result)}")
                raise ValueError(result.error or result.stderr)
            report["bmc"]["raw_bytes"] = parse_control_status(result.stdout)
            report["bmc"]["read_at_monotonic"] = time.monotonic()
        except ValueError as exc:
            if result.succeeded:
                failures.append("status:parse")
                commands[-1] = result.summary(parse_failed=True)
            report["faults"].append(f"BMC status: {exc}")
    check_cancelled(stop)
    report = engine.finalize(assessment, time.monotonic(), read_failed=bool(failures))
    # Optional status reads and scheduler delays can expire a previously fresh table.
    # Reconcile both consumers and poll accounting with the single final policy commit.
    report["collection"]["sensor_data_valid"] = assessment.valid_sample
    if assessment.stale:
        failures.append("sensor:stale")
    return report


def poll_wait_seconds(report, poll_interval, elapsed):
    """Failed collection gets a full rest; thermal alarms alone keep the normal cadence."""
    if report["collection"]["status"] == "failed":
        return poll_interval
    return max(0, poll_interval - elapsed)
