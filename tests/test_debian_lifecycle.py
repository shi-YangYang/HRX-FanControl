"""Run maintainer-script regressions only inside temporary filesystem roots."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]
HELPER = os.environ.get("HRX_TEST_SYSTEMD_HELPER") or shutil.which("deb-systemd-helper")


class MaintainerScriptTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="hrx-maintscript-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.bin = self.root / "bin"
        self.bin.mkdir()
        self.calls = self.root / "calls"
        self.env = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ["PATH"],
                        CALLS=str(self.calls), DPKG_ROOT=str(self.root),
                        DPKG_MAINTSCRIPT_PACKAGE="hrx-fancontrol")
        self.stub("systemctl", 'printf "%s\\n" "$*" >>"$CALLS"\n'
                  'if [ "$1" = show ]; then\n'
                  '  printf "%s\\n" "$TEST_STATE"\n'
                  '  exit "$TEST_QUERY_EXIT"\nfi\n')

    def stub(self, name, body):
        target = self.bin / name
        target.write_text("#!/bin/sh\nset -eu\n" + body)
        target.chmod(0o755)

    def run_script(self, name, action):
        # Rewrite fixed locations in a temporary copy, never host /run or /usr.
        text = (ROOT / "packaging/debian" / name).read_text()
        text = text.replace("/run/hrx-fancontrol-package", str(self.root / "package-state"))
        text = text.replace("/run/systemd/system", str(self.root / "systemd"))
        text = text.replace("/usr/lib/hrx-fancontrol", str(self.root / "usr/lib/hrx-fancontrol"))
        script = self.root / name
        script.write_text(text)
        return subprocess.run(["sh", str(script), action], env=self.env,
                              text=True, capture_output=True, timeout=10)

    def test_upgrade_rejects_failed_and_unknown_state_without_stopping(self):
        (self.root / "systemd").mkdir()
        for state, query_exit in (("active", "1"), ("", "0"), ("unknown", "0")):
            with self.subTest(state=state, query_exit=query_exit):
                self.calls.write_text("")
                self.env.update(TEST_STATE=state, TEST_QUERY_EXIT=query_exit)
                result = self.run_script("preinst", "upgrade")
                self.assertNotEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.calls.read_text().splitlines(),
                                 ["show --property=ActiveState --value hrx-fancontrol.service"])
                self.assertFalse((self.root / "package-state/restart").exists())

    def test_upgrade_stops_expected_states_and_preserves_retry_marker(self):
        (self.root / "systemd").mkdir()
        marker = self.root / "package-state/restart"
        for state in ("active", "activating", "reloading", "deactivating", "inactive", "failed"):
            with self.subTest(state=state):
                marker.unlink(missing_ok=True)
                self.calls.write_text("")
                self.env.update(TEST_STATE=state, TEST_QUERY_EXIT="0")
                result = self.run_script("preinst", "upgrade")
                self.assertEqual(result.returncode, 0, result.stderr)
                should_stop = state in ("active", "activating", "reloading", "deactivating")
                self.assertEqual("stop hrx-fancontrol.service" in self.calls.read_text(), should_stop)
                self.assertEqual(marker.exists(), state in ("active", "activating", "reloading"))
        marker.touch()
        self.env.update(TEST_STATE="inactive", TEST_QUERY_EXIT="0")
        result = self.run_script("preinst", "upgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(marker.exists())

    def test_configure_invalidates_equal_mtime_bytecode_before_optional_restart(self):
        self.stub("deb-systemd-helper", 'printf "helper %s\\n" "$*" >>"$CALLS"\n')
        library = self.root / "usr/lib/hrx-fancontrol"
        package = library / "hrx_fancontrol"
        package.mkdir(parents=True)
        source = package / "__init__.py"
        old, new = b'__version__ = "0.1.2"\n', b'__version__ = "0.1.3"\n'
        self.assertEqual(len(old), len(new))
        epoch = 1700000000
        # Match Ubuntu's normal bytecode writes even on hosts that disable them.
        command = [sys.executable, "-I", "-c",
                   "import sys; sys.pycache_prefix = None; sys.dont_write_bytecode = False; "
                   "sys.path.insert(0, sys.argv[1]); "
                   "import hrx_fancontrol; print(hrx_fancontrol.__version__)", str(library)]
        # The service stub imports the installed package when it is restarted.
        self.stub("systemctl", 'printf "%s\\n" "$*" >>"$CALLS"\n'
                  'if [ "$1" = start ]; then\n  ' +
                  " ".join(shlex.quote(part) for part in command) +
                  " >" + shlex.quote(str(self.root / "started-version")) + "\nfi\n")
        protected = (self.root / "etc/hrx-fancontrol/email.json",
                     self.root / "var/lib/hrx-fancontrol/service-mode.json",
                     self.root / "var/log/hrx-fancontrol/kept.jsonl",
                     self.root / "outside/kept.pyc")
        for path in protected:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"keep this file\n")
        (library / "outside-link").symlink_to(self.root / "outside", target_is_directory=True)
        marker = self.root / "package-state/restart"
        marker.parent.mkdir()
        for has_systemd, restart in ((False, False), (True, False), (True, True)):
            with self.subTest(systemd=has_systemd, restart=restart):
                if has_systemd:
                    (self.root / "systemd").mkdir(exist_ok=True)
                self.calls.write_text("")
                source.write_bytes(old)
                os.utime(source, (epoch, epoch))
                self.assertEqual(subprocess.check_output(command, text=True).strip(), "0.1.2")
                cached = list(package.rglob("*.pyc"))
                self.assertTrue(cached)
                source.write_bytes(new)
                os.utime(source, (epoch, epoch))
                # Prove the stale cache is actually selected before configuration.
                self.assertEqual(subprocess.check_output(command, text=True).strip(), "0.1.2")
                if restart:
                    marker.touch()
                result = self.run_script("postinst", "configure")
                self.assertEqual(result.returncode, 0, result.stderr)
                calls = self.calls.read_text().splitlines()
                self.assertEqual("start hrx-fancontrol.service" in calls, restart)
                self.assertEqual("daemon-reload" in calls, has_systemd)
                self.assertFalse(any("enable " in call for call in calls))
                self.assertFalse(marker.exists())
                if restart:
                    self.assertEqual((self.root / "started-version").read_text().strip(), "0.1.3")
                self.assertEqual(subprocess.check_output(command, text=True).strip(), "0.1.3")
                self.assertEqual(source.read_bytes(), new)
                self.assertEqual(source.stat().st_mtime, epoch)
                for path in protected:
                    self.assertEqual(path.read_bytes(), b"keep this file\n")
                # Start each case with no cache from the previous case's new source.
                for path in package.rglob("*.pyc"):
                    path.unlink()

    def test_configure_cache_failure_keeps_restart_marker_and_does_not_start(self):
        (self.root / "usr/lib/hrx-fancontrol").mkdir(parents=True)
        (self.root / "systemd").mkdir()
        marker = self.root / "package-state/restart"
        marker.parent.mkdir()
        marker.touch()
        self.stub("find", "exit 1\n")
        self.stub("deb-systemd-helper", 'printf "helper %s\\n" "$*" >>"$CALLS"\n')
        result = self.run_script("postinst", "configure")
        self.assertNotEqual(result.returncode, 0)
        self.assertTrue(marker.exists())
        self.assertFalse(self.calls.exists())

    @unittest.skipUnless(HELPER, "requires Debian helper (or HRX_TEST_SYSTEMD_HELPER)")
    def test_configure_records_links_without_enabling_and_purge_removes_them(self):
        self.stub("deb-systemd-helper", "exec perl " + shlex.quote(HELPER) + ' "$@"\n')
        unit = self.root / "usr/lib/systemd/system/hrx-fancontrol.service"
        unit.parent.mkdir(parents=True)
        unit.write_text((ROOT / "packaging/systemd/hrx-fancontrol.service").read_text())
        link = self.root / "etc/systemd/system/multi-user.target.wants/hrx-fancontrol.service"
        result = self.run_script("postinst", "configure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.is_symlink())
        self.assertFalse(self.calls.exists())  # No service-manager start or enable.
        # Match systemctl enable, then an upgrade, then removal of packaged files.
        link.parent.mkdir(parents=True, exist_ok=True)
        link.symlink_to("/usr/lib/systemd/system/hrx-fancontrol.service")
        result = self.run_script("postinst", "configure")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(link.is_symlink())
        unit.unlink()
        user_files = ("etc/hrx-fancontrol/calibration.json",
                      "var/lib/hrx-fancontrol/service-mode.json",
                      "var/lib/hrx-fancontrol/owner.json",
                      "var/log/hrx-fancontrol/preserved.jsonl",
                      "etc/systemd/system/hrx-fancontrol.service.d/local.conf")
        for name in user_files:
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("preserve\n")
        result = self.run_script("postrm", "purge")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertFalse(link.is_symlink())
        for name in user_files:
            self.assertEqual((self.root / name).read_text(), "preserve\n")


if __name__ == "__main__":
    unittest.main()
