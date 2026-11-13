"""Run maintainer-script regressions only inside temporary filesystem roots."""
import os
from pathlib import Path
import shlex
import shutil
import subprocess
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
        # Rewrite fixed runtime locations in a temporary copy, never host /run.
        text = (ROOT / "packaging/debian" / name).read_text()
        text = text.replace("/run/hrx-fancontrol-package", str(self.root / "package-state"))
        text = text.replace("/run/systemd/system", str(self.root / "systemd"))
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
