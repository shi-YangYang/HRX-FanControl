"""An intentionally closed read-only IPMI interface; no arbitrary command API."""
from __future__ import annotations

import os
import re
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Optional


@dataclass(frozen=True)
class CommandResult:
    command: tuple
    stdout: str
    stderr: str
    returncode: Optional[int]
    error: Optional[str]
    elapsed_seconds: float

    def summary(self):
        result = asdict(self)
        result.pop("stdout")
        return result


class ReadOnlyIPMI:
    def __init__(self, timeout=3):
        self.timeout = timeout

    def sensors(self):
        return self._read(("sensor",))

    def control_status(self):
        return self._read(("raw", "0x2e", "0x31"))

    def _read(self, args):
        if args not in (("sensor",), ("raw", "0x2e", "0x31")):
            raise ValueError("Only allowlisted read operations are supported")
        command = ("ipmitool", "-I", "open", *args)
        start = time.monotonic()
        try:
            result = subprocess.run(command, capture_output=True, text=True, errors="replace",
                                    timeout=self.timeout, check=False,
                                    env={**os.environ, "LC_ALL": "C"})
            error = None if result.returncode == 0 else f"ipmitool exit {result.returncode}"
            return CommandResult(command, result.stdout, result.stderr.strip(), result.returncode,
                                 error, time.monotonic() - start)
        except subprocess.TimeoutExpired:
            # Partial timed-out output is not a fresh complete sample.
            return CommandResult(command, "", "", None, "IPMI timeout", time.monotonic() - start)
        except OSError as exc:
            return CommandResult(command, "", "", None, str(exc), time.monotonic() - start)


def parse_control_status(text):
    tokens = text.split()
    # HR650X observations contain a header and eight channel bytes. Meaning is unverified.
    if len(tokens) != 9 or any(re.fullmatch(r"[0-9a-fA-F]{2}", token) is None for token in tokens):
        raise ValueError("Expected nine hexadecimal BMC status bytes; firmware layout unverified")
    return [f"0x{int(token, 16):02x}" for token in tokens]
