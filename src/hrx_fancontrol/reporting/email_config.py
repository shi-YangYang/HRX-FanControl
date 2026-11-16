"""Private SMTP settings, separate from the shared controller configuration."""
import ipaddress
import json
import os
import re
import stat
import tempfile
from pathlib import Path

CONFIG_PATH = Path('/etc/hrx-fancontrol/email.json')
MAX_CONFIG_BYTES = 16384


class EmailConfigError(ValueError):
    pass


def address(value):
    return isinstance(value, str) and len(value) <= 254 and bool(re.fullmatch(
        r"[A-Za-z0-9.!#$%&'*+/=?^_`{|}~-]+@[A-Za-z0-9](?:[A-Za-z0-9.-]*[A-Za-z0-9])?", value))


def validate(data):
    keys = {'enabled', 'host', 'port', 'security', 'username', 'password', 'sender', 'recipient'}
    if not isinstance(data, dict) or set(data) != keys:
        raise EmailConfigError('邮箱配置格式无效，请重新运行 email setup。')
    for key in ('host', 'username', 'password', 'sender', 'recipient'):
        value = data[key]
        if not isinstance(value, str) or not value or len(value) > 1024 or any(ord(c) < 32 or ord(c) == 127 for c in value):
            raise EmailConfigError('邮箱配置含有空值、控制字符或过长字段。')
    host = data['host']
    try:
        ipaddress.ip_address(host)
    except ValueError:
        if len(host) > 253 or not all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', p)
                                      for p in host.split('.')):
            raise EmailConfigError('SMTP 地址应为主机名或 IP，端口请单独填写。') from None
    if not address(data['sender']) or not address(data['recipient']):
        raise EmailConfigError('发件与收件地址必须是单个有效邮箱地址。')
    if type(data['enabled']) is not bool or data['security'] not in ('ssl', 'starttls'):
        raise EmailConfigError('邮箱开关或 TLS 方式无效。')
    if type(data['port']) is not int or not 1 <= data['port'] <= 65535:
        raise EmailConfigError('SMTP 端口必须是 1～65535。')
    return data


def _directory(path, create=False):
    if create:
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    info = path.parent.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise EmailConfigError('邮箱配置目录必须属于当前用户，且不可由其他用户写入。')


def _private(info):
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.geteuid() or
            stat.S_IMODE(info.st_mode) != 0o600 or info.st_nlink != 1):
        raise EmailConfigError('邮箱配置必须为当前用户拥有的独立普通文件，权限为 0600。')


def load(path=None):
    path = Path(path) if path is not None else CONFIG_PATH
    try:
        _directory(path)
    except FileNotFoundError:
        return None
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    with os.fdopen(fd, 'rb') as stream:
        _private(os.fstat(stream.fileno()))
        raw = stream.read(MAX_CONFIG_BYTES + 1)
    if len(raw) > MAX_CONFIG_BYTES:
        raise EmailConfigError('邮箱配置过大。')
    try:
        data = json.loads(raw)
    except (ValueError, UnicodeError):
        raise EmailConfigError('邮箱配置无法解析，请重新运行 email setup。') from None
    return validate(data)


def save(data, path=None):
    data = validate(data)
    path = Path(path) if path is not None else CONFIG_PATH
    _directory(path, create=True)
    try:
        _private(path.lstat())
    except FileNotFoundError:
        pass
    fd, temporary = tempfile.mkstemp(prefix='.email-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as stream:
            os.fchmod(stream.fileno(), 0o600)
            json.dump(data, stream, ensure_ascii=False)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
