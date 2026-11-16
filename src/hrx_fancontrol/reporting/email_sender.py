"""TLS SMTP delivery isolated from cooling, with a wall-clock deadline."""
import json
import smtplib
import ssl
import subprocess
import sys
import time
from email.message import EmailMessage
from email.utils import formatdate, make_msgid
from pathlib import Path

SEND_TIMEOUT = 30.0
RESULTS = {0: 'sent', 1: 'network', 2: 'authentication', 3: 'tls', 4: 'rejected', 5: 'failed'}
MESSAGES = {'sent': 'SMTP 已接受邮件。', 'network': 'SMTP 连接失败。',
            'authentication': 'SMTP 认证失败，请检查账号和授权码。', 'tls': 'SMTP 加密或证书验证失败。',
            'rejected': 'SMTP 拒绝接收邮件。', 'failed': '邮件发送失败。',
            'timeout': '邮件发送超时。', 'cancelled': '邮件发送已取消。'}


def deliver(settings, content):
    """Runs in the isolated child. Never return server text or credentials."""
    smtp = None
    try:
        message = EmailMessage()
        message['From'], message['To'] = settings['sender'], settings['recipient']
        message['Subject'] = content['subject']
        message['Date'], message['Message-ID'] = formatdate(localtime=True), make_msgid()
        message.set_content(content['body'])
        context = ssl.create_default_context()
        if settings['security'] == 'ssl':
            smtp = smtplib.SMTP_SSL(settings['host'], settings['port'], timeout=10,
                                    context=context, local_hostname='hrx-fancontrol')
        else:
            smtp = smtplib.SMTP(settings['host'], settings['port'], timeout=10,
                                local_hostname='hrx-fancontrol')
            smtp.ehlo()
            smtp.starttls(context=context)
            smtp.ehlo()
        smtp.login(settings['username'], settings['password'])
        refused = smtp.send_message(message)
        return 4 if refused else 0
    except smtplib.SMTPAuthenticationError:
        return 2
    except (ssl.SSLError, smtplib.SMTPNotSupportedError):
        return 3
    except (smtplib.SMTPRecipientsRefused, smtplib.SMTPSenderRefused, smtplib.SMTPDataError):
        return 4
    except (OSError, smtplib.SMTPException):
        return 1
    except Exception:
        return 5
    finally:
        # Acceptance of DATA is success even if closing the connection fails.
        if smtp is not None:
            try:
                smtp.close()
            except Exception:
                pass


def send(settings, content, cancelled=lambda: False, timeout=SEND_TIMEOUT):
    """Secrets travel only through stdin; timeout includes DNS and every SMTP step."""
    if cancelled():
        return 'cancelled'
    bootstrap = ('import sys; sys.path.insert(0, sys.argv[1]); '
                 'from hrx_fancontrol.reporting.email_sender import worker; worker()')
    process = None
    try:
        payload = json.dumps({'settings': settings, 'content': content}).encode()
        # Explicit path supports editable source and the Debian wrapper's private library.
        library = str(Path(__file__).resolve().parents[2])
        process = subprocess.Popen([sys.executable, '-I', '-B', '-c', bootstrap, library],
                                   stdin=subprocess.PIPE, stdout=subprocess.DEVNULL,
                                   stderr=subprocess.DEVNULL)
        deadline = time.monotonic() + timeout
        first = True
        while True:
            if cancelled():
                return 'cancelled'
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return 'timeout'
            try:
                process.communicate(input=payload if first else None, timeout=min(0.2, remaining))
                return RESULTS.get(process.returncode, 'failed')
            except subprocess.TimeoutExpired:
                first = False
    except (OSError, ValueError):
        return 'failed'
    finally:
        if process is not None:
            if process.poll() is None:
                process.kill()
            process.communicate()


def worker():
    try:
        data = json.load(sys.stdin)
        from .email_config import validate
        code = deliver(validate(data['settings']), data['content'])
    except Exception:
        code = 5
    raise SystemExit(code)
