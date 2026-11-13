"""Durable operator intent, separate from evidence of an actual BMC write."""
import copy
import json
import os
import tempfile
from pathlib import Path

from .calibration import read_json
from ..hardware.protocol import validate_parameter


def read_state(directory, identity):
    path = Path(directory) / "service-mode.json"
    if not path.exists():
        return None
    data = read_json(path)
    if (not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("identity") != identity or
            data.get("mode") not in ("automatic", "paused", "manual", "suspended") or
            type(data.get("operation_active")) is not bool):
        raise ValueError("Invalid service mode or different machine identity")
    if data["mode"] == "manual":
        validate_parameter(data.get("parameter"))
    elif data.get("parameter") is not None:
        raise ValueError("Only manual mode has a saved parameter")
    return data


class ServiceState:
    def __init__(self, directory, identity):
        self.path = Path(directory) / "service-mode.json"
        self.identity = identity
        self.data = read_state(directory, identity) or {
            "schema_version": 1, "identity": identity, "mode": "automatic", "parameter": None,
            "operation_active": False, "invocation_id": None}

    def set(self, mode, parameter=None, active=False):
        data = {"schema_version": 1, "identity": self.identity, "mode": mode, "parameter": parameter,
                "operation_active": active, "invocation_id": os.environ.get("INVOCATION_ID")}
        self._save(data)
        self.data = data  # Publish in memory only after durable intent exists.

    def _save(self, data):
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, prefix=".mode-", delete=False) as stream:
                name = stream.name
                json.dump(data, stream, allow_nan=False)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, self.path)
            name = None
            fd = os.open(self.path.parent, os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            if name is not None:
                os.unlink(name)

    def snapshot(self):
        return copy.deepcopy(self.data)


def recovery_allowed(directory, identity):
    data = read_state(directory, identity)
    if data is None:  # Old controller installations have only owner.json.
        return True
    if data["mode"] == "automatic":
        return True
    return (data["mode"] == "manual" and data["operation_active"] and
            bool(os.environ.get("INVOCATION_ID")) and data["invocation_id"] == os.environ["INVOCATION_ID"])
