"""Parse the full `ipmitool sensor` table without conflating alarms with NA."""
from __future__ import annotations

import math
from dataclasses import dataclass, replace
from typing import Optional


def number(text):
    try:
        value = float(text)
        return value if math.isfinite(value) else None
    except ValueError:
        return None


@dataclass(frozen=True)
class Reading:
    name: str
    value: Optional[float]
    unit: str
    status: str
    upper_warning: Optional[float] = None
    upper_critical: Optional[float] = None
    upper_nonrecoverable: Optional[float] = None
    error: Optional[str] = None

    @property
    def thermal(self):
        return self.unit.casefold() in ("degrees c", "degrees celsius", "celsius", "°c")


def parse_sensors(text):
    readings, errors = {}, []
    for line_no, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        fields = [part.strip() for part in line.split("|")]
        if len(fields) != 10 or not fields[0]:
            errors.append(f"Malformed sensor row {line_no}: expected 10 pipe-separated columns")
            continue
        name, raw, unit, status = fields[:4]
        key = name.casefold()
        value = number(raw)
        error = None
        if value is None:
            error = f"unavailable or nonnumeric value: {raw}"
        elif status.casefold() not in ("ok", "nc", "cr", "nr", "lnc", "lcr", "lnr", "unc", "ucr", "unr"):
            error = f"unavailable or unsupported status: {status}"
        reading = Reading(name, value, unit, status.casefold(), *(number(v) for v in fields[7:10]), error)
        if key in readings:
            errors.append(f"Duplicate sensor: {name}")
            reading = replace(reading, value=None, error="duplicate sensor name")
        readings[key] = reading
    if not readings:
        errors.append("No sensor rows received")
    return readings, errors


def validate_temperature(reading, policy):
    if reading is None:
        return "missing sensor"
    if reading.error:
        return reading.error
    if not reading.thermal:
        return f"expected Celsius, received {reading.unit}"
    if policy["reject_zero"] and reading.value == 0:
        return "zero reading is unverified or hardware is absent"
    if not policy["minimum"] <= reading.value <= policy["maximum"]:
        return "temperature outside configured plausible range"
    return None
