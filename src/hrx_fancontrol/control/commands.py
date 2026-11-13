"""Bounded local command transport. This module never calls IPMI."""
import json
import os
import queue
import re
import socket
import stat
import threading
import time
from pathlib import Path

from .lifecycle import RUNTIME_DIR, private_directory
from ..hardware.protocol import validate_parameter

SOCKET_NAME = "control.sock"
MAX_MESSAGE = 16384
CLIENT_TIMEOUT = 60.0


def decimal_parameter(value):
    if not re.fullmatch(r"[0-9]{1,3}", value):
        raise ValueError("手动参数必须是十进制整数 1～100")
    return validate_parameter(int(value, 10))


def validate_command(data):
    if not isinstance(data, dict):
        raise ValueError("Expected a command object")
    action = data.get("action")
    expected = {"action", "parameter"} if action == "manual" else {"action"}
    if set(data) != expected or action not in ("on", "off", "manual", "status"):
        raise ValueError("Unknown command or fields")
    if action == "manual":
        validate_parameter(data["parameter"])
    return data


def receive(stream):
    data = bytearray()
    while b"\n" not in data:
        part = stream.recv(min(4096, MAX_MESSAGE + 1 - len(data)))
        if not part:
            raise ValueError("Incomplete command response")
        data.extend(part)
        if len(data) > MAX_MESSAGE:
            raise ValueError("Message too large")
    line, trailing = data.split(b"\n", 1)
    if trailing:
        raise ValueError("Only one message is allowed per connection")
    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate command field")
            result[key] = value
        return result
    return json.loads(line, object_pairs_hook=unique)


def send(stream, data):
    stream.sendall(json.dumps(data, ensure_ascii=False, allow_nan=False).encode() + b"\n")


class Request:
    def __init__(self, command):
        self.command, self.result = command, None
        self.done = threading.Event()
        self.deadline = time.monotonic() + CLIENT_TIMEOUT - 2

    def reply(self, result):
        self.result = result
        self.done.set()


class CommandServer:
    def __init__(self, directory):
        self.path = Path(directory) / SOCKET_NAME
        self.requests = queue.Queue(maxsize=16)
        self.wake, self.stopped = threading.Event(), threading.Event()
        self.slots = threading.BoundedSemaphore(16)
        self.stream = None
        self.workers = set()
        self.workers_lock = threading.Lock()

    def __enter__(self):
        private_directory(self.path.parent)
        if self.path.exists() or self.path.is_symlink():
            info = self.path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.geteuid():
                raise ValueError("Refusing to replace a non-owned socket path")
            self.path.unlink()  # Caller holds the process-wide WriterLock.
        self.stream = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            self.stream.bind(str(self.path))
            os.chmod(self.path, 0o600)
            self.inode = self.path.stat().st_ino
            self.stream.listen(16)
            self.stream.settimeout(.2)
            self.thread = threading.Thread(target=self._accept, daemon=True)
            self.thread.start()
            return self
        except BaseException:
            self.stream.close()
            raise

    def _accept(self):
        while not self.stopped.is_set():
            try:
                stream, _ = self.stream.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            if not self.slots.acquire(False):
                stream.close()
                continue
            worker = threading.Thread(target=self._connection, args=(stream,), daemon=True)
            with self.workers_lock:
                self.workers.add(worker)
            worker.start()

    def _connection(self, stream):
        try:
            with stream:
                stream.settimeout(2)
                try:
                    request = Request(validate_command(receive(stream)))
                    self.requests.put_nowait(request)
                    self.wake.set()
                    while not request.done.wait(.2) and not self.stopped.is_set():
                        if time.monotonic() > request.deadline:
                            break
                    result = request.result or {"ok": False, "message": "结果尚未确认，请用 status 查看；不要自动重发。"}
                except (ValueError, UnicodeError, queue.Full, OSError) as exc:
                    result = {"ok": False, "message": str(exc)}
                try:
                    send(stream, result)
                except OSError:
                    pass  # Disconnect does not undo an already accepted command.
        finally:
            self.slots.release()
            with self.workers_lock:
                self.workers.discard(threading.current_thread())

    def pending(self):
        while True:
            try:
                request = self.requests.get_nowait()
            except queue.Empty:
                self.wake.clear()
                return
            if time.monotonic() <= request.deadline:
                yield request
            else:
                request.reply({"ok": False, "message": "指令排队超时，未执行。"})

    def __exit__(self, *_):
        self.stopped.set()
        self.stream.close()
        self.thread.join(1)
        with self.workers_lock:
            workers = list(self.workers)
        for worker in workers:
            worker.join(2.5)
        if self.path.exists() and self.path.lstat().st_ino == self.inode:
            self.path.unlink()


def command_client(command, directory=RUNTIME_DIR):
    validate_command(command)
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as stream:
        stream.settimeout(CLIENT_TIMEOUT)
        try:
            stream.connect(str(Path(directory) / SOCKET_NAME))
        except OSError as exc:
            raise ValueError("无法连接控制服务，请先启动 hrx-fancontrol 服务。") from exc
        try:
            send(stream, command)
            return receive(stream)
        except (OSError, ValueError) as exc:
            raise ValueError("指令结果未知，请用 status 核对；未自动重发。") from exc
