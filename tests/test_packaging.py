"""Host-independent package and release contract checks; no hardware or upload."""
import hashlib
import importlib.util
import json
from pathlib import Path
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
            self.assertTrue((root / "usr/lib/hrx-fancontrol/hrx_fancontrol/default.yaml").is_file())
            unit = (root / "usr/lib/systemd/system/hrx-fancontrol.service").read_text()
            self.assertNotIn("/opt/", unit)
            self.assertNotIn("--profile", unit)
            self.assertIn("ExecStopPost=/usr/bin/hrx-fancontrol", unit)
            self.assertNotIn("enable ", (root / "DEBIAN/postinst").read_text())
            self.assertFalse((root / "var").exists())
            self.assertEqual(sorted(p.name for p in (root / "etc/hrx-fancontrol").iterdir()), ["config.yaml"])
            for name in ("preinst", "postinst", "prerm", "postrm"):
                self.assertEqual((root / "DEBIAN" / name).stat().st_mode & 0o777, 0o755)

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
