"""Explicit high-to-low calibration using only live, post-write measurements."""
import time

from .calibration import (SETTLE_SECONDS, SAMPLE_INTERVAL, STABLE_SAMPLES, STEP_TIMEOUT,
                          profile_ranges, publish_profile, rpm_ranges, validate_profile)
from ..hardware.collection import timestamp
from ..hardware.ipmi import check_cancelled
from ..hardware.protocol import FANS, LEVEL_BYTES, POLICY_LABELS
from .executor import ExecutionFailure


class CalibrationAborted(ValueError):
    """Calibration cannot safely continue; this is not proof of actuator failure."""


def healthy(report):
    return (report["collection"]["status"] == "ok" and report["collection"]["sensor_data_valid"] and
            report["component_level"] is not None and
            not report["faults"] and not report["emergencies"] and not report.get("bmc_alarms") and
            not report.get("recovery", {}).get("holding") and
            all(name in report["fans"] and not report["fans"][name]["error"] for name in FANS))


def run_calibration(executor, identity, config, path, collect_next, wait, emit, clock=time.monotonic):
    def checked_collect():
        previous_target = executor.target
        report = collect_next()
        # An early L5 callback may already have changed the target before this
        # status read. A request made below, however, cannot use this old readback.
        executor.check_readback(report)
        if previous_target is not None:
            demand = report["recommended_level"]
            if demand is not None and demand > executor.target:
                executor.request(demand)
            if executor.target > previous_target:
                raise CalibrationAborted(f"Thermal policy now requires L{executor.target}; calibration stopped")
        return report

    origin = clock()
    profile = {"schema_version": 1, "created_at": timestamp(), "identity": identity,
               "fan_names": FANS[:], "policy_labels": POLICY_LABELS[:], "levels": {}}
    for level in (5, 4, 3, 2, 1):
        deadline = clock() + STEP_TIMEOUT
        while True:
            report = checked_collect()
            if healthy(report) and report["recommended_level"] <= level:
                break
            if clock() >= deadline:
                raise CalibrationAborted(f"Cannot safely enter L{level}; demand, fault, or recovery hold remains")
            wait(config["controller"]["poll_interval_seconds"])
        check_cancelled(executor.stop)
        executor.request(level)
        completed = executor.completed_at
        samples = []
        unstable_windows = 0
        deadline = completed + STEP_TIMEOUT
        while clock() < deadline:
            wait(config["controller"]["poll_interval_seconds"])
            report = checked_collect()
            if not healthy(report):
                samples = []
                unstable_windows = 0
                continue
            sampled_at = report["sampled_at_monotonic"]
            if sampled_at < completed + SETTLE_SECONDS:
                continue
            if samples and sampled_at - origin - samples[-1]["sampled_elapsed"] < SAMPLE_INTERVAL:
                continue
            sample = {"timestamp": report["timestamp"], "sampled_elapsed": sampled_at - origin,
                      "rpm": {name: report["fans"][name]["rpm"] for name in FANS},
                      "raw_bytes": report["bmc"]["raw_bytes"][:]}
            samples.append(sample)
            samples = samples[-STABLE_SAMPLES:]
            if len(samples) < STABLE_SAMPLES:
                continue
            try:
                rpm_ranges(samples)
            except ValueError:
                unstable_windows += 1
                continue
            profile["levels"][str(level)] = {"byte": LEVEL_BYTES[level],
                "write_completed_elapsed": completed - origin, "samples": samples}
            emit("calibration_level", {"target_level": level, "status": "measured", "samples": samples})
            break
        else:
            if unstable_windows >= STABLE_SAMPLES:
                raise ExecutionFailure(f"Persistent unstable fresh RPM within {STEP_TIMEOUT:g}s at L{level}")
            raise CalibrationAborted(f"Insufficient fresh measurements within {STEP_TIMEOUT:g}s at L{level}")
    ranges = profile_ranges(profile["levels"])
    for level, item in profile["levels"].items():
        item["rpm_ranges"] = ranges[level]
    validate_profile(profile, identity, config)
    check_cancelled(executor.stop)
    publish_profile(path, profile)
    emit("calibration_complete", {"status": "published", "path": str(path)})
    return profile
