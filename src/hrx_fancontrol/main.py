from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from .config import ConfigError, load_config
from .ipmi import ReadOnlyIPMI, parse_control_status
from .logger import AsyncLogger, transition_key
from .sensors import parse_sensors
from .thermal_engine import ThermalEngine


def timestamp():
    return datetime.now(timezone.utc).isoformat()


def render(report):
    lines = [f"{report['timestamp']}  DRY-RUN — no fan settings applied",
             f"Component demand L{report['component_level']}; recommendation "
             f"L{report['recommended_level']} (policy target {report['policy_target_percent']}%, uncalibrated) "
             f"[{report['safety_state']}]",
             "IPMI argument: unresolved; expected RPM: unresolved; no encoding conversion performed",
             f"{'Sensor':<18} {'Celsius':>8} {'Level':>7} {'C/min':>8}  State"]
    for name, sensor in report["sensors"].items():
        value = "--" if sensor["celsius"] is None else f"{sensor['celsius']:.1f}"
        level = "--" if sensor["requested_level"] is None else f"L{sensor['requested_level']}"
        trend = "--" if sensor["trend_celsius_per_minute"] is None else f"{sensor['trend_celsius_per_minute']:+.2f}"
        lines.append(f"{name:<18} {value:>8} {level:>7} {trend:>8}  "
                     f"{sensor['error'] or sensor['policy']}")
    for name, fan in report["fans"].items():
        lines.append(f"{name}: {fan['rpm'] if fan['rpm'] is not None else '--'} RPM  {fan['error'] or 'ok'}")
    raw = report["bmc"]["raw_bytes"]
    lines.append(f"BMC raw bytes: {' '.join(raw) if raw else 'unavailable / not read'}; mode and percentage unverified")
    for message in report["faults"] + report["emergencies"]:
        lines.append(f"ALERT: {message}")
    return "\n".join(lines)


def collect(ipmi, engine, config, source=None, on_alert=None):
    sampled_at = time.monotonic()
    previous_level = engine.safety.previous_level
    commands, errors = [], []
    if source is not None:
        text = source
    else:
        result = ipmi.sensors()
        commands.append(result.summary())
        text = result.stdout
        if result.error or result.stderr:
            errors.append(result.error or f"IPMI sensor diagnostic: {result.stderr}")
    readings, parse_errors = parse_sensors(text)
    errors.extend(parse_errors)
    report = engine.update(readings, time.monotonic(), sampled_at, errors)
    report.update(timestamp=timestamp(), source="offline-snapshot" if source is not None else "local-openipmi",
                  commands=commands, bmc={"raw_bytes": None, "mode": "unverified", "percent": None})
    # Publish detected thermal/fan emergencies before waiting on the optional status query.
    if on_alert and (report["faults"] or report["emergencies"]):
        on_alert(report)
    if source is None and config["ipmi"]["read_control_status"]:
        result = ipmi.control_status()
        commands.append(result.summary())
        try:
            if result.error or result.stderr:
                raise ValueError(result.error or result.stderr)
            report["bmc"]["raw_bytes"] = parse_control_status(result.stdout)
        except ValueError as exc:
            report["faults"].append(f"BMC status: {exc}")
            # A failed readback must not undo a previously held higher recommendation.
            engine.safety.previous_level = max(previous_level, engine.safety.previous_level)
            level, state = engine.safety.update(report["component_level"], report["faults"],
                                                 report["emergencies"], time.monotonic())
            report.update(recommended_level=level, safety_state=state,
                          policy_target_percent=config["fan"]["levels"][level - 1])
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description="HR650X read-only thermal monitor and fan-level simulator")
    parser.add_argument("--config", type=Path, help="YAML overrides; bundled HR650X defaults are always loaded")
    parser.add_argument("--once", action="store_true", help="read one snapshot and exit")
    parser.add_argument("--input", type=Path, help="offline ipmitool sensor output; requires --once")
    parser.add_argument("--json", action="store_true", help="JSON snapshot per line")
    parser.add_argument("--no-log", action="store_true", help="disable local file logging")
    args = parser.parse_args(argv)
    if args.input and not args.once:
        parser.error("--input requires --once; replaying an unchanged file as fresh sensor data is unsafe")
    logger = None
    handlers = {}
    stop = threading.Event()
    exit_code = 0
    try:
        config = load_config(args.config)
        if args.no_log:
            config["logging"]["enabled"] = False
        source = args.input.read_text(encoding="utf-8") if args.input else None
        logger = AsyncLogger(config["logging"])
        engine = ThermalEngine(config)
        ipmi = ReadOnlyIPMI(config["ipmi"]["command_timeout_seconds"])
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        previous_key, previous_alert = None, None
        last_log = float("-inf")

        def alert(report):
            nonlocal previous_alert
            key = (tuple(report["faults"]), tuple(report["emergencies"]))
            if key != previous_alert:
                print("DRY-RUN ALERT: " + "; ".join(report["faults"] + report["emergencies"]),
                      file=sys.stderr, flush=True)
                # Serialize now so later readback changes cannot mutate the queued record.
                logger.emit(json.loads(json.dumps({"event": "safety_alert", **report})))
                previous_alert = key

        while not stop.is_set():
            start = time.monotonic()
            report = collect(ipmi, engine, config, source, alert)
            if report["faults"] or report["emergencies"]:
                alert(report)
            else:
                previous_alert = None
            report["logging_health"] = {"dropped_records": logger.dropped, "error": logger.error}
            key = transition_key(report)
            now = time.monotonic()
            if key != previous_key or now - last_log >= config["logging"]["interval_seconds"]:
                event = "transition" if key != previous_key else "summary"
                logger.emit({"event": event, **report})
                last_log = now
            previous_key = key
            print(json.dumps(report, ensure_ascii=False, allow_nan=False) if args.json else render(report), flush=True)
            if logger.error or logger.dropped:
                print(f"Logging degraded: {logger.error or 'queue overflow'}; dropped={logger.dropped}", file=sys.stderr)
            exit_code = 2 if report["faults"] or report["emergencies"] else 0
            if args.once:
                break
            stop.wait(max(0, config["controller"]["poll_interval_seconds"] - (time.monotonic() - start)))
        logger.emit({"event": "shutdown", "timestamp": timestamp(), "mode": "dry-run", "applied": False})
    except (ConfigError, OSError, ValueError) as exc:
        print(f"hrx-fancontrol: {exc}", file=sys.stderr)
        exit_code = 1
    finally:
        for sig, handler in handlers.items():
            signal.signal(sig, handler)
        if logger:
            logger.close()
            if logger.error or logger.dropped:
                print(f"Logging incomplete: {logger.error or 'queue overflow'}; dropped={logger.dropped}", file=sys.stderr)
                exit_code = 1
    return exit_code
