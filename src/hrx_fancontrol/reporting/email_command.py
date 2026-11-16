"""Interactive mailbox commands never initialize the hardware controller."""
import getpass
import json
import sys
import warnings

from . import email_config
from .email_sender import MESSAGES, send


def _ask(prompt, default=None):
    suffix = f' [{default}]' if default is not None else ''
    value = input(prompt + suffix + '：').strip()
    return value or default or ''


def setup(path=None):
    host = _ask('SMTP 地址（例如 smtp.example.com）')
    username = _ask('SMTP 账号')
    with warnings.catch_warnings():
        warnings.simplefilter('error', getpass.GetPassWarning)
        password = getpass.getpass('SMTP 授权码（隐藏输入）：')
    sender = username if email_config.address(username) else _ask('发件邮箱')
    recipient = _ask('收件邮箱', sender)
    security = _ask('加密方式 ssl / starttls', 'ssl').lower()
    try:
        port = int(_ask('SMTP 端口', '587' if security == 'starttls' else '465'))
    except ValueError:
        raise email_config.EmailConfigError('SMTP 端口必须是 1～65535。') from None
    email_config.save(dict(enabled=True, host=host, username=username, password=password,
                           sender=sender, recipient=recipient, security=security, port=port), path)
    return {'message': '邮箱配置已保存并开启，服务会自动采用；可运行 email test 发送测试邮件。'}


def run(action, json_output=False, path=None):
    try:
        if action == 'setup':
            if json_output:
                raise email_config.EmailConfigError('交互配置不能使用 --json。')
            result = setup(path)
        else:
            settings = email_config.load(path)
            if action == 'status':
                result = ({'configured': False, 'enabled': False, 'message': '尚未配置邮箱，请运行 email setup。'}
                          if settings is None else
                          {'configured': True, **{key: settings[key] for key in
                           ('enabled', 'host', 'port', 'security', 'username', 'sender', 'recipient')},
                           'message': '邮箱通知已开启。' if settings['enabled'] else '邮箱通知已关闭。'})
            elif settings is None:
                raise email_config.EmailConfigError('尚未配置邮箱，请先运行 email setup。')
            elif action in ('on', 'off'):
                settings['enabled'] = action == 'on'
                email_config.save(settings, path)
                result = {'message': '邮箱通知已开启。' if settings['enabled'] else '邮箱通知已关闭，自动调控保持原状。'}
            else:
                outcome = send(settings, {'subject': 'HRX-FanControl 测试邮件',
                                         'body': '这是一封配置测试邮件，不代表服务器发生温度告警。'})
                result = {'message': MESSAGES[outcome] + (' 请检查收件箱。' if outcome == 'sent' else '')}
                if outcome != 'sent':
                    _display(result, json_output)
                    return 1
        _display(result, json_output)
        return 0
    except (EOFError, KeyboardInterrupt):
        print('邮箱配置已取消，原配置保持不变。', file=sys.stderr)
    except getpass.GetPassWarning:
        print('无法安全隐藏授权码，请在交互终端重新运行。', file=sys.stderr)
    except email_config.EmailConfigError as exc:
        print(str(exc), file=sys.stderr)  # Only fixed, locally defined validation messages.
    except (OSError, ValueError):
        print('邮箱操作失败，请检查文件权限与配置；现有风扇模式未改变。', file=sys.stderr)
    return 1


def _display(result, json_output):
    if json_output:
        print(json.dumps(result, ensure_ascii=False))
    else:
        print(result['message'])
        if result.get('configured'):
            print(f"SMTP：{result['host']}:{result['port']}（{result['security']}）")
            print(f"账号：{result['username']}；发件：{result['sender']}；收件：{result['recipient']}")
