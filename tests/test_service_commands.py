"""Command service integration with temporary sockets/files and fake BMC only."""
import io
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from hrx_fancontrol.commands import (CommandServer, Request, command_client, decimal_parameter,
                                    receive, send, validate_command, MAX_MESSAGE)
from hrx_fancontrol.executor import Executor, ExternalOverride, ExecutionFailure, OwnershipUnverified
from hrx_fancontrol.lifecycle import Ownership
from hrx_fancontrol.protocol import ControlIPMI, matches_parameter
from hrx_fancontrol.service import ServiceController
from hrx_fancontrol.service_state import ServiceState, recovery_allowed
from hrx_fancontrol.config import load_config
from hrx_fancontrol.main import main
from test_controlled_execution import IDENTITY, profile, report, raw, success, status_result


def parameter_raw(value):
    return ["0x20"] + [f"0x{value:02x}"] * 6 + ["0x00", "0x00"]


class Queue:
    def __init__(self):
        self.items = []
    def put(self, action, parameter=None):
        command = {"action": action}
        if parameter is not None:
            command["parameter"] = parameter
        request = Request(command)
        self.items.append(request)
        return request
    def pending(self):
        items, self.items = self.items, []
        yield from items


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.now = 100.0
        self.config = load_config()
        self.ipmi = Mock()
        self.ipmi.set_parameter.return_value = success()
        self.ipmi.set_level.return_value = success()
        self.stop = threading.Event()
        self.owner = Ownership(self.root, IDENTITY, profile(), "new")
        self.events = []
        self.executor = Executor(self.ipmi, profile(), self.owner,
                                 lambda event, value: self.events.append((event, value)), self.stop, lambda: self.now)
        self.collect = Mock(return_value=report(1, self.now, 1))
        self.publish = Mock()
        self.controller = ServiceController(self.executor, self.config, self.root, self.collect, self.publish, lambda: self.now)
        self.queue = Queue()

    def test_decimal_protocol_and_bounds(self):
        ipmi = ControlIPMI()
        with patch.object(ipmi, "_execute", return_value=success()) as execute:
            for parameter in (1, 20, 25, 60, 100):
                ipmi.set_parameter(parameter)
                self.assertEqual(execute.call_args.args[0][-1], f"0x{parameter:02x}")
                self.assertEqual(execute.call_args.args[0][4:8], ("0x2e", "0x30", "0x00", "0x00"))
                self.assertTrue(matches_parameter(parameter_raw(parameter), parameter))
        for text in ("0", "101", "-1", "1.5", "0x14", "True", " 20", "20 "):
            with self.assertRaises(ValueError):
                decimal_parameter(text)
        self.assertEqual(decimal_parameter("025"), 25)
        for data in ({"action": "manual", "parameter": True}, {"action": "manual", "parameter": "25"},
                     {"action": "manual", "parameter": 101}, {"action": "on", "extra": 1}, {"action": "raw"}):
            before = self.controller.state.path.read_bytes()
            with self.assertRaises(ValueError):
                self.controller.apply(data)
            self.assertEqual(self.controller.state.path.read_bytes(), before)
        self.ipmi.set_parameter.assert_not_called()

    def test_off_during_poll_wins_before_old_auto_write_and_idle_has_no_poll(self):
        request = None
        def during_poll(*_):
            nonlocal request
            request = self.queue.put("off")
            return report(1, self.now, recommended=5)
        self.collect.side_effect = during_poll
        self.controller.tick(self.queue)
        self.assertTrue(request.result["ok"])
        self.assertEqual(self.controller.state.data["mode"], "paused")
        self.ipmi.set_level.assert_not_called()
        self.now += 1000
        self.controller.tick(self.queue)
        self.assertEqual(self.collect.call_count, 1)
        self.assertFalse(recovery_allowed(self.root, IDENTITY))

    def test_sample_age_includes_slow_read_and_grows_while_paused(self):
        def slow_poll(*_):
            sample = report(1, self.now, recommended=1)
            self.now += 12
            return sample
        self.collect.side_effect = slow_poll
        self.controller.tick(self.queue)
        self.assertEqual(self.controller.snapshot()["sample_age_seconds"], 12)

        self.queue.put("off")
        self.controller.tick(self.queue)
        self.now = 130
        request = self.queue.put("status")
        self.controller.tick(self.queue)
        state = request.result["status"]
        self.assertTrue(request.result["ok"])
        self.assertFalse(state["automatic"])
        self.assertTrue(state["readings_are_cached"])
        self.assertEqual(state["sample_age_seconds"], 30)
        self.collect.assert_called_once()
        self.ipmi.set_level.assert_not_called()
        self.ipmi.set_parameter.assert_not_called()

    def test_manual_during_poll_is_fixed_and_unknown_rpm_not_verified(self):
        def during_poll(*_):
            self.queue.put("manual", 25)
            return report(1, self.now, recommended=5)
        self.collect.side_effect = during_poll
        self.controller.tick(self.queue)
        self.ipmi.set_parameter.assert_called_once_with(25)
        self.ipmi.set_level.assert_not_called()
        self.assertTrue(self.controller.manual_pending)
        self.assertFalse(self.controller.snapshot()["automatic"])
        after = report(1, self.now + 1, recommended=5)
        after["bmc"]["raw_bytes"] = parameter_raw(25)
        self.now += 1
        self.collect.side_effect = None
        self.collect.return_value = after
        self.controller.tick(self.queue)
        state = self.controller.snapshot()
        self.assertTrue(state["control"]["bmc_confirmed"])
        self.assertFalse(state["control"]["verified"])
        self.assertFalse(state["control"]["rpm_calibrated"])
        self.assertFalse(self.controller.manual_pending)
        self.assertFalse(self.owner.active)
        self.assertFalse(self.controller.allow_fallback)
        self.now += 600
        self.controller.tick(self.queue)
        self.assertEqual(self.collect.call_count, 2)
        self.assertTrue(state["readings_are_cached"])

    def test_known_manual_parameter_uses_five_level_rpm_verification_without_curve(self):
        self.controller.apply({"action": "manual", "parameter": 20})
        for moment in (110, 125, 140):
            self.now = moment
            self.collect.return_value = report(2, moment, recommended=5)
            self.controller.tick(self.queue)
        self.assertTrue(self.executor.snapshot()["verified"])
        self.assertFalse(self.controller.manual_pending)
        self.ipmi.set_parameter.assert_called_once_with(20)
        self.ipmi.set_level.assert_not_called()

    def test_off_cancels_pending_manual_verification_and_disarms_recovery(self):
        with patch.dict(os.environ, {"INVOCATION_ID": "one", "SERVICE_RESULT": "signal"}):
            self.owner.data["invocation_id"] = "one"  # Created inside the service invocation in production.
            self.controller.apply({"action": "manual", "parameter": 25})
            self.assertTrue(json.loads(self.owner.path.read_text())["active"])
            self.assertIsNotNone(Ownership.recover(self.root, IDENTITY, profile()))
            self.controller.apply({"action": "off"})
            self.assertIsNone(Ownership.recover(self.root, IDENTITY, profile()))
            self.now += 300
            self.controller.tick(self.queue)
            self.collect.assert_not_called()
            self.assertFalse(self.controller.allow_fallback)

    def test_manual_failure_stays_off_and_is_eligible_for_one_l3(self):
        from hrx_fancontrol.ipmi import CommandResult
        self.ipmi.set_parameter.return_value = CommandResult(("write",), "", "", 1, "failed", .1)
        with self.assertRaises(ExecutionFailure):
            self.controller.apply({"action": "manual", "parameter": 25})
        self.assertEqual(self.controller.state.data["mode"], "manual")
        self.assertTrue(self.controller.allow_fallback)
        self.assertTrue(self.owner.active)
        self.executor.request(3, fallback=True)
        self.ipmi.set_level.assert_called_once_with(3)
        with self.assertRaises(ValueError):
            self.executor.request(3, fallback=True)

    def test_persistence_failure_prevents_manual_write_and_auto_resumption(self):
        with patch.object(self.controller.state, "_save", side_effect=OSError("disk full")):
            with self.assertRaises(OSError):
                self.controller.apply({"action": "manual", "parameter": 25})
        self.ipmi.set_parameter.assert_not_called()
        self.assertFalse(self.controller.allow_fallback)
        self.controller.apply({"action": "off"})
        with patch.object(self.owner, "resume", side_effect=OSError("cannot clear owner")):
            with self.assertRaises(OSError):
                self.controller.apply({"action": "on"})
        self.assertEqual(self.controller.state.data["mode"], "paused")

    def test_restart_remembers_manual_without_reissuing_or_sampling(self):
        self.controller.apply({"action": "manual", "parameter": 25})
        self.owner.confirm(None, parameter=25)
        owner = Ownership(self.root, IDENTITY, profile(), "next")
        executor = Executor(self.ipmi, profile(), owner, Mock(), self.stop)
        executor.restore_confirmed(owner.check_start())
        controller = ServiceController(executor, self.config, self.root, self.collect, self.publish)
        controller.tick(self.queue)
        self.assertFalse(controller.snapshot()["automatic"])
        self.assertEqual(controller.snapshot()["manual_parameter"], 25)
        self.assertIsNone(controller.snapshot()["last_sample_time"])
        self.assertFalse(controller.snapshot()["control"]["verified"])
        self.assertFalse(controller.state.data["operation_active"])
        self.collect.assert_not_called()
        self.ipmi.set_parameter.assert_called_once_with(25)

    def test_resume_resets_clock_and_requires_new_data(self):
        self.controller.apply({"action": "off"})
        old_engine = self.controller.engine
        self.now += 10000
        self.controller.apply({"action": "on"})
        self.assertIsNot(self.controller.engine, old_engine)
        self.ipmi.set_level.assert_not_called()
        self.controller.tick(self.queue)
        self.collect.assert_called_once()
        self.assertEqual(self.executor.target, 1)

    def test_external_override_suspends_but_explicit_manual_can_resume(self):
        self.controller.tick(self.queue)
        self.now += 15
        self.collect.return_value = report(2, self.now, recommended=5)
        self.controller.tick(self.queue)
        self.assertEqual(self.controller.state.data["mode"], "suspended")
        self.ipmi.set_level.assert_not_called()
        self.assertTrue(json.loads(self.owner.path.read_text())["yielded"])
        self.controller.apply({"action": "off"})
        self.assertTrue(json.loads(self.owner.path.read_text())["yielded"])
        self.controller.apply({"action": "manual", "parameter": 25})
        self.ipmi.set_parameter.assert_called_once_with(25)
        self.assertFalse(self.executor.yielded)

    def test_confirmed_manual_write_yields_to_a_later_external_change(self):
        self.controller.apply({"action": "manual", "parameter": 20})
        self.now = 110
        self.collect.return_value = report(2, self.now, recommended=5)
        self.controller.tick(self.queue)
        self.assertTrue(self.executor.snapshot()["bmc_confirmed"])
        self.assertTrue(self.controller.manual_pending)
        self.now = 125
        self.collect.return_value = report(3, self.now, recommended=5)
        self.controller.tick(self.queue)
        self.assertEqual(self.controller.state.data["mode"], "suspended")
        self.assertFalse(self.controller.allow_fallback)
        self.assertFalse(self.controller.manual_pending)
        self.assertTrue(json.loads(self.owner.path.read_text())["yielded"])
        self.ipmi.set_level.assert_not_called()
        self.ipmi.set_parameter.assert_called_once_with(20)

    def test_stale_or_failed_manual_samples_do_not_verify_calibrated_rpm(self):
        self.controller.apply({"action": "manual", "parameter": 20})
        self.now += 20
        self.collect.return_value = report(2, self.now, 5, status="failed")
        self.controller.tick(self.queue)
        self.assertFalse(self.executor.snapshot()["verified"])
        self.now += 100
        with self.assertRaises(ExecutionFailure):
            self.controller.tick(self.queue)

    def test_legacy_owner_and_arbitrary_owner_resume_protect_external_changes(self):
        for parameter in (20, 25):
            data = self.owner.data.copy()
            data.update(active=True, last_target=2, confirmed_target=2)
            if parameter == 20:
                data.pop("parameter")
                data.pop("confirmed_parameter")
            else:
                data.update(last_target=None, confirmed_target=None, parameter=25, confirmed_parameter=25)
            self.owner.path.write_text(json.dumps(data))
            owner = Ownership(self.root, IDENTITY, profile(), "new")
            executor = Executor(self.ipmi, profile(), owner, Mock(), self.stop)
            executor.restore_confirmed(owner.check_start())
            self.assertTrue(executor.ownership_pending)
            self.assertFalse(executor.snapshot()["bmc_confirmed"])
            self.ipmi.control_status.return_value = status_result(1)
            with self.assertRaises(ExternalOverride):
                executor.request(5)
        self.ipmi.set_level.assert_not_called()


class SocketTests(unittest.TestCase):
    def test_real_socket_dispatch_permissions_and_status(self):
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory, CommandServer(directory) as server:
            result = []
            client = threading.Thread(target=lambda: result.append(command_client({"action": "manual", "parameter": 25}, directory)))
            client.start()
            self.assertTrue(server.wake.wait(2))
            requests = list(server.pending())
            self.assertEqual(len(requests), 1)
            self.assertEqual(requests[0].command, {"action": "manual", "parameter": 25})
            requests[0].reply({"ok": True, "message": "accepted; not verified"})
            client.join(2)
            self.assertFalse(client.is_alive())
            self.assertTrue(result[0]["ok"])
            self.assertEqual(server.path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(Path(directory, "control.sock").exists())

    def test_reject_invalid_duplicate_oversized_messages_before_queue(self):
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory, CommandServer(directory) as server:
            for payload in (b'{"action":"manual","parameter":true}\n', b'{"action":"off","action":"on"}\n',
                            b'{"action":"off","extra":1}\n', b'x' * (MAX_MESSAGE + 1)):
                with socket.socket(socket.AF_UNIX) as stream:
                    stream.settimeout(3)
                    stream.connect(str(server.path))
                    stream.sendall(payload)
                    self.assertFalse(receive(stream)["ok"])
            self.assertEqual(list(server.pending()), [])

    def test_expired_requests_are_never_executed(self):
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory, CommandServer(directory) as server:
            request = Request({"action": "manual", "parameter": 25})
            request.deadline = 0
            server.requests.put(request)
            self.assertEqual(list(server.pending()), [])
            self.assertFalse(request.result["ok"])

    def test_non_socket_path_and_symlink_are_not_overwritten(self):
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory:
            path = Path(directory, "control.sock")
            path.write_text("keep")
            with self.assertRaises(ValueError):
                with CommandServer(directory):
                    pass
            self.assertEqual(path.read_text(), "keep")
            path.unlink()
            path.symlink_to("missing")
            with self.assertRaises(ValueError):
                with CommandServer(directory):
                    pass
            self.assertTrue(path.is_symlink())

    def test_cli_client_only_and_old_floor_rejected(self):
        with patch("hrx_fancontrol.commands.command_client", return_value={"ok": True, "message": "ok"}) as client, \
             patch("hrx_fancontrol.protocol.ControlIPMI.set_parameter") as write, \
             patch("sys.stdout", new_callable=io.StringIO), patch("sys.stderr", new_callable=io.StringIO):
            self.assertEqual(main(["manual", "25"]), 0)
            client.assert_called_once_with({"action": "manual", "parameter": 25})
            self.assertEqual(main(["auto", "off"]), 0)
            self.assertEqual(main(["--json", "status"]), 0)
            self.assertEqual(main(["manual", "0x14"]), 1)
            with self.assertRaises(SystemExit):
                main(["--manual-level", "2", "--profile", "p.json"])
            write.assert_not_called()
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaisesRegex(ValueError, "无法连接"):
                command_client({"action": "manual", "parameter": 25}, directory)

class ServiceSocketIntegrationTests(unittest.TestCase):
    def test_manual_verification_then_off_queued_during_auto_read(self):
        with tempfile.TemporaryDirectory(prefix="hrx-", dir="/tmp") as directory:
            config = load_config()
            owner = Ownership(directory, IDENTITY, profile(), "service")
            stop = threading.Event()
            ipmi = Mock()
            ipmi.set_parameter.return_value = success()
            executor = Executor(ipmi, profile(), owner, Mock(), stop)
            entered, release, verified = threading.Event(), threading.Event(), threading.Event()
            block_auto = threading.Event()
            errors = []
            def collect_next(engine, automatic):
                if automatic and block_auto.is_set():
                    entered.set()
                    if not release.wait(3):
                        raise AssertionError("test failed to release the simulated BMC read")
                data = report(1, time.monotonic(), recommended=5)
                data["bmc"]["raw_bytes"] = parameter_raw(25)
                return data
            def publish(data, mode):
                if mode == "manual":
                    verified.set()
            controller = ServiceController(executor, config, directory, collect_next, publish)
            controller.apply({"action": "off"})
            with CommandServer(directory) as server:
                def run():
                    try:
                        controller.run(server)
                    except Exception as exc:
                        errors.append(exc)
                worker = threading.Thread(target=run)
                worker.start()
                try:
                    result = command_client({"action": "manual", "parameter": 25}, directory)
                    self.assertTrue(result["ok"])
                    self.assertTrue(verified.wait(2))
                    state = command_client({"action": "status"}, directory)["status"]
                    self.assertFalse(state["automatic"])
                    self.assertTrue(state["control"]["bmc_confirmed"])
                    self.assertFalse(state["control"]["verified"])
                    self.assertEqual(state["manual_parameter"], 25)
                    block_auto.set()
                    self.assertTrue(command_client({"action": "on"}, directory)["ok"])
                    self.assertTrue(entered.wait(2))
                    responses = []
                    client = threading.Thread(target=lambda: responses.append(command_client({"action": "off"}, directory)))
                    client.start()
                    self.assertTrue(server.wake.wait(2))
                    release.set()
                    client.join(3)
                    self.assertFalse(client.is_alive())
                    self.assertTrue(responses[0]["ok"])
                    self.assertFalse(responses[0]["status"]["automatic"])
                    ipmi.set_parameter.assert_called_once_with(25)
                    ipmi.set_level.assert_not_called()
                finally:
                    release.set()
                    stop.set()
                    server.wake.set()
                    worker.join(3)
                self.assertFalse(worker.is_alive())
                self.assertEqual(errors, [])
