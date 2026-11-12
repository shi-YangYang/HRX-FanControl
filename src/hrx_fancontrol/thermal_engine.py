"""Pure policy: one fresh snapshot in, independent component demands out."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass

from .fan_reader import fan_health
from .hysteresis import Hysteresis
from .safety import Safety
from .sensors import validate_temperature


def _known_demand(sensors):
    """Preserve valid component demand even when another critical sensor is absent."""
    return max((sensor["held_level"] for sensor in sensors.values()
                if sensor["celsius"] is not None and sensor["held_level"] is not None), default=None)


@dataclass
class Assessment:
    report: dict
    states: dict
    previous: dict
    hardware_faults: list
    fan_faults: list
    valid_sample: bool
    read_failed: bool
    gap: bool
    now: float
    sampled_at: float
    stale: bool


class ThermalEngine:
    def __init__(self, config):
        self.config = config
        self.states = {name: Hysteresis() for name in config["sensors"]}
        self.safety = Safety(config["safety"])
        self.previous = {}
        self.last_update = None

    def update(self, readings, now, sampled_at, errors=(), *, read_failed=False):
        assessment = self.assess(readings, now, sampled_at, errors, read_failed=read_failed)
        return self.finalize(assessment, now, read_failed=read_failed)

    def assess(self, readings, now, sampled_at, errors=(), *, read_failed=False):
        """Evaluate a sample for immediate alarms; commit state only after the entire poll."""
        c = self.config
        if read_failed:
            readings = {}
        timeout = c["safety"]["sensor_timeout_seconds"]
        stale = now - sampled_at > timeout or sampled_at > now
        gap = self.last_update is not None and (now - self.last_update > timeout or now < self.last_update)
        states, previous_values = deepcopy(self.states), dict(self.previous)
        faults = list(errors)
        available = not read_failed and not stale
        hardware_faults = list(errors) if available else []
        if stale or gap:
            faults.append("Stale sample or gap in sensor updates")
            for state in states.values():
                state.invalidate()
            previous_values.clear()
        sensors, sensor_errors, emergencies, bmc_alarms = {}, {}, [], []
        for name, policy in c["sensors"].items():
            reading = readings.get(name.strip().casefold())
            error = "stale sample" if stale else validate_temperature(reading, policy)
            state = states[name]
            level, trend = None, None
            if error:
                state.invalidate()
                previous_values.pop(name, None)
                sensor_errors[name] = error
                if policy["critical"]:
                    faults.append(f"{name}: {error}")
                    if available:
                        hardware_faults.append(f"{name}: {error}")
            else:
                if policy["curve"]:
                    level = state.update(reading.value, c["curves"][policy["curve"]], now, c["hysteresis"])
                previous = previous_values.get(name)
                if previous and now > previous[0]:
                    trend = round((reading.value - previous[1]) * 60 / (now - previous[0]), 3)
                previous_values[name] = (now, reading.value)
                if policy["emergency_celsius"] is not None and reading.value >= policy["emergency_celsius"]:
                    emergencies.append(f"{name}: configured emergency temperature")
            sensors[name] = {
                "celsius": reading.value if reading and not error else None,
                "observed_value": reading.value if reading else None,
                "status": reading.status if reading else "missing",
                "requested_level": level, "held_level": state.level if policy["curve"] else None,
                "trend_celsius_per_minute": trend, "error": error,
                "policy": policy["curve"] or "monitor-only",
            }
        policies = {name.strip().casefold(): policy for name, policy in c["sensors"].items()}
        for key, reading in readings.items():
            if reading.thermal:
                # Honor BMC alarms even when a configured plausibility filter rejects the value.
                limits = [v for v in (reading.upper_warning, reading.upper_critical, reading.upper_nonrecoverable) if v is not None]
                if reading.status in ("nc", "cr", "nr", "lnc", "lcr", "lnr", "unc", "ucr", "unr") or (
                    reading.value is not None and any(reading.value >= limit for limit in limits)
                ):
                    message = f"{reading.name}: BMC threshold alarm"
                    bmc_alarms.append(message)
                    if not policies.get(key, {}).get("curve"):
                        emergencies.append(message)
                if key not in policies:
                    sensors[reading.name] = {"celsius": reading.value if not reading.error else None,
                        "observed_value": reading.value, "status": reading.status, "requested_level": None,
                        "held_level": None, "trend_celsius_per_minute": None,
                        "error": reading.error or "unconfigured thermal sensor", "policy": "unconfigured"}
                    faults.append(f"{reading.name}: unconfigured thermal sensor")
                    hardware_faults.append(f"{reading.name}: unconfigured thermal sensor")
        fans, fan_errors = fan_health(readings if available else {}, c["fan"]["installed"], c["fan"]["minimum_rpm"])
        faults.extend(fan_errors)
        fan_faults = fan_errors if available else []
        valid_sample = available and not any(c["sensors"][name]["critical"] for name in sensor_errors)
        demand = _known_demand(sensors)
        level, state = self.safety.preview(demand, hardware_faults, emergencies, fan_faults=fan_faults)
        report = {"mode": "dry-run", "applied": False, "sensors": sensors, "fans": fans,
                "component_level": demand if valid_sample else None, "recommended_level": level,
                "policy_target_percent": c["fan"]["levels"][level - 1] if level is not None else None, "safety_state": state,
                "calibration": {"status": "unverified", "ipmi_argument": None, "expected_rpm": None},
                "sensor_errors": sensor_errors, "faults": sorted(set(faults)),
                "emergencies": sorted(set(emergencies)), "bmc_alarms": sorted(set(bmc_alarms))}

        return Assessment(report, states, previous_values, hardware_faults, fan_faults, valid_sample,
                          read_failed or stale, gap, now, sampled_at, stale)

    def finalize(self, assessment, now, *, read_failed=False):
        """Apply communication outcome once; failed polls cannot advance cooldown evidence."""
        report = assessment.report
        timeout = self.config["safety"]["sensor_timeout_seconds"]
        stale = assessment.stale or now - assessment.sampled_at > timeout or assessment.sampled_at > now
        # A fresh preview may already have actuated L5 before the status read stalled.
        # Keep that protection, without treating the aged sample as new evidence.
        early_l5 = not assessment.stale and report["recommended_level"] == 5
        if stale:
            assessment.valid_sample = False
            assessment.read_failed = True
            assessment.previous.clear()
            report["faults"].append("Stale sample or gap in sensor updates")
            for name, sensor in report["sensors"].items():
                sensor.update(celsius=None, requested_level=None, trend_celsius_per_minute=None,
                              error="stale sample")
                report["sensor_errors"][name] = "stale sample"
            for name, fan in report["fans"].items():
                # Missing placeholders from an already-expired table are not fan faults.
                if assessment.stale and fan["error"] == "missing installed fan":
                    report["faults"].remove(f"{name}: missing installed fan")
                fan.update(observed_rpm=fan["rpm"], rpm=None, error="stale sample")
            report["faults"] = sorted(set(report["faults"]))
        assessment.stale = stale
        read_failed = read_failed or assessment.read_failed
        if read_failed:
            for name, state in assessment.states.items():
                state.level = max(state.level, self.states[name].level)
                state.invalidate()
                sensor = report["sensors"][name]
                if sensor["held_level"] is not None:
                    sensor["held_level"] = state.level
                    if sensor["requested_level"] is not None:
                        sensor["requested_level"] = state.level
        self.states, self.previous = assessment.states, assessment.previous
        self.last_update = assessment.now
        demand = _known_demand(report["sensors"])
        if stale and early_l5:
            demand = 5
        level, state = self.safety.update(
            demand, assessment.hardware_faults, report["emergencies"], now,
            read_failed=read_failed, valid_sample=assessment.valid_sample,
            sampled_at=assessment.sampled_at, gap=assessment.gap,
            alerting=bool(report["bmc_alarms"]),
            fan_faults=assessment.fan_faults if not stale else [],
        )
        report.update(component_level=demand if assessment.valid_sample else None,
                      recommended_level=level, safety_state=state,
                      policy_target_percent=self.config["fan"]["levels"][level - 1] if level is not None else None,
                      read_failure=self.safety.read_failure_status(now),
                      recovery=self.safety.recovery_status())
        return report
