"""Strict, versioned machine-bound calibration evidence and atomic publication."""
import json
import math
import os
import statistics
import tempfile
from datetime import datetime
from pathlib import Path

from .protocol import FANS, LEVEL_BYTES, POLICY_LABELS, matches_level

SETTLE_SECONDS = 10.0
SAMPLE_INTERVAL = 5.0
STABLE_SAMPLES = 3
STEP_TIMEOUT = 180.0


def number(value):
    return type(value) in (int, float) and math.isfinite(value)


def rpm_ranges(samples):
    ranges = {}
    for name in FANS:
        values = [sample["rpm"][name] for sample in samples]
        mean = statistics.mean(values)
        if max(values) - min(values) > max(200, mean * .10):
            raise ValueError(f"Unstable RPM samples for {name}")
        margin = max(200, mean * .15)
        ranges[name] = [max(1, min(values) - margin), max(values) + margin]
    return ranges


def profile_ranges(levels):
    """Bound distribution tolerances by adjacent measured midpoints, per physical fan."""
    result = {key: rpm_ranges(value["samples"]) for key, value in levels.items()}
    for name in FANS:
        for lower in range(1, 5):
            low_values = [sample["rpm"][name] for sample in levels[str(lower)]["samples"]]
            high_values = [sample["rpm"][name] for sample in levels[str(lower + 1)]["samples"]]
            midpoint = (statistics.mean(low_values) + statistics.mean(high_values)) / 2
            if max(low_values) >= midpoint or min(high_values) <= midpoint:
                raise ValueError(f"Measured RPM distributions must increase: {name} L{lower}/L{lower+1}")
            result[str(lower)][name][1] = min(result[str(lower)][name][1], midpoint - .5)
            result[str(lower + 1)][name][0] = max(result[str(lower + 1)][name][0], midpoint + .5)
    return result


def _timestamp(value):
    if not isinstance(value, str):
        raise ValueError("Evidence timestamp must be a string")
    parsed = datetime.fromisoformat(value)
    if parsed.tzinfo is None:
        raise ValueError("Evidence timestamps must include timezone")
    return parsed


def validate_profile(profile, identity, config):
    """Do not trust a verified flag, caller-provided ranges, or a historical mean."""
    if not isinstance(profile, dict) or type(profile.get("schema_version")) is not int or profile["schema_version"] != 1:
        raise ValueError("Unsupported calibration schema")
    if profile.get("identity") != identity:
        raise ValueError("Calibration belongs to a different machine or BMC firmware")
    if (profile.get("fan_names") != FANS or config["fan"]["installed"] != FANS or
            profile.get("policy_labels") != POLICY_LABELS or config["fan"]["levels"] != POLICY_LABELS):
        raise ValueError("Calibration fan layout or policy labels do not match")
    _timestamp(profile.get("created_at"))
    levels = profile.get("levels")
    if not isinstance(levels, dict) or set(levels) != {str(level) for level in LEVEL_BYTES}:
        raise ValueError("Calibration must contain all five levels")
    last_end = -1
    for level in (5, 4, 3, 2, 1):
        item = levels[str(level)]
        if not isinstance(item, dict) or type(item.get("byte")) is not int or item["byte"] != LEVEL_BYTES[level]:
            raise ValueError("Calibration write byte does not match fixed protocol")
        write_at = item.get("write_completed_elapsed")
        if not number(write_at) or write_at <= last_end:
            raise ValueError("Calibration writes are not ordered L5 to L1")
        samples = item.get("samples")
        if not isinstance(samples, list) or len(samples) != STABLE_SAMPLES:
            raise ValueError("Each level requires three consecutive stable samples")
        previous, previous_time = None, None
        for sample in samples:
            if not isinstance(sample, dict):
                raise ValueError("Invalid calibration sample")
            elapsed, rpm = sample.get("sampled_elapsed"), sample.get("rpm")
            if (not number(elapsed) or elapsed < write_at + SETTLE_SECONDS or
                    elapsed > write_at + STEP_TIMEOUT or
                    previous is not None and elapsed - previous < SAMPLE_INTERVAL):
                raise ValueError("Calibration requires fresh, separated, post-settle samples")
            sample_time = _timestamp(sample.get("timestamp"))
            if previous_time is not None and (sample_time - previous_time).total_seconds() < SAMPLE_INTERVAL:
                raise ValueError("Calibration timestamps must be separated")
            if not isinstance(rpm, dict) or set(rpm) != set(FANS) or any(
                    not number(value) or not 1 <= value <= 100000 for value in rpm.values()):
                raise ValueError("Calibration needs finite positive RPM for every installed fan")
            if not matches_level(sample.get("raw_bytes"), level):
                raise ValueError("Calibration readback does not match manual mode and all six duty bytes")
            previous, previous_time = elapsed, sample_time
        rpm_ranges(samples)
        last_end = previous
    expected = profile_ranges(levels)
    if any(item.get("rpm_ranges") != expected[level] for level, item in levels.items()):
        raise ValueError("Calibration tolerances must match measurements and adjacent-level midpoints")
    return profile


def _unique(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"Duplicate calibration key: {key}")
        result[key] = value
    return result


def read_json(path):
    with Path(path).open("rb") as stream:
        data = stream.read(1024 * 1024 + 1)
    if len(data) > 1024 * 1024:
        raise ValueError("Evidence file is too large")
    return json.loads(data, object_pairs_hook=_unique,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError(f"Invalid number: {value}")))


def load_profile(path, identity, config):
    return validate_profile(read_json(path), identity, config)


def publish_profile(path, profile):
    """Hard-link a fully flushed temporary file: atomic and never overwrites any target."""
    path = Path(path)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(mode="w", dir=path.parent, prefix=".hrx-calibration-", delete=False) as stream:
            temporary = stream.name
            json.dump(profile, stream, ensure_ascii=False, allow_nan=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.link(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if temporary is not None:
            os.unlink(temporary)


def rpm_matches(report, profile, level):
    ranges = profile["levels"][str(level)]["rpm_ranges"]
    return all(name in report["fans"] and not report["fans"][name]["error"] and
               number(report["fans"][name]["rpm"]) and
               ranges[name][0] <= report["fans"][name]["rpm"] <= ranges[name][1] for name in FANS)
