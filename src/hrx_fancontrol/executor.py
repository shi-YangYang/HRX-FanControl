"""Actuation state machine: sending is not evidence that a fan setting took effect."""
import time

from .ipmi import check_cancelled, parse_control_status
from .protocol import LEVEL_BYTES, matches_level, matches_parameter, level_for_parameter, validate_parameter


CONFIRM_TIMEOUT = 90.0
READBACK_MAX_AGE = 30.0


class ExecutionFailure(RuntimeError):
    pass


class ExternalOverride(RuntimeError):
    """A previously confirmed setting changed; relinquish without another write."""


class OwnershipUnverified(RuntimeError):
    """An interrupted run's setting has not yet been checked against the BMC."""


class Executor:
    def __init__(self, ipmi, profile, owner, emit, stop, clock=time.monotonic):
        self.ipmi, self.profile, self.owner, self.emit, self.stop = ipmi, profile, owner, emit, stop
        self.clock = clock
        self.parameter = None
        self.confirmed_parameter = None
        self.target = None
        self.status = "waiting-for-data"
        self.completed_at = None
        self.last_sample = None
        self.confirmations = self.mismatches = 0
        self.command = None
        self.error = None
        self.confirmed_target = None
        self.yielded = False
        self.ownership_pending = False
        self.raw = None
        self.read_at = None

    def resume(self):
        self.owner.resume()
        self.__init__(self.ipmi, self.profile, self.owner, self.emit, self.stop, self.clock)

    def restore_confirmed(self, level):
        # A durable target is historical evidence, not permission to write now.
        parameter = level.get("parameter") if isinstance(level, dict) else LEVEL_BYTES.get(level)
        self.parameter = self.confirmed_parameter = parameter
        self.target = self.confirmed_target = level_for_parameter(parameter)
        self.ownership_pending = parameter is not None

    def snapshot(self):
        return {"target_level": self.target, "parameter": self.parameter, "byte": f"0x{self.parameter:02x}" if self.parameter else None,
                "bmc_confirmed": (not self.ownership_pending and self.parameter is not None and
                                  self.confirmed_parameter == self.parameter),
                "ownership_pending": self.ownership_pending,
                "rpm_calibrated": False, "confirmation": "bmc-setting",
                "bmc_raw_bytes": self.raw, "bmc_read_age_seconds":
                    round(max(0, self.clock() - self.read_at), 1) if self.read_at is not None else None,
                "status": self.status, "verified": self.status == "verified", "stable_samples": self.confirmations,
                "required_samples": 1, "command": self.command, "error": self.error}

    def request(self, level, *, fallback=False):
        if type(level) is not int or level not in LEVEL_BYTES:
            raise ValueError("Expected level 1..5")
        self._request(LEVEL_BYTES[level], level, fallback=fallback)

    def request_parameter(self, parameter):
        validate_parameter(parameter)
        self.readback()
        if self.raw is not None and self.clock() - self.read_at <= READBACK_MAX_AGE and matches_parameter(self.raw, parameter):
            self.target, self.parameter = level_for_parameter(parameter), parameter
            self.completed_at = self.clock()
            self._confirm_current_target()
            return
        self._request(parameter, level_for_parameter(parameter), manual=True)

    def _request(self, parameter, level, *, fallback=False, manual=False):
        check_cancelled(self.stop)
        self.guard_external_change()
        if manual:
            self.owner.before_write(level, fallback=fallback, parameter=parameter)
        else:
            self.owner.before_write(level, fallback=fallback)
        self.confirmed_target = self.confirmed_parameter = None
        self.parameter = parameter
        self.target, self.status = level, "write-in-flight-unknown"
        self.confirmations = self.mismatches = 0
        self.completed_at = self.last_sample = None
        self.error = self.command = None
        self.emit("write_started", self.snapshot())
        result = self.ipmi.set_parameter(parameter) if manual else self.ipmi.set_level(level)
        self.command = result.summary()
        self.completed_at = self.clock()
        if not result.succeeded:
            self.status = "failed-unknown"
            self.error = result.error or result.stderr
            self.emit("write_failed", self.snapshot())
            raise ExecutionFailure(f"Fan write failed or may have executed: {self.error}")
        self.status = "pending-verification"
        self.emit("write_sent", self.snapshot())
        self.readback()

    def early_l5(self, report):
        if report["recommended_level"] == 5 and self.target != 5:
            self.request(5)

    def update(self, report):
        # Readback belongs to the OLD target, even if this poll requests a new one.
        fresh_status = self.check_readback(report)
        if self.ownership_pending:
            self.status = "waiting-for-control-status"
            return
        level = report["recommended_level"]
        if level is None:
            return
        target = level
        sensor_valid = report["collection"]["sensor_data_valid"]
        raw = report["bmc"]["raw_bytes"] if fresh_status else None
        holding = report.get("recovery", {}).get("holding", False)
        if target < 5 and (not sensor_valid or (self.target is None and holding)):
            if self.target is None:
                # A startup read-failure floor must not lower an existing L4/L5.
                # The first usable readback may arrive during recovery.
                # Duty bytes establish a setting, never RPM verification.
                physical = next((candidate for candidate in LEVEL_BYTES
                                 if raw is not None and matches_level(raw, candidate)), None)
                if physical is None:
                    return  # Keep an unknown setting until valid data and recovery permit control.
                self.target = physical
                self.parameter = LEVEL_BYTES[physical]
            target = max(target, self.target)
        if holding and self.target is not None:
            target = max(target, self.target)
        if target != self.target or (self.completed_at is None and sensor_valid):
            # The current BMC setting is sufficient; RPM need not change.
            if raw is not None and self.raw == raw and matches_level(raw, target):
                self.target, self.parameter = target, LEVEL_BYTES[target]
                self.completed_at = self.clock()
                self._confirm_current_target()
            else:
                self.request(target)
                return  # The report predates the write; only its new readback can confirm it.
        self.check_deadline()

    def check_readback(self, report):
        """Use the status read's own timestamp, independently of temperature validity."""
        # Only readback() starts a bounded query before the deadline and may
        # accept its in-flight result afterward. A completed poll cannot do so.
        self.check_deadline()
        accepted = self._accept_readback(report["bmc"]["raw_bytes"],
                              report["bmc"].get("read_at_monotonic", report["sampled_at_monotonic"]))
        self.check_deadline()
        return accepted

    def _accept_readback(self, raw, read_at, *, direct=False):
        if (raw is None or self.clock() - read_at > READBACK_MAX_AGE or
                (self.read_at is not None and read_at < self.read_at) or
                (self.completed_at is not None and
                 (read_at < self.completed_at or (not direct and read_at == self.completed_at)))):
            return False
        self.raw, self.read_at = raw, read_at
        self.check_external_change(raw)
        if self.parameter is not None and matches_parameter(raw, self.parameter):
            self._confirm_current_target()
        return True

    def readback(self):
        """One bounded, serial BMC read; an unavailable/mismatching result stays pending."""
        check_cancelled(self.stop)
        self.check_deadline()
        result = self.ipmi.control_status()
        check_cancelled(self.stop)
        if result.succeeded:
            try:
                raw = parse_control_status(result.stdout)
            except ValueError:
                pass
            else:
                self._accept_readback(raw, self.clock(), direct=True)
        self.check_deadline()
        return self.status == "verified"

    def check_deadline(self):
        if (self.completed_at is not None and self.status != "verified" and
                self.clock() >= self.completed_at + CONFIRM_TIMEOUT):
            self.fail("BMC setting confirmation timed out after 90 seconds")

    @property
    def awaiting_confirmation(self):
        return self.status == "pending-verification"

    def _confirm_current_target(self):
        if self.confirmed_parameter != self.parameter or self.ownership_pending:
            # Keep observed BMC state even if saving it fails; fallback must still
            # detect an external change. Only durable confirmation opens recovery.
            self.confirmed_target = self.target
            self.confirmed_parameter = self.parameter
            if self.target is None:
                self.owner.confirm(None, parameter=self.parameter)
            else:
                self.owner.confirm(self.target)
            self.ownership_pending = False
        previous = self.status
        self.status, self.confirmations = "verified", 1
        if previous != "verified":
            self.emit("verified", self.snapshot())

    def check_external_change(self, raw):
        if self.yielded:
            raise ExternalOverride(self.error)
        if (raw is not None and self.confirmed_parameter is not None and
                not matches_parameter(raw, self.confirmed_parameter)):
            self.yielded = True  # Set before any logging or disk I/O can fail.
            self.status = "external-override"
            self.error = "Previously confirmed BMC mode or duty bytes changed"
            raise ExternalOverride(self.error)

    def guard_external_change(self):
        """Recheck before every new write, including exception/service fallback."""
        self.check_external_change(None)
        if self.confirmed_parameter is None:
            return
        result = self.ipmi.control_status()
        check_cancelled(self.stop)
        if not result.succeeded:
            if self.ownership_pending:
                self._wait_for_ownership()
            return  # A failed read alone cannot identify an external change.
        try:
            raw = parse_control_status(result.stdout)
        except ValueError:
            if self.ownership_pending:
                self._wait_for_ownership()
            return
        self._accept_readback(raw, self.clock(), direct=True)

    def _wait_for_ownership(self):
        self.status = "waiting-for-control-status"
        raise OwnershipUnverified("等待核实上次运行的 BMC 设置，未发送调速命令。")

    def observe(self, report):
        # Retained for callers of the previous executor interface; no RPM gate.
        self.check_readback(report)

    def fail(self, error):
        self.error, self.status = error, "failed"
        self.emit("verification_failed", self.snapshot())
        raise ExecutionFailure(error)
