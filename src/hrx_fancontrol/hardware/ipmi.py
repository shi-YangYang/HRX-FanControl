"""An intentionally closed read-only IPMI interface; no arbitrary command API."""
from __future__ import annotations

import os
import re
import signal
import subprocess
import time
from dataclasses import asdict, dataclass
from typing import Optional


DIAGNOSTIC_TAIL_CHARS = 2048
_CANCEL_CHECK_SECONDS = 0.1
_TERMINATE_SECONDS = 0.5


class CollectionCancelled(Exception):
    """The user stopped collection; this is not a failed sensor sample."""


def check_cancelled(stop):
    if stop is not None and stop.is_set():
        raise CollectionCancelled()


def _text(output):
    return output.decode("utf-8", errors="replace") if isinstance(output, bytes) else output or ""


def _byte_count(output):
    return len(output) if isinstance(output, bytes) else len((output or "").encode("utf-8"))


@dataclass(frozen=True)
class CommandResult:
    command: tuple
    stdout: str
    stderr: str
    returncode: Optional[int]
    error: Optional[str]
    elapsed_seconds: float
    timeout_seconds: Optional[float] = None
    failure_kind: Optional[str] = None
    stdout_bytes: int = 0
    stderr_bytes: int = 0
    stdout_lines: int = 0
    stdout_tail: str = ""

    @property
    def succeeded(self):
        return self.returncode == 0 and not self.error and not self.stderr

    def summary(self, parse_failed=False):
        result = asdict(self)
        result.pop("stdout")
        if parse_failed:
            result.update(failure_kind="parse", error="IPMI output parse failed",
                          stdout_tail=self.stdout[-DIAGNOSTIC_TAIL_CHARS:])
        return result


class ReadOnlyIPMI:
    def __init__(self, timeout=20, status_timeout=3, stop=None):
        self.timeout = timeout
        self.status_timeout = status_timeout
        self.stop = stop

    def sensors(self):
        return self._read(("sensor",))

    def control_status(self):
        return self._read(("raw", "0x2e", "0x31"))

    def _read(self, args):
        if args not in (("sensor",), ("raw", "0x2e", "0x31")):
            raise ValueError("Only allowlisted read operations are supported")
        command = ("ipmitool", "-I", "open", *args)
        timeout = self.timeout if args == ("sensor",) else self.status_timeout
        return self._execute(command, timeout)

    def _execute(self, command, timeout):
        check_cancelled(self.stop)
        start = time.monotonic()
        try:
            # A terminal Ctrl+C must reach the parent first, not truncate ipmitool's output.
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                       stderr=subprocess.PIPE, start_new_session=True,
                                       env={**os.environ, "LC_ALL": "C"})
        except OSError as exc:
            return self._result(command, start, timeout, b"", b"", None, "spawn", str(exc))
        needs_cleanup = True
        try:
            while True:
                check_cancelled(self.stop)
                remaining = timeout - (time.monotonic() - start)
                if remaining <= 0:
                    needs_cleanup = False
                    stdout, stderr = self._terminate(process)
                    check_cancelled(self.stop)
                    return self._result(command, start, timeout, stdout, stderr,
                                        process.returncode, "timeout", "IPMI timeout")
                try:
                    stdout, stderr = process.communicate(timeout=min(_CANCEL_CHECK_SECONDS, remaining))
                    needs_cleanup = False
                    break
                except subprocess.TimeoutExpired:
                    # communicate retains partial bytes between calls. They are never policy input.
                    continue
            check_cancelled(self.stop)
            if process.returncode != 0:
                failure, error = "nonzero-exit", f"ipmitool exit {process.returncode}"
            elif _text(stderr).strip():
                failure, error = "diagnostic", "IPMI diagnostic output"
            else:
                failure, error = None, None
            return self._result(command, start, timeout, stdout, stderr, process.returncode, failure, error)
        except BaseException:
            if needs_cleanup:
                self._terminate(process)
            raise

    @staticmethod
    def _terminate(process):
        # Signal the private process group, including any descendants holding the pipes open.
        for sig in (signal.SIGTERM, signal.SIGKILL):
            try:
                os.killpg(process.pid, sig)
            except ProcessLookupError:
                pass
            try:
                return process.communicate(timeout=_TERMINATE_SECONDS)
            except subprocess.TimeoutExpired:
                continue
        # Do not leave an unbounded wait on inherited pipes if a descendant changed its session.
        process.stdout.close()
        process.stderr.close()
        try:
            process.wait(timeout=_TERMINATE_SECONDS)
        except subprocess.TimeoutExpired as exc:
            raise OSError("IPMI process did not exit after SIGKILL") from exc
        raise OSError("IPMI process pipes did not close after termination")

    @staticmethod
    def _result(command, start, timeout, stdout, stderr, returncode, failure, error):
        output, diagnostic = _text(stdout), _text(stderr)
        return CommandResult(
            command, output if failure is None else "", diagnostic.strip()[-DIAGNOSTIC_TAIL_CHARS:],
            returncode, error, time.monotonic() - start, timeout, failure,
            _byte_count(stdout), _byte_count(stderr), len(output.splitlines()),
            output[-DIAGNOSTIC_TAIL_CHARS:] if failure else "",
        )


def parse_control_status(text):
    tokens = text.split()
    # HR650X observations contain a header and eight channel bytes. Meaning is unverified.
    if len(tokens) != 9 or any(re.fullmatch(r"[0-9a-fA-F]{2}", token) is None for token in tokens):
        raise ValueError("Expected nine hexadecimal BMC status bytes; firmware layout unverified")
    return [f"0x{int(token, 16):02x}" for token in tokens]
