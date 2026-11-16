"""Email configuration and notification contracts, with no external SMTP or IPMI."""
import copy
import getpass
import io
import json
import os
from pathlib import Path
import smtplib
import ssl
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import Mock, patch

from hrx_fancontrol.main import main
from hrx_fancontrol.reporting import email_alerts, email_command, email_config, email_sender

SECRET = 'test-authorization-code'


def settings():
    return dict(enabled=True, host='smtp.example.com', port=465, security='ssl',
                username='sender@example.com', password=SECRET,
                sender='sender@example.com', recipient='receiver@example.com')


def report(level=5, valid=True):
    return dict(timestamp='2026-11-16T01:00:00+00:00', recommended_level=level,
                collection={'sensor_data_valid': valid},
                sensors={'PCH_Temp': {'celsius': 80, 'requested_level': 5}},
                fans={'FAN1_R_Speed': {'rpm': 3680}})


class ConfigurationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / 'etc' / 'email.json'

    def test_private_atomic_save_and_rejected_replacement_preserve_old(self):
        email_config.save(settings(), self.path)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(email_config.load(self.path), settings())
        original = self.path.read_bytes()
        with patch.object(email_config.os, 'replace', side_effect=OSError('no space')):
            with self.assertRaises(OSError):
                email_config.save({**settings(), 'enabled': False}, self.path)
        self.assertEqual(self.path.read_bytes(), original)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])
        for bad in ({**settings(), 'port': 0}, {**settings(), 'security': 'plain'},
                    {**settings(), 'sender': 'x\nBcc:x@host'}, {**settings(), 'host': 'smtp://host'},
                    {**settings(), 'recipient': 'a@b,c@d'}):
            with self.assertRaises(email_config.EmailConfigError):
                email_config.save(bad, self.path)
        self.assertEqual(self.path.read_bytes(), original)

    def test_unsafe_file_directory_and_link_rejected(self):
        email_config.save(settings(), self.path)
        for permissions in (0o644, 0o660):
            self.path.chmod(permissions)
            with self.assertRaises(email_config.EmailConfigError):
                email_config.load(self.path)
        self.path.chmod(0o600)
        target = self.path.with_name('target')
        self.path.rename(target)
        self.path.symlink_to(target)
        for operation in (lambda: email_config.load(self.path), lambda: email_config.save(settings(), self.path)):
            with self.assertRaises((OSError, email_config.EmailConfigError)):
                operation()
        self.assertEqual(json.loads(target.read_text()), settings())
        self.path.unlink()
        os.mkfifo(self.path, 0o600)
        with self.assertRaises(email_config.EmailConfigError):
            email_config.load(self.path)
        self.path.unlink()
        self.path.parent.chmod(0o777)
        with self.assertRaises(email_config.EmailConfigError):
            email_config.save(settings(), self.path)

    def test_setup_order_defaults_and_no_automatic_test(self):
        prompts = []
        answers = iter(['smtp.example.com', 'sender@example.com', '', '', ''])
        def read(prompt):
            prompts.append(prompt)
            return next(answers)
        def password(prompt):
            prompts.append(prompt)
            return SECRET
        with patch('builtins.input', side_effect=read), patch.object(getpass, 'getpass', side_effect=password), \
                patch.object(email_command, 'send') as send, patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(email_command.run('setup', path=self.path), 0)
        self.assertIn('SMTP 地址', prompts[0])
        self.assertIn('SMTP 账号', prompts[1])
        self.assertIn('授权码', prompts[2])
        data = email_config.load(self.path)
        self.assertEqual((data['port'], data['security'], data['recipient']), (465, 'ssl', data['sender']))
        self.assertNotIn(SECRET, output.getvalue())
        send.assert_not_called()

    def test_cancel_and_unsafe_getpass_keep_original(self):
        email_config.save(settings(), self.path)
        old = self.path.read_bytes()
        for error in (KeyboardInterrupt(), EOFError(), getpass.GetPassWarning('unsafe')):
            with patch('builtins.input', side_effect=['smtp.example.com', 'sender@example.com']), \
                    patch.object(getpass, 'getpass', side_effect=error), patch('sys.stderr', new_callable=io.StringIO):
                self.assertEqual(email_command.run('setup', path=self.path), 1)
            self.assertEqual(self.path.read_bytes(), old)

    def test_cli_only_configuration_commands_no_hardware_or_credentials(self):
        email_config.save(settings(), self.path)
        with patch.object(email_config, 'CONFIG_PATH', self.path), \
                patch('hrx_fancontrol.main.ReadOnlyIPMI') as ipmi, \
                patch('hrx_fancontrol.control.commands.command_client') as client, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(main(['--json', 'email', 'status']), 0)
            self.assertTrue(json.loads(output.getvalue())['enabled'])
            self.assertNotIn(SECRET, output.getvalue())
            self.assertEqual(main(['email', 'off']), 0)
            self.assertFalse(email_config.load(self.path)['enabled'])
            self.assertEqual(main(['email', 'on']), 0)
            self.assertTrue(email_config.load(self.path)['enabled'])
            ipmi.assert_not_called()
            client.assert_not_called()
        with patch('sys.stderr', new_callable=io.StringIO), self.assertRaises(SystemExit):
            main(['--serve', 'email', 'test'])

    def test_explicit_test_and_redacted_configuration_failure(self):
        email_config.save(settings(), self.path)
        with patch.object(email_command, 'send', return_value='sent') as send, \
                patch('sys.stdout', new_callable=io.StringIO) as output:
            self.assertEqual(email_command.run('test', path=self.path), 0)
            self.assertIn('测试邮件', send.call_args.args[1]['subject'])
            self.assertNotIn(SECRET, output.getvalue())
        self.path.write_text('{"password":"' + SECRET)
        with patch('sys.stderr', new_callable=io.StringIO) as errors:
            self.assertEqual(email_command.run('status', path=self.path), 1)
        self.assertNotIn(SECRET, errors.getvalue())


class SenderTests(unittest.TestCase):
    def test_ssl_and_starttls_authenticate_after_verified_tls(self):
        for security in ('ssl', 'starttls'):
            with self.subTest(security=security):
                smtp = Mock()
                smtp.send_message.return_value = {}
                with patch.object(smtplib, 'SMTP_SSL', return_value=smtp) as implicit, \
                        patch.object(smtplib, 'SMTP', return_value=smtp) as explicit:
                    self.assertEqual(email_sender.deliver({**settings(), 'security': security},
                                                         email_alerts.content_for(report())), 0)
                if security == 'ssl':
                    context = implicit.call_args.kwargs['context']
                    explicit.assert_not_called()
                else:
                    context = smtp.starttls.call_args.kwargs['context']
                    self.assertLess([c[0] for c in smtp.method_calls].index('starttls'),
                                    [c[0] for c in smtp.method_calls].index('login'))
                    implicit.assert_not_called()
                self.assertTrue(context.check_hostname)
                self.assertEqual(context.verify_mode, ssl.CERT_REQUIRED)
                smtp.login.assert_called_once_with(settings()['username'], SECRET)
                message = smtp.send_message.call_args.args[0]
                self.assertIn('80 °C', message.get_content())
                self.assertIn('3680 RPM', message.get_content())
                self.assertNotIn(SECRET, str(message))
                smtp.close.assert_called_once()

    def test_smtp_errors_never_escape_and_quit_failure_does_not_resend(self):
        for error, code in ((smtplib.SMTPAuthenticationError(535, SECRET.encode()), 2),
                            (ssl.SSLError(SECRET), 3), (smtplib.SMTPDataError(550, SECRET.encode()), 4)):
            smtp = Mock()
            smtp.login.side_effect = error
            with patch.object(smtplib, 'SMTP_SSL', return_value=smtp):
                self.assertEqual(email_sender.deliver(settings(), email_alerts.content_for(report())), code)
        smtp = Mock()
        smtp.send_message.return_value = {}
        smtp.close.side_effect = OSError(SECRET)
        with patch.object(smtplib, 'SMTP_SSL', return_value=smtp):
            self.assertEqual(email_sender.deliver(settings(), email_alerts.content_for(report())), 0)

    def test_process_deadline_cancellation_and_private_stdin(self):
        process = Mock(returncode=0)
        process.communicate.side_effect = [subprocess.TimeoutExpired('smtp', .1), (None, None), (None, None)]
        process.poll.return_value = 0
        with patch.object(email_sender.subprocess, 'Popen', return_value=process) as popen:
            self.assertEqual(email_sender.send(settings(), {'subject': 'test', 'body': 'test'}), 'sent')
        self.assertNotIn(SECRET, repr(popen.call_args))
        self.assertIn(SECRET.encode(), process.communicate.call_args_list[0].kwargs['input'])
        self.assertIsNone(process.communicate.call_args_list[1].kwargs['input'])
        process = Mock()
        process.poll.return_value = None
        with patch.object(email_sender.subprocess, 'Popen', return_value=process):
            self.assertEqual(email_sender.send(settings(), {}, timeout=0), 'timeout')
        process.kill.assert_called_once()
        self.assertEqual(email_sender.send(settings(), {}, cancelled=lambda: True), 'cancelled')

    def test_child_imports_private_library_without_network(self):
        # The real isolated child rejects invalid settings before creating SMTP.
        with patch.dict(os.environ, {'PYTHONPATH': ''}):
            self.assertEqual(email_sender.send({}, {}, timeout=3), 'failed')


class NotificationTests(unittest.TestCase):
    def wait_for(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate():
            if time.monotonic() >= deadline:
                self.fail('Notification worker did not make expected progress')
            time.sleep(.005)

    def notifier(self, loader=None, sender=None, clock=time.monotonic):
        self.output = []
        notify = email_alerts.EmailAlerts(loader=loader or (lambda: settings()),
                                         sender=sender or (lambda *_: 'sent'), clock=clock, output=self.output.append)
        self.addCleanup(notify.close)
        return notify

    def test_l5_dedup_unknown_does_not_rearm_and_recovery_rearms(self):
        sender = Mock(return_value='sent')
        notify = self.notifier(sender=sender)
        notify.observe(report())
        self.wait_for(lambda: notify.sent)
        for item in (report(), report(None, False), report(3, False), report()):
            notify.observe(item)
        self.assertEqual(sender.call_count, 1)
        notify.observe(report(4))
        notify.observe(report())
        self.wait_for(lambda: sender.call_count == 2)
        self.assertIn('PCH_Temp', sender.call_args.args[1]['body'])

    def test_retry_limit_interval_pause_and_off_cancel_pending(self):
        now = [0]
        config = [settings()]
        sender = Mock(return_value='network')
        notify = self.notifier(loader=lambda: copy.deepcopy(config[0]), sender=sender, clock=lambda: now[0])
        notify.observe(report())
        self.wait_for(lambda: notify.due == 60)
        now[0] = 59
        notify.observe(report())
        self.assertEqual(sender.call_count, 1)
        now[0] = 60
        notify.observe(report())
        self.wait_for(lambda: notify.due == 120)
        now[0] = 120
        notify.observe(report())
        self.wait_for(lambda: notify.attempts == 3)
        now[0] = 1000
        notify.observe(report())
        self.assertEqual(sender.call_count, 3)
        notify.observe(report(1))
        config[0]['enabled'] = False
        notify.observe(report())
        # Both configuration changes and off/pause are consumed before a later high sample.
        notify.pause()
        self.assertIsNone(notify.content)
        self.assertEqual(sender.call_count, 3)

    def test_hot_configuration_and_invalid_config_dont_escape_worker(self):
        config = [None]
        sender = Mock(return_value='sent')
        def load():
            if isinstance(config[0], Exception):
                raise config[0]
            return config[0]
        notify = self.notifier(loader=load, sender=sender)
        notify.observe(report())
        config[0] = ValueError(SECRET)
        notify.observe(report())
        self.wait_for(lambda: notify.config_error)
        self.assertNotIn(SECRET, str(self.output))
        notify.observe(report(1))  # A recovered episode must not return on setup.
        config[0] = settings()
        notify.observe(report(1))
        notify.observe(report())
        self.wait_for(lambda: notify.sent)
        self.assertEqual(sender.call_count, 1)

    def test_blocked_send_does_not_block_observation_pause_or_stop(self):
        entered = threading.Event()
        cancelled = threading.Event()
        def slow(_, content, stop):
            entered.set()
            while not stop():
                time.sleep(.005)
            cancelled.set()
            return 'cancelled'
        notify = self.notifier(sender=slow)
        notify.observe(report())
        self.assertTrue(entered.wait(1))
        notify.observe(report(1))
        self.assertTrue(cancelled.wait(1))
        notify.pause()
        notify.close()
        self.assertFalse(notify.thread.is_alive())

    def test_disabling_configuration_cancels_inflight_send(self):
        entered, cancelled = threading.Event(), threading.Event()
        current = [settings()]
        now = [0.0]
        def slow(_, content, stop):
            entered.set()
            while not stop():
                time.sleep(.005)
            cancelled.set()
            return 'cancelled'
        notify = self.notifier(loader=lambda: copy.deepcopy(current[0]), sender=slow, clock=lambda: now[0])
        notify.observe(report())
        self.assertTrue(entered.wait(1))
        current[0]['enabled'] = False
        now[0] = 2
        self.assertTrue(cancelled.wait(1))
        self.assertFalse(notify.sent)


class RuntimeEmailTests(unittest.TestCase):
    def test_only_service_automatic_reports_reach_notifier_and_off_cancels(self):
        from argparse import Namespace
        from contextlib import ExitStack
        from hrx_fancontrol.config import load_config
        from hrx_fancontrol.control.control_runtime import run_control
        from hrx_fancontrol.control.lifecycle import WriterLock
        from test_controlled_execution import IDENTITY, SAMPLE
        from hrx_fancontrol.hardware.collection import collect
        from hrx_fancontrol.policy.thermal_engine import ThermalEngine
        config = load_config()
        config['logging']['enabled'] = False
        notifier = Mock()
        data = collect(None, ThermalEngine(config), config, source=SAMPLE)
        data['recommended_level'] = 5
        with tempfile.TemporaryDirectory(prefix='hrx-', dir='/tmp') as tmp, ExitStack() as stack:
            ipmi = Mock()
            stack.enter_context(patch('hrx_fancontrol.control.control_runtime.machine_identity', return_value=IDENTITY))
            stack.enter_context(patch('hrx_fancontrol.control.control_runtime.ControlIPMI', return_value=ipmi))
            stack.enter_context(patch('hrx_fancontrol.control.control_runtime.WriterLock', side_effect=lambda: WriterLock(Path(tmp) / 'run')))
            stack.enter_context(patch('hrx_fancontrol.control.control_runtime.control_state_directory', return_value=Path(tmp) / 'run'))
            stack.enter_context(patch.object(email_alerts, 'EmailAlerts', return_value=notifier))
            stack.enter_context(patch('sys.stdout', new_callable=io.StringIO))
            stack.enter_context(patch('sys.stderr', new_callable=io.StringIO))
            def collect(*args, **kwargs):
                if kwargs['on_alert']:
                    kwargs['on_alert'](data)
                return data
            stack.enter_context(patch('hrx_fancontrol.control.control_runtime.collect', side_effect=collect))
            def service(controller, server):
                controller.collect_next(controller.engine, True)
                self.assertEqual(notifier.observe.call_count, 1)
                controller.publish(data, 'automatic')
                self.assertEqual(notifier.observe.call_count, 2)  # Worker deduplicates these two paths.
                controller.publish(data, 'manual')
                self.assertEqual(notifier.observe.call_count, 2)
                controller.apply({'action': 'off'})
                controller.executor.emit('mode_changed', controller.executor.snapshot())
                self.assertTrue(notifier.pause.called)
                controller.collect_next(controller.engine, False)
                self.assertEqual(notifier.observe.call_count, 2)
            stack.enter_context(patch('hrx_fancontrol.control.service.ServiceController.run', service))
            args = Namespace(calibrate=None, auto=True, service_recover=False, profile=None, json=True)
            self.assertEqual(run_control(args, config), 0)
        notifier.close.assert_called_once()
        ipmi.set_level.assert_not_called()
        ipmi.set_parameter.assert_not_called()
