"""Strict YAML profile loading, including the read-only execution boundary."""
from __future__ import annotations

import math
from copy import deepcopy
from importlib.resources import read_text
from pathlib import Path

import yaml


class ConfigError(ValueError):
    pass


class UniqueLoader(yaml.SafeLoader):
    pass


def _mapping(loader, node):
    result = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node)
        if not isinstance(key, str) or key in result:
            raise ConfigError(f"Duplicate or non-string YAML key: {key!r}")
        result[key] = loader.construct_object(value_node)
    return result


UniqueLoader.add_constructor(yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _mapping)


def _number(value, name, minimum=0):
    if type(value) not in (int, float) or not math.isfinite(value) or value < minimum:
        raise ConfigError(f"{name} must be a finite number >= {minimum}")


def _merge(base, override, path=""):
    if not isinstance(override, dict):
        raise ConfigError(f"{path or 'profile'} must be a mapping")
    for key, value in override.items():
        if key not in base:
            raise ConfigError(f"Unknown configuration key: {path}{key}")
        if isinstance(base[key], dict):
            _merge(base[key], value, f"{path}{key}.")
        else:
            base[key] = value


def load_config(path=None):
    config = yaml.load(read_text("hrx_fancontrol", "default.yaml"), Loader=UniqueLoader)
    if path:
        try:
            override = yaml.load(Path(path).read_text(encoding="utf-8"), Loader=UniqueLoader)
            # A sensor profile can add discovered sensors and custom curves.
            if isinstance(override, dict):
                for section in ("sensors", "curves"):
                    additions = override.get(section, {})
                    if isinstance(additions, dict):
                        for name, value in additions.items():
                            if name not in config[section]:
                                config[section][name] = deepcopy(value)
            _merge(config, override)
        except (OSError, yaml.YAMLError) as exc:
            raise ConfigError(str(exc)) from exc
    validate(config)
    return config


def validate(c):
    required_values = {
        ("controller", "enabled"): False, ("controller", "mode"): "dry-run",
        ("ipmi", "interface"): "open", ("logging", "backup_enabled"): False,
        ("safety", "fail_safe_enabled"): True, ("safety", "verify_fan_rpm"): True,
        ("safety", "sensor_failure_action"): "safe_mode",
        ("safety", "ipmi_failure_action"): "stop_lowering",
        ("hysteresis", "upshift_delay_seconds"): 0,
    }
    for (section, key), expected in required_values.items():
        value = c[section][key]
        if type(value) is not type(expected) or value != expected:
            raise ConfigError(f"This prototype requires {section}.{key} = {expected!r}")
    for section, key in (("hysteresis", "enabled"), ("logging", "enabled"), ("ipmi", "read_control_status")):
        if type(c[section][key]) is not bool:
            raise ConfigError(f"{section}.{key} must be a boolean")
    for section, key in (
        ("controller", "poll_interval_seconds"), ("ipmi", "command_timeout_seconds"),
        ("ipmi", "status_timeout_seconds"), ("safety", "read_failure_timeout_seconds"),
        ("logging", "interval_seconds"), ("safety", "sensor_timeout_seconds"),
    ):
        _number(c[section][key], f"{section}.{key}", 0.01)
    for section, key in (
        ("hysteresis", "downshift_delay_seconds"), ("hysteresis", "downshift_margin_celsius"),
        ("fan", "minimum_rpm"),
    ):
        _number(c[section][key], f"{section}.{key}")
    for section, key in (("logging", "retention_days"), ("logging", "queue_capacity"), ("safety", "safe_level"),
                         ("safety", "recovery_success_count"), ("safety", "read_failure_count"),
                         ("safety", "read_failure_level")):
        value = c[section][key]
        if type(value) is not int or value < 1:
            raise ConfigError(f"{section}.{key} must be a positive integer")
    for key in ("safe_level", "read_failure_level"):
        if c["safety"][key] > 5:
            raise ConfigError(f"{key} must be in 1..5")
    if c["safety"]["sensor_timeout_seconds"] <= c["controller"]["poll_interval_seconds"]:
        raise ConfigError("sensor_timeout_seconds must exceed poll_interval_seconds")
    for key in ("command_timeout_seconds", "status_timeout_seconds"):
        if c["ipmi"][key] >= c["safety"]["sensor_timeout_seconds"]:
            raise ConfigError(f"ipmi.{key} must be less than safety.sensor_timeout_seconds")
    if c["ipmi"]["read_control_status"] and (c["ipmi"]["command_timeout_seconds"] +
                                           c["ipmi"]["status_timeout_seconds"] >=
                                           c["safety"]["sensor_timeout_seconds"]):
        raise ConfigError("Combined IPMI timeouts must be less than safety.sensor_timeout_seconds")
    levels = c["fan"]["levels"]
    if not isinstance(levels, list) or len(levels) != 5 or any(type(v) is not int for v in levels):
        raise ConfigError("fan.levels must contain five integer percentages")
    if levels != sorted(set(levels)) or not 15 <= levels[0] < levels[-1] <= 60:
        raise ConfigError("fan.levels must strictly increase within 15..60")
    for key, expected in (("minimum_percent", levels[0]), ("maximum_percent", levels[-1])):
        if type(c["fan"][key]) is not int or c["fan"][key] != expected:
            raise ConfigError(f"fan.{key} must match the end of fan.levels")
    installed = c["fan"]["installed"]
    if not isinstance(installed, list) or not installed or any(not isinstance(v, str) or not v.strip() for v in installed):
        raise ConfigError("fan.installed must contain sensor names")
    if len({v.strip().casefold() for v in installed}) != len(installed):
        raise ConfigError("fan.installed contains duplicate names")
    if not isinstance(c["logging"]["directory"], str) or not c["logging"]["directory"].strip():
        raise ConfigError("logging.directory must be a nonempty path")
    for name, curve in c["curves"].items():
        if not isinstance(curve, list) or len(curve) != 4:
            raise ConfigError(f"Curve {name} must have four entry thresholds")
        for value in curve:
            _number(value, f"curve {name}", -100)
        if curve != sorted(set(curve)):
            raise ConfigError(f"Curve {name} must strictly increase")
    names = set()
    fields = {"curve", "critical", "emergency_celsius", "minimum", "maximum", "reject_zero"}
    for name, sensor in c["sensors"].items():
        if not name.strip() or name.strip().casefold() in names:
            raise ConfigError(f"Duplicate or empty sensor name: {name}")
        names.add(name.strip().casefold())
        if not isinstance(sensor, dict) or set(sensor) != fields:
            raise ConfigError(f"Sensor {name} must specify {sorted(fields)}")
        if sensor["curve"] is not None and (not isinstance(sensor["curve"], str) or sensor["curve"] not in c["curves"]):
            raise ConfigError(f"Unknown curve for {name}")
        for field in ("critical", "reject_zero"):
            if type(sensor[field]) is not bool:
                raise ConfigError(f"{name}.{field} must be boolean")
        for field in ("minimum", "maximum"):
            _number(sensor[field], f"{name}.{field}", -273.15)
        if sensor["minimum"] >= sensor["maximum"]:
            raise ConfigError(f"Invalid sensor range: {name}")
        if sensor["emergency_celsius"] is not None:
            _number(sensor["emergency_celsius"], f"{name}.emergency_celsius", sensor["minimum"])
            if sensor["emergency_celsius"] > sensor["maximum"]:
                raise ConfigError(f"Emergency threshold exceeds valid range: {name}")
