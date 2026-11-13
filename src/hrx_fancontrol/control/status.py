"""Published service state; socket readers never inspect a live executor."""
from copy import deepcopy
import threading
import time


class StatusCache:
    def __init__(self, clock=time.monotonic):
        self.clock = clock
        self.lock = threading.Lock()
        self.value = None
        self.sampled_at = self.read_at = None

    def publish(self, value, sampled_at=None, read_at=None):
        value = deepcopy(value)
        with self.lock:
            self.value, self.sampled_at, self.read_at = value, sampled_at, read_at

    def read(self):
        with self.lock:
            value = deepcopy(self.value)
            sampled_at, read_at = self.sampled_at, self.read_at
        now = self.clock()
        value["sample_age_seconds"] = round(max(0, now - sampled_at), 1) if sampled_at is not None else None
        value["control"]["bmc_read_age_seconds"] = round(max(0, now - read_at), 1) if read_at is not None else None
        return value
