from __future__ import annotations

import argparse
import json
import signal
import sys
import threading
import time
from pathlib import Path

from .collection import CollectionStats, collect, poll_wait_seconds, timestamp
from .config import ConfigError, load_config
from .ipmi import CollectionCancelled, ReadOnlyIPMI
from .logger import AsyncLogger, transition_key
from .log_record import format_log_record
from .thermal_engine import ThermalEngine


L5_ALERT = "警告：当前建议档位已达到 L5。"


def render(report):
    control = report.get("control")
    available = report["component_level"] is not None
    demand = (f"Component demand L{report['component_level']}" if available else
              "Component demand unavailable")
    level = report["recommended_level"]
    recommendation = ("Waiting for valid sensor data; no recommendation" if level is None else
                      f"recommendation L{level} (policy target {report['policy_target_percent']}%, uncalibrated)")
    lines = [f"{report['timestamp']}  DRY-RUN — no fan settings applied",
             f"{demand}; {recommendation} [{report['safety_state']}]",
             "IPMI argument: unresolved; expected RPM: unresolved; no encoding conversion performed",
             f"{'Sensor':<18} {'Celsius':>8} {'Level':>7} {'C/min':>8}  State"]
    if control:
        lines[0] = f"{report['timestamp']}  {report['mode'].upper()} — {control['status']}"
        lines[1] = lines[1].replace(", uncalibrated", ", calibrated profile")
        target = f"L{control['target_level']}" if control['target_level'] is not None else "waiting for valid data"
        lines[2] = (f"Control target {target}; byte {control['byte']}; "
                    f"fresh RPM confirmations {control['stable_samples']}/{control['required_samples']}; "
                    f"verified={control['verified']}")
    failure = report.get("read_failure")
    if failure:
        lines.insert(3, f"Read failures: {failure['consecutive_failures']}/{failure['count_threshold']}; "
                     f"last valid sample age {failure['no_valid_sample_seconds']:.1f}/"
                     f"{failure['time_threshold_seconds']} s; "
                     f"fallback floor L{failure['fallback_level']}; "
                     f"trigger: {', '.join(failure['trigger_reasons']) or 'none'}")
    recovery = report.get("recovery")
    if recovery and recovery["holding"]:
        lines.insert(-1, f"Recovery: {recovery['healthy_samples']}/{recovery['required_samples']} healthy samples")
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
    if control and raw:
        from .protocol import decode_status
        lines[-1] = f"BMC raw bytes: {' '.join(raw)}; mode={decode_status(raw)['mode']}"
    for message in report["faults"]:
        lines.append(f"Diagnostic: {message}")
    return "\n".join(lines)


def main(argv=None):
    parser = argparse.ArgumentParser(description="HR650X thermal monitor and explicitly enabled calibrated fan control")
    parser.add_argument("--config", type=Path, help="YAML overrides; bundled HR650X defaults are always loaded")
    parser.add_argument("--once", action="store_true", help="read one snapshot and exit")
    parser.add_argument("--input", type=Path, help="offline ipmitool sensor output; requires --once")
    parser.add_argument("--json", action="store_true", help="JSON snapshot per line")
    parser.add_argument("--no-log", action="store_true", help="disable local file logging")
    modes = parser.add_mutually_exclusive_group()
    modes.add_argument("--calibrate", type=Path, metavar="NEW_JSON", help="explicitly write L5 to L1 and save live machine calibration")
    modes.add_argument("--auto", action="store_true", help="enable calibrated continuous automatic control")
    modes.add_argument("--manual-level", type=int, choices=range(1, 6), help="continuous control with this level as a floor")
    modes.add_argument("--service-recover", action="store_true", help=argparse.SUPPRESS)
    parser.add_argument("--profile", type=Path, help="same-machine calibration JSON, required for automatic/manual control")
    parser.add_argument("--resume-control", action="store_true", help="explicitly resume control after an external override")
    args = parser.parse_args(argv)
    controlling = args.calibrate is not None or args.auto or args.manual_level is not None or args.service_recover
    if args.resume_control and (not controlling or args.service_recover):
        parser.error("--resume-control requires --auto, --manual-level, or --calibrate")
    if controlling and (args.input or args.once):
        parser.error("Control/calibration cannot be combined with --input or --once")
    if (args.auto or args.manual_level is not None or args.service_recover) and not args.profile:
        parser.error("Automatic/manual/recovery control requires --profile")
    if args.profile and (not controlling or args.calibrate):
        parser.error("--profile is only valid with --auto, --manual-level, or service recovery")
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
        if controlling:
            from .control_runtime import run_control
            return run_control(args, config)
        source = args.input.read_text(encoding="utf-8") if args.input else None
        logger = AsyncLogger(config["logging"])
        engine = ThermalEngine(config)
        stats = CollectionStats()
        ipmi = ReadOnlyIPMI(config["ipmi"]["command_timeout_seconds"],
                            config["ipmi"]["status_timeout_seconds"], stop)
        for sig in (signal.SIGINT, signal.SIGTERM):
            handlers[sig] = signal.signal(sig, lambda *_: stop.set())
        previous_key, l5_notified = None, False
        last_log = float("-inf")

        def alert(report):
            nonlocal l5_notified
            if report["recommended_level"] == 5 and not l5_notified:
                print(L5_ALERT, file=sys.stderr, flush=True)
                logger.emit(format_log_record(report, "safety_alert", time.monotonic() - start))
                l5_notified = True

        while not stop.is_set():
            start = time.monotonic()
            try:
                report = collect(ipmi, engine, config, source, alert, stop, stats)
            except CollectionCancelled:
                break
            if report["recommended_level"] == 5:
                alert(report)
            else:
                l5_notified = False
            report["logging_health"] = {"dropped_records": logger.dropped, "error": logger.error}
            key = transition_key(report)
            now = time.monotonic()
            if key != previous_key or now - last_log >= config["logging"]["interval_seconds"]:
                event = "transition" if key != previous_key else "summary"
                logger.emit(format_log_record(report, event, now - start))
                last_log = now
            previous_key = key
            print(json.dumps(report, ensure_ascii=False, allow_nan=False) if args.json else render(report), flush=True)
            if logger.error or logger.dropped:
                print(f"Logging degraded: {logger.error or 'queue overflow'}; dropped={logger.dropped}", file=sys.stderr)
            exit_code = 2 if report["recommended_level"] == 5 or report["faults"] or report["emergencies"] else 0
            if args.once:
                break
            stop.wait(poll_wait_seconds(report, config["controller"]["poll_interval_seconds"],
                                       time.monotonic() - start))
        if stop.is_set():
            exit_code = 0
        logger.emit(format_log_record({"timestamp": timestamp(), "mode": "dry-run", "applied": False,
                     "run_id": stats.run_id, "poll_id": stats.started,
                     "source": "offline-snapshot" if source is not None else "local-openipmi",
                     "collection_counts": stats.snapshot(),
                     "reason": "requested" if stop.is_set() else "once"}, "shutdown"))
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
