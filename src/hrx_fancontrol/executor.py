"""Actuation state machine: sending is not evidence that a fan setting took effect."""
import time

from .calibration import SAMPLE_INTERVAL, SETTLE_SECONDS, STABLE_SAMPLES, rpm_matches, rpm_ranges
from .ipmi import check_cancelled, parse_control_status
from .protocol import FANS, LEVEL_BYTES, matches_level, matches_parameter, level_for_parameter, validate_parameter


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
        self.rpm_window = []
        self.confirmed_target = None
        self.yielded = False
        self.ownership_pending = False

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
                "rpm_calibrated": self.target is not None and self.profile is not None,
                "status": self.status, "verified": self.status == "verified", "stable_samples": self.confirmations,
                "required_samples": STABLE_SAMPLES, "command": self.command, "error": self.error}

    def request(self, level, *, fallback=False):
        if type(level) is not int or level not in LEVEL_BYTES:
            raise ValueError("Expected level 1..5")
        self._request(LEVEL_BYTES[level], level, fallback=fallback)

    def request_parameter(self, parameter):
        validate_parameter(parameter)
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
        self.rpm_window = []
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

    def early_l5(self, report):
        if report["recommended_level"] == 5 and self.target != 5:
            self.request(5)

    def update(self, report):
        # Readback belongs to the OLD target, even if this poll requests a new one.
        self.check_readback(report)
        if self.ownership_pending:
            self.status = "waiting-for-control-status"
            return
        level = report["recommended_level"]
        if level is None:
            return
        target = level
        sensor_valid = report["collection"]["sensor_data_valid"]
        raw = report["bmc"]["raw_bytes"]
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
            # Existing physical setting can be verified without issuing a redundant write.
            if (report["collection"]["status"] == "ok" and sensor_valid and raw is not None and
                    matches_level(raw, target) and rpm_matches(report, self.profile, target)):
                self.target, self.status = target, "pending-verification-existing"
                self.parameter = LEVEL_BYTES[target]
                self.completed_at = report["sampled_at_monotonic"] - SETTLE_SECONDS
                self.confirmations = self.mismatches = 0
                self.rpm_window = []
                self.last_sample = None
            else:
                self.request(target)
                return  # Neither old status nor old RPM can verify a command just issued.
        self.observe(report)

    def check_readback(self, report):
        """Check independently available status against the last actual write target."""
        raw = report["bmc"]["raw_bytes"]
        self.check_external_change(raw)
        if raw is None or self.parameter is None:
            return
        if not matches_parameter(raw, self.parameter):
            self.fail("BMC mode or six installed duty bytes differ from the requested target")
        self._confirm_current_target()

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
        self.check_external_change(raw)
        self._confirm_current_target()

    def _wait_for_ownership(self):
        self.status = "waiting-for-control-status"
        raise OwnershipUnverified("等待核实上次运行的 BMC 设置，未发送调速命令。")

    def observe(self, report):
        if self.target is None:
            return
        self.check_readback(report)
        if report["collection"]["status"] != "ok" or not report["collection"]["sensor_data_valid"]:
            # Communication loss is handled by ThermalEngine's 5-poll/300s rule.
            self.confirmations = 0
            self.rpm_window = []
            self.status = "waiting-for-data"
            return
        if report["bmc"]["raw_bytes"] is None:
            self.status = "waiting-for-data"
            self.confirmations = 0
            self.rpm_window = []
            return
        sampled_at = report["sampled_at_monotonic"]
        if self.completed_at is None or sampled_at < self.completed_at + SETTLE_SECONDS:
            return
        if self.last_sample is not None and sampled_at - self.last_sample < SAMPLE_INTERVAL:
            return
        self.last_sample = sampled_at
        if not rpm_matches(report, self.profile, self.target):
            self.confirmations = 0
            self.rpm_window = []
            self.mismatches += 1
            self.status = "rpm-mismatch"
            if self.mismatches >= STABLE_SAMPLES:
                self.fail("Three fresh post-settle RPM samples differ from calibration")
        else:
            self.rpm_window.append({"rpm": {name: report["fans"][name]["rpm"] for name in FANS}})
            self.rpm_window = self.rpm_window[-STABLE_SAMPLES:]
            try:
                rpm_ranges(self.rpm_window)
            except ValueError:
                self.confirmations = 0
                self.mismatches += 1
                self.status = "rpm-unstable"
                if self.mismatches >= STABLE_SAMPLES:
                    self.fail("Persistent unstable fresh RPM samples")
                return
            self.mismatches = 0
            self.confirmations = len(self.rpm_window)
            old_status = self.status
            self.status = "verified" if self.confirmations >= STABLE_SAMPLES else "pending-verification"
            if self.status == "verified" and old_status != self.status:
                self.emit("verified", self.snapshot())

    def fail(self, error):
        self.error, self.status = error, "failed"
        self.emit("verification_failed", self.snapshot())
        raise ExecutionFailure(error)
