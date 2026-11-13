"""Each component owns its own downshift timer, measured with a monotonic clock."""
from dataclasses import dataclass
from typing import Optional


@dataclass
class Hysteresis:
    level: int = 1
    below_since: Optional[float] = None

    def invalidate(self):
        self.below_since = None

    def update(self, temperature, thresholds, now, settings):
        requested = 1 + sum(temperature >= threshold for threshold in thresholds)
        if requested >= self.level or not settings["enabled"]:
            self.level = requested
            self.below_since = None
        elif temperature <= thresholds[self.level - 2] - settings["downshift_margin_celsius"]:
            if self.below_since is None:
                self.below_since = now
            if now - self.below_since >= settings["downshift_delay_seconds"]:
                self.level -= 1
                self.below_since = None
        else:
            self.below_since = None
        return self.level
