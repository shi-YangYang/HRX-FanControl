"""One local writer, durable ownership, signal cancellation, and systemd progress."""
import hashlib
import json
import os
import signal
import socket
import stat
import tempfile
from pathlib import Path

from .calibration import read_json

RUNTIME_DIR = Path("/run/hrx-fancontrol")
STATE_DIR = Path("/var/lib/hrx-fancontrol")
OVERRIDE_EXIT_STATUS = 79


class ControlSuspended(ValueError):
    """Explicit resumption is required after yielding to an external setting."""


def private_directory(directory):
    directory = Path(directory)
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = directory.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise ValueError("Control directory must be owned by this user and not writable by others")
    return directory


def control_state_directory():
    return private_directory(STATE_DIR)


def profile_digest(profile):
    return hashlib.sha256(json.dumps(profile, sort_keys=True, allow_nan=False).encode()).hexdigest()


class WriterLock:
    def __init__(self, directory=RUNTIME_DIR):
        self.directory, self.fd = Path(directory), None

    def __enter__(self):
        import fcntl
        private_directory(self.directory)
        fd = os.open(self.directory / "writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            raise ValueError("Another fan controller is already running")
        self.fd = fd
        return self

    def __exit__(self, *_):
        os.close(self.fd)
        self.fd = None
        # Never unlink the lock inode: waiting contenders must share the same inode.


class Ownership:
    def __init__(self, directory, identity, profile, run_id):
        self.path = Path(directory) / "owner.json"
        self.data = {"schema_version": 1, "identity": identity, "run_id": run_id,
                     "invocation_id": os.environ.get("INVOCATION_ID"),
                     "profile_digest": profile_digest(profile) if profile else None,
                     "active": True, "fallback_attempted": False, "last_target": None,
                     "confirmed_target": None, "yielded": False}
        self.active = False

    def _save(self):
        name = None
        try:
            with tempfile.NamedTemporaryFile(mode="w", dir=self.path.parent, prefix=".owner-", delete=False) as stream:
                name = stream.name
                json.dump(self.data, stream, allow_nan=False)
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

    def before_write(self, level, fallback=False):
        if self.data.get("yielded"):
            raise ControlSuspended("Control yielded to an external setting")
        if fallback and self.data["fallback_attempted"]:
            raise ValueError("Fallback was already attempted")
        self.data.update(last_target=level, confirmed_target=None,
                         fallback_attempted=self.data["fallback_attempted"] or fallback)
        self._save()
        self.active = True

    def confirm(self, level):
        self.data.update(last_target=level, confirmed_target=level)
        self._save()
        self.active = True

    def relinquish(self):
        self.data.update(active=False, yielded=True)
        self.active = False
        self._save()

    def check_start(self, resume=False):
        if not self.path.exists():
            return
        previous = read_json(self.path)
        if not isinstance(previous, dict) or previous.get("schema_version") != 1:
            raise ValueError("Invalid existing control ownership record")
        if resume:
            if previous.get("identity") != self.data["identity"]:
                raise ValueError("Cannot resume a different machine or firmware's ownership record")
            # Explicit intent also supersedes an active record left behind when
            # persisting a detected external override failed.
            self.data["active"] = False
            self._save()
            self.data["active"] = True
            return
        if previous.get("yielded") is not True:
            # An interrupted stop-post may have observed a change but failed to
            # persist it. Retain the old confirmed baseline for the next startup.
            confirmed = previous.get("confirmed_target")
            if (previous.get("active") is True and previous.get("identity") == self.data["identity"] and
                    type(confirmed) is int and confirmed in range(1, 6) and
                    confirmed == previous.get("last_target")):
                return confirmed
            return
        raise ControlSuspended("检测到此前已让位，使用 --resume-control 主动重新启用。")

    def finish(self):
        if self.active:
            self.data["active"] = False
            self._save()
            self.active = False

    @classmethod
    def recover(cls, directory, identity, profile):
        if os.environ.get("EXIT_CODE") == "exited" and os.environ.get("EXIT_STATUS") in ("78", str(OVERRIDE_EXIT_STATUS)):
            return None
        invocation = os.environ.get("INVOCATION_ID")
        if not invocation or os.environ.get("SERVICE_RESULT") not in (
                "exit-code", "signal", "core-dump", "watchdog", "timeout", "oom-kill"):
            return None
        path = Path(directory) / "owner.json"
        if not path.exists():
            return None
        data = read_json(path)
        if (not isinstance(data, dict) or data.get("schema_version") != 1 or data.get("active") is not True or
                data.get("yielded") is True or
                data.get("fallback_attempted") is not False or data.get("invocation_id") != invocation or
                data.get("identity") != identity or data.get("profile_digest") != profile_digest(profile) or
                type(data.get("last_target")) is not int or data["last_target"] not in range(1, 6)):
            return None
        confirmed = data.get("confirmed_target")
        if confirmed is not None and (type(confirmed) is not int or confirmed != data["last_target"]):
            raise ValueError("Invalid confirmed control target")
        owner = cls(directory, identity, profile, data.get("run_id"))
        owner.data, owner.active = data, True
        return owner


class StopSignals:
    def __init__(self, stop):
        self.stop, self.handlers = stop, {}

    def __enter__(self):
        for sig in (signal.SIGINT, signal.SIGTERM):
            self.handlers[sig] = signal.signal(sig, lambda *_: self.stop.set())
        return self

    def __exit__(self, *_):
        for sig, handler in self.handlers.items():
            signal.signal(sig, handler)


def notify(message):
    address = os.environ.get("NOTIFY_SOCKET")
    if not address:
        return
    if address.startswith("@"):
        address = "\0" + address[1:]
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as stream:
            stream.connect(address)
            stream.sendall(message.encode())
    except OSError:
        # Logging/control progress still works outside systemd or after its socket closes.
        pass
