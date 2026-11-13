"""Host-independent package and release contract checks; no hardware or upload."""
import hashlib
import importlib.util
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))
import build_deb
import release


class PackagingTests(unittest.TestCase):
    def test_native_tree_is_complete_without_machine_data(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            build_deb.stage(root, build_deb.version())
            self.assertEqual((root / "DEBIAN/conffiles").read_text(), "/etc/hrx-fancontrol/config.yaml\n")
            self.assertIn("python3-yaml (>= 6.0)", (root / "DEBIAN/control").read_text())
            self.assertEqual((root / "usr/bin/hrx-fancontrol").stat().st_mode & 0o777, 0o755)
            library = root / "usr/lib/hrx-fancontrol"
            package = library / "hrx_fancontrol"
            source = build_deb.ROOT / "src/hrx_fancontrol"
            expected = {path.relative_to(source) for path in source.rglob("*")
                        if path.is_file() and path.suffix in (".py", ".yaml")
                        and not any(part == "__pycache__" or part.endswith((".egg-info", ".dist-info"))
                                    for part in path.relative_to(source).parts)}
            self.assertEqual({path.relative_to(package) for path in package.rglob("*") if path.is_file()},
                             expected)
            for relative in expected:
                self.assertEqual((package / relative).read_bytes(), (source / relative).read_bytes())
            for group in ("hardware", "policy", "control", "reporting"):
                self.assertTrue((package / group / "__init__.py").is_file())
            self.assertTrue((package / "default.yaml").is_file())
            for name in ("hr650x-guide.md", "development.md"):
                document = root / "usr/share/doc/hrx-fancontrol" / name
                self.assertTrue(document.is_file())
                self.assertEqual(document.read_text(), (build_deb.ROOT / "docs" / name).read_text())
            unit = (root / "usr/lib/systemd/system/hrx-fancontrol.service").read_text()
            self.assertNotIn("/opt/", unit)
            self.assertNotIn("--profile", unit)
            self.assertIn("ExecStopPost=/usr/bin/hrx-fancontrol", unit)
            self.assertNotIn("enable ", (root / "DEBIAN/postinst").read_text())
            self.assertFalse((root / "var").exists())
            self.assertEqual(sorted(p.name for p in (root / "etc/hrx-fancontrol").iterdir()), ["config.yaml"])
            for name in ("preinst", "postinst", "prerm", "postrm"):
                self.assertEqual((root / "DEBIAN" / name).stat().st_mode & 0o777, 0o755)
            replay = subprocess.run(
                [sys.executable, "-I", "-B", "-c", '''
import importlib
from pathlib import Path
import runpy
import sys
library = Path(sys.argv.pop(1)).resolve()
sys.path.insert(0, str(library))
package = library / "hrx_fancontrol"
for path in sorted(package.rglob("*.py")):
    if path.name == "__main__.py":
        continue
    parts = path.relative_to(library).with_suffix("").parts
    name = ".".join(parts[:-1] if parts[-1] == "__init__" else parts)
    module = importlib.import_module(name)
    assert Path(module.__file__).resolve() == path.resolve(), module.__file__
runpy.run_module("hrx_fancontrol", run_name="__main__")
''', str(library), "--once", "--input",
                 str(root / "usr/share/doc/hrx-fancontrol/examples/hr650x-observed-sensors.txt"),
                 "--no-log", "--json"],
                cwd=root, text=True, capture_output=True, timeout=10)
            self.assertEqual(replay.returncode, 0, replay.stderr)
            report = json.loads(replay.stdout)
            self.assertEqual(report["source"], "offline-snapshot")
            self.assertEqual(report["mode"], "dry-run")
            self.assertFalse(report["applied"])
            self.assertEqual(len(report["fans"]), 6)

    def test_rejects_invalid_or_mismatched_tag_before_git(self):
        with patch.object(release, "run") as git:
            for tag in ("$(touch bad)", "--help", "v01.0.0", "v0.1.0-rc1", "v9.9.9"):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release.validate_tag(tag, "abc")
            git.assert_not_called()

    def test_pins_event_sha_and_rejects_moved_tag(self):
        with patch.object(release, "run", side_effect=["event", "main", "moved"]), patch.object(release.subprocess, "run") as command:
            with self.assertRaisesRegex(ValueError, "moved"):
                release.validate_tag("v" + build_deb.version(), "event")
            self.assertEqual(command.call_count, 1)

    def test_requires_main_ancestry(self):
        with patch.object(release, "run", side_effect=["event", "main", "event"]), patch.object(release.subprocess, "run") as command:
            release.validate_tag("v" + build_deb.version(), "event")
            self.assertEqual(command.call_args.args[0], ["git", "merge-base", "--is-ancestor", "event", "main"])
            self.assertTrue(command.call_args.kwargs['check'])

    def test_release_immutable_draft_or_changed_tag_rejected(self):
        for data in ({"immutable": True}, {"draft": True}, {"tag_name": "other"}):
            with patch.object(release, "api", return_value=dict(tag_name="v0.1.0", **{k:v for k,v in data.items() if k!='tag_name'}) if 'tag_name' not in data else data):
                with self.assertRaises(ValueError):
                    release.release_info("o/r", "123", "v0.1.0")

    def test_upload_retries_matching_asset_and_refuses_collision(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deb = root / "hrx-fancontrol_0.1.0_all.deb"
            deb.write_bytes(b"deb")
            sums = root / "SHA256SUMS"
            sums.write_text("checksum")
            info = {"assets": [{"name": deb.name, "id": 42}], "upload_url": "https://uploads.github.com/repos/o/r/releases/1/assets{?name,label}"}
            def execute(args, **kw):
                if "--method" not in args:
                    kw["stdout"].write(b"deb")
            with patch.object(release, "release_info", return_value=info), patch.object(release.subprocess, "run", side_effect=execute) as command:
                release.upload("o/r", "1", "v0.1.0", root)
                self.assertEqual(command.call_count, 2)
                self.assertIn(str(sums), command.call_args.args[0])
            def wrong(args, **kw):
                kw["stdout"].write(b"different")
            with patch.object(release, "release_info", return_value=info), patch.object(release.subprocess, "run", side_effect=wrong) as command:
                with self.assertRaisesRegex(ValueError, "differs"):
                    release.upload("o/r", "1", "v0.1.0", root)
                self.assertEqual(command.call_count, 1)


if __name__ == '__main__':
    unittest.main()
