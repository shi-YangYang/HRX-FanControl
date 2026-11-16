"""One bounded L5 episode, delivered independently of the hardware loop."""
import socket
import sys
import threading
import time

from .email_config import load
from .email_sender import MESSAGES, send

RETRY_SECONDS = 60.0
RELOAD_SECONDS = 5.0
MAX_ATTEMPTS = 3


def content_for(report):
    lines = ['温度策略已建议 L5，请检查服务器温度、负载与通风。',
             f"主机：{socket.gethostname()}", f"采样时间：{report.get('timestamp', '未知')}",
             '建议档位：L5（不代表风扇设置已执行或验证）', '', '温度：']
    for name, sensor in report.get('sensors', {}).items():
        if sensor.get('celsius') is not None:
            lines.append(f"{name}: {sensor['celsius']} °C")
    lines.extend(['', '风扇转速：'])
    for name, fan in report.get('fans', {}).items():
        lines.append(f"{name}: {fan.get('rpm') if fan.get('rpm') is not None else '未知'} RPM")
    return {'subject': 'HRX-FanControl 温度告警：建议档位 L5', 'body': '\n'.join(lines)}


def notice(message):
    try:
        print('邮箱通知：' + message, file=sys.stderr, flush=True)
    except OSError:
        pass


class EmailAlerts:
    def __init__(self, loader=load, sender=send, clock=time.monotonic, output=notice):
        self.loader, self.sender, self.clock, self.output = loader, sender, clock, output
        self.condition = threading.Condition()
        self.stopping = False
        self.episode = self.sent = False
        self.content = None
        self.generation = self.attempts = 0
        self.due = 0.0
        self.config_error = False
        self.thread = threading.Thread(target=self._work, name='hrx-email', daemon=True)
        self.thread.start()

    def observe(self, report):
        level = report.get('recommended_level')
        valid = report.get('collection', {}).get('sensor_data_valid', False)
        with self.condition:
            if self.stopping:
                return
            if level == 5:
                if not self.episode:
                    self.episode, self.sent = True, False
                    self.generation += 1
                    self.attempts, self.due = 0, 0.0
                if self.content is None and not self.sent:
                    self.content = content_for(report)
            elif valid and level is not None and level < 5:
                self.episode, self.sent = False, False
                self.content = None
                self.generation += 1
                self.attempts, self.due = 0, 0.0
            self.condition.notify_all()

    def pause(self):
        """No pending retries while sampling/control is off; keep successful dedup."""
        with self.condition:
            self.content = None
            self.generation += 1
            self.condition.notify_all()

    def _cancelled(self, generation):
        with self.condition:
            return self.stopping or generation != self.generation

    def _work(self):
        previous_settings = None
        while True:
            with self.condition:
                if self.stopping:
                    return
            try:
                settings = self.loader()
                self.config_error = False
            except (OSError, ValueError):
                settings = None
                if not self.config_error:
                    self.output('配置无法读取或权限不安全；本次不发送，请运行 email setup 检查。')
                    self.config_error = True
            with self.condition:
                if self.stopping:
                    return
                if settings != previous_settings:
                    # Reconfiguration can repair an unsent episode; delivered ones stay deduplicated.
                    self.attempts, self.due = 0, 0.0
                    previous_settings = settings
                ready = (settings and settings['enabled'] and self.content is not None and
                         not self.sent and self.attempts < MAX_ATTEMPTS and self.clock() >= self.due)
                if not ready:
                    self.condition.wait(RELOAD_SECONDS)
                    continue
                generation, content = self.generation, self.content
                self.attempts += 1
            try:
                next_config_check = self.clock()

                def cancelled():
                    nonlocal next_config_check
                    if self._cancelled(generation):
                        return True
                    if self.clock() >= next_config_check:
                        next_config_check = self.clock() + 1.0
                        try:
                            return self.loader() != settings
                        except (OSError, ValueError):
                            return True
                    return False

                result = self.sender(settings, content, cancelled)
            except Exception:
                result = 'failed'  # No exception text may cross the credential boundary.
            with self.condition:
                if self.stopping:
                    return
                if generation != self.generation:
                    continue
                self.sent = result == 'sent'
                self.due = self.clock() + RETRY_SECONDS
            if result != 'cancelled':
                self.output(MESSAGES.get(result, MESSAGES['failed']))

    def close(self):
        with self.condition:
            self.stopping = True
            self.condition.notify_all()
        self.thread.join(timeout=1.0)
