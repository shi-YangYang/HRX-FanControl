"""Pure policy: one fresh snapshot in, independent component demands out."""
from __future__ import annotations

from .fan_reader import fan_health
from .hysteresis import Hysteresis
from .safety import Safety
from .sensors import validate_temperature


class ThermalEngine:
    def __init__(self, config):
        self.config = config
        self.states = {name: Hysteresis() for name in config["sensors"]}
        self.safety = Safety(config["safety"])
        self.previous = {}
        self.last_update = None

    def update(self, readings, now, sampled_at, errors=()):
        c = self.config
        timeout = c["safety"]["sensor_timeout_seconds"]
        stale = now - sampled_at > timeout or sampled_at > now
        gap = self.last_update is not None and (now - self.last_update > timeout or now < self.last_update)
        faults = list(errors)
        if stale or gap:
            faults.append("Stale sample or gap in sensor updates")
            for state in self.states.values():
                state.invalidate()
            self.previous.clear()
        self.last_update = now
        sensors, sensor_errors, emergencies = {}, {}, []
        for name, policy in c["sensors"].items():
            reading = readings.get(name.strip().casefold())
            error = "stale sample" if stale else validate_temperature(reading, policy)
            state = self.states[name]
            level, trend = None, None
            if error:
                state.invalidate()
                self.previous.pop(name, None)
                sensor_errors[name] = error
                if policy["critical"]:
                    faults.append(f"{name}: {error}")
            else:
                if policy["curve"]:
                    level = state.update(reading.value, c["curves"][policy["curve"]], now, c["hysteresis"])
                previous = self.previous.get(name)
                if previous and now > previous[0]:
                    trend = round((reading.value - previous[1]) * 60 / (now - previous[0]), 3)
                self.previous[name] = (now, reading.value)
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
        known = {name.strip().casefold() for name in c["sensors"]}
        for key, reading in readings.items():
            if reading.thermal:
                # Honor BMC alarms even when a configured plausibility filter rejects the value.
                limits = [v for v in (reading.upper_warning, reading.upper_critical, reading.upper_nonrecoverable) if v is not None]
                if reading.status in ("nc", "cr", "nr", "lnc", "lcr", "lnr", "unc", "ucr", "unr") or (
                    reading.value is not None and any(reading.value >= limit for limit in limits)
                ):
                    emergencies.append(f"{reading.name}: BMC threshold alarm")
                if key not in known:
                    sensors[reading.name] = {"celsius": reading.value if not reading.error else None,
                        "observed_value": reading.value, "status": reading.status, "requested_level": None,
                        "held_level": None, "trend_celsius_per_minute": None,
                        "error": reading.error or "unconfigured thermal sensor", "policy": "unconfigured"}
                    faults.append(f"{reading.name}: unconfigured thermal sensor")
        fans, fan_errors = fan_health(readings, c["fan"]["installed"], c["fan"]["minimum_rpm"])
        faults.extend(fan_errors)
        demand = max((entry["held_level"] or 1 for entry in sensors.values()), default=1)
        level, state = self.safety.update(demand, faults, emergencies, now)
        return {"mode": "dry-run", "applied": False, "sensors": sensors, "fans": fans,
                "component_level": demand, "recommended_level": level,
                "policy_target_percent": c["fan"]["levels"][level - 1], "safety_state": state,
                "calibration": {"status": "unverified", "ipmi_argument": None, "expected_rpm": None},
                "sensor_errors": sensor_errors, "faults": sorted(set(faults)),
                "emergencies": sorted(set(emergencies))}
