"""One service event loop owns all hardware calls and operator mode transitions."""
import time

from .commands import validate_command
from .executor import ExternalOverride
from ..hardware.ipmi import check_cancelled
from .lifecycle import notify
from .service_state import ServiceState
from ..policy.thermal_engine import ThermalEngine


class ServiceController:
    def __init__(self, executor, config, state_directory, collect_next, publish, clock=time.monotonic):
        self.executor, self.config = executor, config
        self.collect_next, self.publish, self.clock = collect_next, publish, clock
        self.state = ServiceState(state_directory, executor.owner.data["identity"])
        self.engine = ThermalEngine(config)
        self.last_report = None
        self.error = None
        self.generation = 0
        self.manual_pending = False
        self.allow_fallback = self.state.data["mode"] == "automatic"
        self.next_poll = clock()
        mode = "suspended" if executor.yielded else self.state.data["mode"]
        # Startup remembers intent but never reissues a saved manual command.
        self.state.set(mode, self.state.data["parameter"] if mode == "manual" else None)
        if mode != "automatic" and not executor.ownership_pending:
            executor.owner.finish()

    def snapshot(self):
        mode = self.state.data["mode"]
        report = self.last_report
        return {"mode": mode, "automatic": mode == "automatic", "manual_parameter": self.state.data["parameter"],
                "operation_active": self.manual_pending, "control": self.executor.snapshot(),
                "last_sample_time": report.get("timestamp") if report else None,
                "sample_age_seconds": round(max(0, self.clock() - report["sampled_at_monotonic"]), 1) if report else None,
                "readings_are_cached": True,
                "bmc_raw_bytes": self.executor.raw,
                "fans_rpm": {name: fan["rpm"] for name, fan in report["fans"].items()} if report else None,
                "error": self.error}

    def suspend(self, exc):
        self.manual_pending = False
        self.allow_fallback = False
        self.error = str(exc)
        # The executor's yielded flag already blocks every future request/fallback.
        try:
            self.executor.owner.relinquish()
        finally:
            self.state.set("suspended")
        self.executor.emit("external_override", {**self.executor.snapshot(), "reason": str(exc)})

    def apply(self, command):
        validate_command(command)
        action = command["action"]
        if action == "status":
            return "最近一次采样与控制状态（缓存）。"
        if (action == "on" and self.state.data["mode"] == "automatic" and
                not self.executor.ownership_pending and not self.executor.yielded):
            return "自动调控已开启。"
        self.generation += 1
        self.allow_fallback = False
        self.manual_pending = False
        self.error = None
        if action == "off":
            self.state.set("suspended" if self.executor.yielded else "paused")
            self.executor.owner.finish()
            return "自动采样与调控已关闭，保持当前风扇设置。"
        # Persist OFF before clearing ownership or writing any manual target.
        self.state.set("manual" if action == "manual" else "paused", command.get("parameter"))
        self.executor.resume()
        self.engine = ThermalEngine(self.config)
        if action == "on":
            self.state.set("automatic")
            self.allow_fallback = True
            self.next_poll = self.clock()
            return "自动调控已开启，等待新的有效采样。"
        self.state.set("manual", command["parameter"], active=True)
        self.manual_pending = True
        self.allow_fallback = True
        self.executor.request_parameter(command["parameter"])
        if self._finish_manual():
            return "手动设置已确认，BMC 回读匹配；自动调控已关闭。"
        self.next_poll = self.clock() + 5
        return "手动命令已返回，自动已关闭；等待 BMC 回读。"

    def _finish_manual(self):
        if self.executor.status != "verified":
            return False
        self.state.set("manual", self.state.data["parameter"], active=False)
        self.manual_pending = False
        self.allow_fallback = False
        self.executor.owner.finish()
        return True

    def commands(self, server):
        for request in server.pending():
            try:
                message = self.apply(request.command)
                request.reply({"ok": True, "message": message, "status": self.snapshot()})
            except ExternalOverride as exc:
                self.suspend(exc)
                request.reply({"ok": False, "message": "检测到外部修改，已让位。", "status": self.snapshot()})
            except Exception as exc:
                request.reply({"ok": False, "message": str(exc), "status": self.snapshot()})
                raise  # Persistence or execution failures are not silently converted to success.

    def tick(self, server):
        self.commands(server)
        automatic = self.state.data["mode"] == "automatic"
        if not automatic and not self.manual_pending:
            return
        self.executor.check_deadline()
        if self.clock() < self.next_poll:
            return
        if self.manual_pending or self.executor.awaiting_confirmation:
            # Finish the bounded setting transaction before starting another
            # sensor poll; slow sensors must not postpone its BMC query deadline.
            try:
                self.executor.readback()
                if self.manual_pending:
                    self._finish_manual()
            except ExternalOverride as exc:
                self.suspend(exc)
            self.next_poll = self.clock() + (0 if automatic and self.executor.status == "verified" else 5)
            return
        generation = self.generation
        started = self.clock()
        report = self.collect_next(self.engine, automatic)
        check_cancelled(self.executor.stop)
        self.last_report = report
        # A command queued during a slow read takes precedence over its old recommendation.
        self.commands(server)
        if self.generation != generation:
            return
        try:
            self.executor.update(report)
            self.publish(report, "automatic" if automatic else "manual")
        except ExternalOverride as exc:
            self.suspend(exc)
        from ..hardware.collection import poll_wait_seconds
        delay = (5 if self.executor.awaiting_confirmation else
                 poll_wait_seconds(report, self.config["controller"]["poll_interval_seconds"], self.clock() - started))
        self.next_poll = self.clock() + delay

    def run(self, server):
        while not self.executor.stop.is_set():
            self.tick(server)
            notify("WATCHDOG=1")  # Idle is intentional; a blocked hardware call cannot reach this line.
            server.wake.wait(.2)
