"""Safety recommendations only. This module cannot actuate hardware."""


def _highest(*levels):
    return max((level for level in levels if level is not None), default=None)


class Safety:
    def __init__(self, settings):
        self.settings = settings
        self.previous_level = None
        self.holding = False
        self.healthy_samples = 0
        self.consecutive_failures = 0
        self.started_at = None
        self.last_valid_sample_at = None
        self.read_failure_reasons = []

    def preview(self, requested, faults, emergency):
        """Report known hardware alarms before readback, without committing a sample."""
        if faults or emergency:
            return (_highest(requested, self.previous_level, 5 if emergency else self.settings["safe_level"]),
                    "emergency" if emergency else "safe-mode")
        level = _highest(requested, self.previous_level)
        return level, "collection-pending" if level is not None else "waiting-for-sample"

    def _sample_age(self, now):
        reference = self.last_valid_sample_at if self.last_valid_sample_at is not None else self.started_at
        return max(0.0, now - reference) if reference is not None else 0.0

    def read_failure_status(self, now):
        return {
            "consecutive_failures": self.consecutive_failures,
            "count_threshold": self.settings["read_failure_count"],
            "no_valid_sample_seconds": round(self._sample_age(now), 3),
            "time_threshold_seconds": self.settings["read_failure_timeout_seconds"],
            "fallback_level": self.settings["read_failure_level"],
            "trigger_reasons": list(self.read_failure_reasons),
        }

    def recovery_status(self):
        return {"healthy_samples": self.healthy_samples,
                "required_samples": self.settings["recovery_success_count"], "holding": self.holding}

    def update(self, requested, faults, emergency, now, *, read_failed=False,
               valid_sample=True, sampled_at=None, gap=False, alerting=False):
        """Commit known demand; only complete healthy polls advance recovery."""
        sampled_at = now if sampled_at is None else sampled_at
        if self.started_at is None:
            self.started_at = min(now, sampled_at)
        # Check the old observation before accepting a fresh sample after a long silence.
        overdue = self._sample_age(now) >= self.settings["read_failure_timeout_seconds"]
        if read_failed:
            self.consecutive_failures += 1
        elif valid_sample:
            self.consecutive_failures = 0
        reasons = []
        if self.consecutive_failures >= self.settings["read_failure_count"]:
            reasons.append("failure-count")
        if overdue:
            reasons.append("no-valid-sample-timeout")
        for reason in reasons:
            if reason not in self.read_failure_reasons:
                self.read_failure_reasons.append(reason)
        if valid_sample:
            self.last_valid_sample_at = sampled_at
        floor = self.settings["read_failure_level"] if self.read_failure_reasons else None
        unhealthy = read_failed or not valid_sample or faults or emergency
        if unhealthy or gap or overdue:
            self.healthy_samples = 0
            self.holding = self.previous_level is not None or floor is not None
        if faults or emergency:
            level, state = self.preview(_highest(requested, floor), faults, emergency)
            self.holding = True
        elif read_failed or not valid_sample:
            level = _highest(requested, self.previous_level, floor)
            self.holding = level is not None
            state = ("waiting-for-sample" if level is None else
                     "read-failure-degraded" if self.read_failure_reasons else "read-failure-hold")
        elif self.holding:
            # Advisory alarms do not start a hold, but cannot prove fault recovery.
            self.healthy_samples = 0 if alerting else self.healthy_samples + 1
            if self.healthy_samples < self.settings["recovery_success_count"]:
                level = _highest(requested, self.previous_level, floor)
                state = "recovery"
            else:
                self.holding = False
                self.read_failure_reasons.clear()
                level, state = requested, "normal"
        else:
            self.healthy_samples = 0
            level, state = requested, "normal"
        self.previous_level = level
        return level, state
