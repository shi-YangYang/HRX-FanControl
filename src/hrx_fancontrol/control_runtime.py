"""Explicit control CLI lifecycle; default monitoring never enters this module's runner."""
import json
import os
import sys
import threading
import time

from .calibration import SETTLE_SECONDS, load_profile
from .calibration_run import CalibrationAborted, run_calibration
from .collection import CollectionStats, collect, poll_wait_seconds, timestamp
from .executor import ExecutionFailure, Executor, ExternalOverride, OwnershipUnverified
from .identity import machine_identity, validate_control_config
from .ipmi import CollectionCancelled, check_cancelled
from .lifecycle import (OVERRIDE_EXIT_STATUS, ControlSuspended, Ownership,
                        StopSignals, WriterLock, control_state_directory, notify)
from .logger import AsyncLogger, transition_key
from .log_record import format_log_record
from .protocol import ControlIPMI, FANS, matches_level
from .thermal_engine import ThermalEngine

FALLBACK_LEVEL = 3
FALLBACK_TIMEOUT = 90.0


def fallback(executor, collect_next, wait, emit, clock=time.monotonic):
    """At most one L3 command; never retries a failed/ambiguous fallback write."""
    executor.request(FALLBACK_LEVEL, fallback=True)
    deadline = clock() + FALLBACK_TIMEOUT
    while clock() < deadline:
        wait(5)
        report = collect_next()
        executor.check_readback(report)
        if executor.profile is not None:
            executor.observe(report)
            if executor.status == "verified":
                emit("fallback_verified", executor.snapshot())
                return True
        elif (report["collection"]["status"] == "ok" and report["collection"]["sensor_data_valid"] and
              report["sampled_at_monotonic"] >= executor.completed_at + SETTLE_SECONDS and
              matches_level(report["bmc"]["raw_bytes"], FALLBACK_LEVEL) and
              all(name in report["fans"] and not report["fans"][name]["error"] for name in FANS)):
            executor.status = "readback-only-unverified"
            emit("fallback_unverified", executor.snapshot())
            return False
    executor.status = "verification-timeout"
    emit("fallback_unverified", executor.snapshot())
    return False


def run_control(args, config):
    stop = threading.Event()
    logger = None
    owner = executor = None
    stats = CollectionStats()
    last_report = None
    previous_key, last_log = None, float("-inf")
    l5_notified = False
    code = 0
    normal_stop = False
    yielded_exit = False

    def output(value):
        try:
            print(value, flush=True)
        except BrokenPipeError:
            pass  # A closed terminal pipe must not disable cooling or invent an actuator error.

    def notify_l5(level):
        nonlocal l5_notified
        if level == 5 and not l5_notified:
            print("警告：当前建议档位已达到 L5。", file=sys.stderr, flush=True)
            l5_notified = True
        elif level is not None and level < 5:
            l5_notified = False

    def emit(event, control):
        if event == "write_started":
            notify_l5(control.get("target_level"))
        record = {"schema_version": 2, "time": timestamp(), "event": event, "run_id": stats.run_id,
                  "poll_id": stats.started, "source": "local-openipmi", "mode": mode,
                  "applied": control.get("verified", False),
                  "control": {key: value for key, value in control.items()
                              if key != "command" or value and (value.get("error") or value.get("returncode") != 0)},
                  "message": {"write_started": "开始发送档位，物理状态尚未确认。",
                              "write_sent": "命令已返回，等待新采样验证。", "verified": "回读与新采样 RPM 已验证。",
                              "write_failed": "写入失败，是否生效可能未知。", "verification_failed": "档位验证失败。",
                              "shutdown": "控制已停止，保持最后设置，不追加写入。",
                              "fallback_verified": f"异常兜底 L{FALLBACK_LEVEL} 已验证。",
                              "fallback_unverified": f"异常兜底 L{FALLBACK_LEVEL} 尚未完整验证。",
                              "calibration_level": "本档校准采样完成。",
                              "calibration_complete": "五档校准完成，资料已保存。",
                              "calibration_aborted": "校准已中止，保留最后设置，未生成完整资料。",
                              "control_failed": "控制发生异常，停止常规调速。",
                              "external_override": "检测到外部修改，已停止自动调速。",
                              "ownership_unverified": "等待核实上次运行的 BMC 设置，未发送调速命令。",
                              "fallback_failed": f"异常兜底 L{FALLBACK_LEVEL} 失败，当前物理状态未确认。"}.get(event, event)}
        logger.emit(record)
        output(json.dumps({**record, "control": control}, ensure_ascii=False, allow_nan=False) if args.json else
               f"{record['time']} {record['message']} {json.dumps(control, ensure_ascii=False, allow_nan=False)}")

    def wait(seconds):
        if stop.wait(seconds):
            raise CollectionCancelled()

    def relinquish(exc):
        nonlocal yielded_exit
        if yielded_exit:
            return
        yielded_exit = True
        # Exit policy must survive a broken log sink or ownership filesystem.
        try:
            owner.relinquish()
        except Exception as error:
            try:
                print(f"Unable to persist control suspension: {error}", file=sys.stderr)
            except OSError:
                pass
        try:
            emit("external_override", {**executor.snapshot(), "reason": str(exc)})
        except Exception:
            try:
                print("检测到外部修改，已停止自动调速。", file=sys.stderr)
            except OSError:
                pass

    mode = "calibration" if args.calibrate else "manual" if args.manual_level else "automatic"
    if args.service_recover:
        mode = "service-recovery"
        if not os.environ.get("INVOCATION_ID") or os.environ.get("SERVICE_RESULT") not in (
                "exit-code", "signal", "core-dump", "watchdog", "timeout", "oom-kill"):
            return 0
    with StopSignals(stop):
        try:
            validate_control_config(config)
            if args.calibrate and args.calibrate.exists():
                raise ValueError("Calibration output already exists; choose a new path")
            ipmi = ControlIPMI(config["ipmi"]["command_timeout_seconds"], config["ipmi"]["status_timeout_seconds"], stop)
            identity = machine_identity(ipmi)
            profile = load_profile(args.profile, identity, config) if args.profile else None
            logger = AsyncLogger(config["logging"])
            with WriterLock() as lock:
                state_directory = control_state_directory()
                previous_confirmed = None
                if args.service_recover:
                    owner = Ownership.recover(state_directory, identity, profile)
                    if owner is None:
                        return 0
                    previous_confirmed = owner.data.get("confirmed_target")
                else:
                    owner = Ownership(state_directory, identity, profile, stats.run_id)
                    previous_confirmed = owner.check_start(getattr(args, "resume_control", False))
                engine = ThermalEngine(config)
                executor = Executor(ipmi, profile, owner, emit, stop)
                executor.restore_confirmed(previous_confirmed)
                notify("READY=1\nWATCHDOG=1\nSTATUS=Control preflight complete")

                def alert(report):
                    nonlocal l5_notified
                    if not l5_notified:
                        report.update(mode=mode, applied=False, control=executor.snapshot())
                        logger.emit(format_log_record(report, "safety_alert"))
                    notify_l5(5)
                    # Notify promptly, but wait for readback before any physical write.

                def collect_next(early=True):
                    nonlocal last_report, l5_notified
                    last_report = collect(ipmi, engine, config, on_alert=alert if early else None,
                                          stop=stop, stats=stats)
                    notify("WATCHDOG=1")
                    return last_report

                try:
                    if args.service_recover:
                        code = 0 if fallback(executor, lambda: collect_next(False), wait, emit) else 1
                    elif args.calibrate:
                        run_calibration(executor, identity, config, args.calibrate, collect_next, wait, emit)
                    else:
                        while not stop.is_set():
                            started = time.monotonic()
                            report = collect_next()
                            check_cancelled(stop)
                            executor.update(report, args.manual_level)
                            report.update(mode=mode, applied=executor.status == "verified", control=executor.snapshot(),
                                          calibration={"status": "validated-profile", "ipmi_argument": executor.snapshot()["byte"],
                                                       "expected_rpm": profile["levels"][str(executor.target)]["rpm_ranges"] if executor.target else None})
                            notify_l5(max(report["recommended_level"] or 0, executor.target or 0) or None)
                            report["logging_health"] = {"error": logger.error, "dropped_records": logger.dropped}
                            if logger.error or logger.dropped:
                                print(f"Logging degraded: {logger.error or 'queue overflow'}; dropped={logger.dropped}", file=sys.stderr)
                            key = (transition_key(report), executor.status, executor.target)
                            now = time.monotonic()
                            if key != previous_key or now - last_log >= config["logging"]["interval_seconds"]:
                                logger.emit(format_log_record(report, "transition" if key != previous_key else "summary", now-started))
                                last_log = now
                            previous_key = key
                            from .main import render
                            output(json.dumps(report, ensure_ascii=False, allow_nan=False) if args.json else render(report))
                            wait(poll_wait_seconds(report, config["controller"]["poll_interval_seconds"], time.monotonic()-started))
                except CollectionCancelled:
                    code, normal_stop = 0, True
                except ExternalOverride as exc:
                    relinquish(exc)
                except OwnershipUnverified as exc:
                    emit("ownership_unverified", {**executor.snapshot(), "reason": str(exc)})
                    code = 1
                except CalibrationAborted as exc:
                    emit("calibration_aborted", {**executor.snapshot(), "reason": str(exc)})
                    code = 1
                except Exception as exc:
                    emit("control_failed", {**executor.snapshot(), "reason": str(exc)})
                    code = 1
                    if owner.active and not stop.is_set() and not owner.data["fallback_attempted"]:
                        try:
                            fallback(executor, lambda: collect_next(False), wait, emit)
                        except CollectionCancelled:
                            normal_stop = True
                        except ExternalOverride as override:
                            relinquish(override)
                        except OwnershipUnverified as unverified:
                            emit("ownership_unverified", {**executor.snapshot(), "reason": str(unverified)})
                        except Exception as fallback_error:
                            emit("fallback_failed", {**executor.snapshot(), "reason": str(fallback_error)})
                finally:
                    normal_stop = normal_stop or stop.is_set()
                    try:
                        emit("shutdown", {**executor.snapshot(), "reason": "external-override" if yielded_exit else
                                          "requested" if stop.is_set() else "finished",
                                          "last_recommended_level": last_report["recommended_level"] if last_report else None})
                    finally:
                        if not yielded_exit and not executor.ownership_pending:
                            owner.finish()
                    notify("STOPPING=1")
        except CollectionCancelled:
            code, normal_stop = 0, True
        except ControlSuspended as exc:
            yielded_exit = True
            print(f"hrx-fancontrol: {exc}", file=sys.stderr)
        except (OSError, ValueError) as exc:
            print(f"hrx-fancontrol: {exc}", file=sys.stderr)
            code = 1
        finally:
            if logger:
                try:
                    logger.close()
                    if logger.error or logger.dropped:
                        print(f"Logging incomplete: {logger.error or 'queue overflow'}; dropped={logger.dropped}", file=sys.stderr)
                        code = 1
                except Exception:
                    if not yielded_exit:
                        raise
            if yielded_exit:
                return OVERRIDE_EXIT_STATUS
    # A diagnostic/marker I/O error during a requested stop must never become crash recovery.
    # systemd treats 78 as clean termination; ExecStopPost independently recognizes it.
    return 78 if normal_stop and code else code
