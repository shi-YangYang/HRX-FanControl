"""Safety recommendations only. This module cannot actuate hardware."""


class Safety:
    def __init__(self, settings):
        self.settings = settings
        self.previous_level = settings["safe_level"]
        self.holding = True
        self.healthy_since = None

    def update(self, requested, faults, emergency, now):
        if faults or emergency:
            self.holding = True
            self.healthy_since = None
            level = max(requested, self.previous_level, 5 if emergency else self.settings["safe_level"])
            state = "emergency" if emergency else "safe-mode"
        elif self.holding:
            if self.healthy_since is None:
                self.healthy_since = now
            if now - self.healthy_since < self.settings["recovery_delay_seconds"]:
                level = max(requested, self.previous_level)
                state = "startup-or-recovery"
            else:
                self.holding = False
                level, state = requested, "normal"
        else:
            level, state = requested, "normal"
        self.previous_level = level
        return level, state
