"""Reuse SDR metadata, never readings or thresholds; all subprocesses stay serial."""
from dataclasses import dataclass
import hashlib
import math
from pathlib import Path
import re
import stat
import tempfile
import time

from .ipmi import CommandResult, check_cancelled, supported_sensor_name
from .sensors import parse_sensors

REFRESH_SECONDS = 3600
RETRY_SECONDS = 300
MAX_CACHE_BYTES = 4 * 1024 * 1024


class CacheError(ValueError):
    pass


class EmptyRepository(CacheError):
    pass


class InventoryMismatch(CacheError):
    pass


def repository_info(text):
    fields = text.split()
    if len(fields) != 14 or any(not re.fullmatch(r"[0-9a-fA-F]{2}", value) for value in fields):
        raise CacheError("Invalid SDR repository information")
    data = bytes(int(value, 16) for value in fields)
    count = int.from_bytes(data[1:3], "little")
    if count == 0:
        # ipmitool sensor/dump may rebuild an empty SDR. Never invoke that path here.
        raise EmptyRepository("SDR repository is empty; discovery was not attempted")
    if data[0] not in (0x51, 0x01, 0x02):
        raise CacheError("Unsupported SDR version")
    return data, count


def validate_dump(data, count, sensor_count):
    offset, ids, sensors = 0, set(), 0
    if not data or len(data) > MAX_CACHE_BYTES:
        raise CacheError("Invalid SDR cache size")
    while offset < len(data):
        if offset + 5 > len(data):
            raise CacheError("Truncated SDR header")
        record_id = int.from_bytes(data[offset:offset + 2], "little")
        version, kind, length = data[offset + 2:offset + 5]
        if version not in (0x51, 0x01, 0x02) or not length or record_id in ids or offset + 5 + length > len(data):
            raise CacheError("Invalid or truncated SDR record")
        if kind in (1, 2):
            base = 43 if kind == 1 else 27
            if length < base:
                raise CacheError("Truncated SDR sensor record")
            name_length = data[offset + 5 + base - 1] & 0x1f
            if name_length > 16 or length < base + name_length:
                raise CacheError("Truncated or unsupported SDR sensor name")
            sensors += 1
        ids.add(record_id)
        offset += 5 + length
    if len(ids) != count:
        raise CacheError("SDR cache record count does not match the repository")
    # Only a complete directory can prove that the sensor table omitted rows.
    if sensors != sensor_count:
        raise InventoryMismatch("SDR inventory does not match the complete sensor table")


def selected_names(text, required):
    readings, errors = parse_sensors(text)
    if errors:
        raise InventoryMismatch("; ".join(errors))
    names = []
    for line in text.splitlines():
        if not line.strip():
            continue
        fields = [value.strip() for value in line.split("|")]
        name, raw, unit = fields[:3]
        reading = readings[name.casefold()]
        # Unknown NA sensors can become thermal when their device returns.
        if reading.thermal or unit.casefold() == "rpm" or name.casefold() in required or raw == "na" or not unit:
            if not supported_sensor_name(name):
                raise CacheError("Sensor names are not compatible with cached CSV output")
            names.append(name)
    if not names or len(names) > 512:
        raise CacheError("Unsupported cached sensor inventory size")
    return tuple(names), len(readings)


def csv_table(text, names, returncode):
    """ipmitool 1.8.19 -c sensor get: one preamble, then ten unquoted fields."""
    lines = text.splitlines()
    if not lines or lines.pop(0) != "Locating sensor record...":
        raise CacheError("Missing sensor-get preamble")
    expected, seen, rows, unavailable = set(names), set(), [], False
    for line in lines:
        fields = [value.strip() for value in line.split(",")]
        if len(fields) != 10 or fields[0] not in expected or fields[0] in seen:
            raise CacheError("Incomplete, duplicate or unexpected cached sensor rows")
        name, raw, unit, status = fields[:4]
        if any("|" in value for value in fields):
            raise CacheError("Ambiguous sensor field")
        for value in (raw, *fields[4:]):
            if value == "na" or re.fullmatch(r"0x[0-9a-fA-F]+", value):
                continue
            try:
                valid = math.isfinite(float(value))
            except ValueError:
                valid = False
            if not valid:
                raise CacheError("Invalid numeric cached sensor field")
        if status not in ("ok", "nc", "cr", "nr", "lnc", "lcr", "lnr", "unc", "ucr", "unr", "na", "ns") and not re.fullmatch(r"0x[0-9a-fA-F]+", status):
            raise CacheError("Invalid cached sensor status")
        if (raw == "na") != (status == "na"):
            raise CacheError("Inconsistent unavailable sensor fields")
        unavailable |= raw == "na"
        seen.add(name)
        rows.append(" | ".join(fields))
    if seen != expected or returncode not in (0, 255) or (returncode == 255 and not unavailable):
        raise CacheError("Incomplete cached sensor output or unexplained exit code")
    return "\n".join(rows) + "\n"


@dataclass
class SensorSample:
    """A validated collection, distinct from its truthful raw subprocess outcomes."""
    stdout: str
    result: CommandResult
    subcommands: list
    reader: dict
    accepted: bool
    elapsed_seconds: float
    error: str = None
    failure_kind: str = None

    @property
    def succeeded(self):
        return self.accepted

    @property
    def stderr(self):
        return self.result.stderr

    def summary(self, parse_failed=False):
        return self.result.summary(parse_failed)


class CachedSensorReader:
    def __init__(self, ipmi, names, directory=None, clock=time.monotonic):
        self.ipmi, self.clock = ipmi, clock
        self.required = {name.casefold() for name in names}
        self.directory = directory
        self.temporary = None
        self.path = None
        self.fingerprint = self.digest = None
        self.names = ()
        self.created = self.retry_at = 0
        self.last_fallback = None

    def close(self):
        if self.temporary:
            self.temporary.cleanup()
            self.temporary = None
        self.path = None
        self.fingerprint = self.digest = None
        self.names = ()

    def _remaining(self):
        check_cancelled(self.ipmi.stop)
        remaining = self.deadline - self.clock()
        if remaining <= 0:
            raise TimeoutError("IPMI timeout")
        return remaining

    def _run(self, operation, *args, limit=None):
        remaining = self._remaining()
        result = operation(*args, min(remaining, limit) if limit is not None else remaining)
        self.commands.append(result.summary())
        check_cancelled(self.ipmi.stop)
        self._remaining()
        return result

    def _info(self):
        result = self._run(self.ipmi.sdr_info, limit=self.ipmi.status_timeout)
        if not result.succeeded:
            raise CacheError(result.stderr or result.error)
        return repository_info(result.stdout)

    def _bytes(self):
        info = self.path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_CACHE_BYTES:
            raise CacheError("Invalid SDR cache file")
        return self.path.read_bytes()

    def _finish(self, result, text=None, *, error=None, failure_kind=None):
        accepted = text is not None and error is None
        return SensorSample(text or "", result, self.commands, self.reader, accepted,
                            self.clock() - self.started, error or (None if accepted else result.error),
                            failure_kind or (None if accepted else result.failure_kind))

    def _full(self):
        self.reader["path"] = "full"
        result = self._run(self.ipmi.sensor_table)
        return self._finish(result, result.stdout if result.succeeded else None)

    def read(self):
        self.started = self.clock()
        self.deadline = self.started + self.ipmi.timeout
        self.commands, self.reader = [], {"path": "full"}
        full_candidate = None
        after_checked = False
        try:
            before, count = self._info()
            if self.clock() < self.retry_at:
                self.reader["fallback_reason"] = self.last_fallback
                return self._full()
            warm = self.fingerprint == before and self.clock() - self.created < REFRESH_SECONDS
            if warm and hashlib.sha256(self._bytes()).digest() != self.digest:
                raise CacheError("SDR cache changed on disk")
            if warm:
                self.reader.update(path="cached", sensors=len(self.names))
                result = self._run(self.ipmi.cached_sensors, self.path, self.names)
                if (result.stderr or result.returncode not in (0, 255) or
                        result.failure_kind not in (None, "nonzero-exit")):
                    raise CacheError(result.error or result.stderr or "Cached sensor read failed")
                text = csv_table(result.stdout or result.captured_stdout, self.names, result.returncode)
                if result.returncode == 255:
                    self.commands[-1]["accepted_unavailable"] = True
                after, _ = self._info()
                if after != before:
                    raise CacheError("SDR inventory changed during sensor read")
                return self._finish(result, text)
            self.close()
            result = self._run(self.ipmi.sensor_table)
            if not result.succeeded:
                return self._finish(result)
            full_candidate = result  # Same-poll data only, never retained between read() calls.
            names, sensor_count = selected_names(result.stdout, self.required)
            self.temporary = tempfile.TemporaryDirectory(prefix="hrx-sdr-", dir=self.directory)
            self.path = Path(self.temporary.name) / "inventory.sdr"
            dump = self._run(self.ipmi.dump_sdr, self.path)
            if not dump.succeeded:
                raise CacheError(dump.error or dump.stderr)
            data = self._bytes()
            validate_dump(data, count, sensor_count)
            after_checked = True
            after, _ = self._info()
            if after != before:
                full_candidate = None
                raise CacheError("SDR inventory changed during discovery")
            self.path.chmod(0o600)
            self.names, self.fingerprint = names, before
            self.digest, self.created = hashlib.sha256(data).digest(), self.clock()
            self.reader.update(path="discovery", sensors=len(names), discovered_sensors=sensor_count)
            return self._finish(result, result.stdout)
        except (EmptyRepository, InventoryMismatch) as exc:
            self.close()
            return self._failure(str(exc), "parse")
        except TimeoutError:
            self.close()
            self.retry_at = self.clock() + RETRY_SECONDS
            self.last_fallback = "Sensor cache preparation or read timed out"
            return self._failure("IPMI timeout", "timeout")
        except (CacheError, OSError) as exc:
            self.close()
            self.retry_at = self.clock() + RETRY_SECONDS
            self.last_fallback = self.reader["fallback_reason"] = str(exc)
            try:
                if full_candidate and not after_checked:
                    try:
                        unchanged = self._info()[0] == before
                    except EmptyRepository:
                        raise
                    except CacheError:
                        unchanged = False
                    if unchanged:
                        self.reader["path"] = "full"
                        return self._finish(full_candidate, full_candidate.stdout)
                return self._full()
            except EmptyRepository as empty:
                return self._failure(str(empty), "parse")
            except TimeoutError:
                return self._failure("IPMI timeout", "timeout")

    def _failure(self, message, kind):
        result = CommandResult(("sensor-sample",), "", "", None, message,
                               self.clock() - self.started, self.ipmi.timeout, kind)
        self.commands.append(result.summary())
        return self._finish(result)
