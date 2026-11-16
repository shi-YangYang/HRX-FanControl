"""Host-independent package and release contract checks; no hardware or upload."""
import hashlib
import importlib.util
import io
import json
import os
from pathlib import Path
import shutil
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

    def test_rejects_invalid_tag_before_git_or_package_output(self):
        with patch.object(release, "run") as git:
            for tag in ("$(touch bad)", "--help", "v01.0.0", "v0.1.0-rc1", "v.0.1.2",
                        "v0.1.2\n", "0.1.2", "v0.01.2", "v0.1.02", "v0.1.2/other"):
                with self.subTest(tag=tag), self.assertRaises(ValueError):
                    release.validate_tag(tag, "abc")
            git.assert_not_called()
        with tempfile.TemporaryDirectory() as tmp:
            destination = Path(tmp) / "package"
            with self.assertRaises(ValueError):
                build_deb.stage(destination, "0.1.2\nInjected: yes")
            self.assertFalse(destination.exists())
            output = Path(tmp) / "dist"
            with patch.object(sys, "argv", ["build_deb.py", "--tag", "v.0.1.2", "--output", str(output)]), \
                    patch.object(build_deb.subprocess, "run") as builder, patch.object(sys, "stderr", io.StringIO()):
                with self.assertRaises(SystemExit) as result:
                    build_deb.main()
                self.assertEqual(result.exception.code, 2)
                builder.assert_not_called()
                self.assertFalse(output.exists())

    def test_stage_uses_release_version_preserving_source_and_other_initialization(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "source"
            root.mkdir()
            for name in ("src", "packaging", "docs", "examples"):
                shutil.copytree(build_deb.ROOT / name, root / name,
                                ignore=shutil.ignore_patterns("__pycache__", "*.egg-info"))
            for name in ("LICENSE", "pyproject.toml"):
                shutil.copyfile(build_deb.ROOT / name, root / name)
            source_init = root / "src/hrx_fancontrol/__init__.py"
            original = 'MARKER = "保留"; __version__ = "0.1.1"; AFTER = 42\r\n'.encode()
            source_init.write_bytes(original)
            project_before = (root / "pyproject.toml").read_bytes()
            destination = Path(tmp) / "package"
            build_deb.stage(destination, build_deb.version_from_tag("v0.1.2"), root)
            library = destination / "usr/lib/hrx-fancontrol"
            self.assertIn("Version: 0.1.2\n", (destination / "DEBIAN/control").read_text())
            self.assertEqual((library / "hrx_fancontrol/__init__.py").read_bytes(),
                             original.replace(b'"0.1.1"', b"'0.1.2'"))
            installed = subprocess.check_output(
                [sys.executable, "-I", "-B", "-c",
                 'import sys,json; sys.path.insert(0,sys.argv[1]); import hrx_fancontrol as p; '
                 'print(json.dumps([p.__version__,p.MARKER,p.AFTER]))', str(library)],
                text=True, cwd=tmp)
            self.assertEqual(json.loads(installed), ["0.1.2", "保留", 42])
            self.assertEqual(source_init.read_bytes(), original)
            self.assertEqual((root / "pyproject.toml").read_bytes(), project_before)

    def test_build_cli_names_metadata_and_checksums_use_selected_version(self):
        originals = {path: path.read_bytes() for path in
                     (build_deb.ROOT / "pyproject.toml", build_deb.ROOT / "src/hrx_fancontrol/__init__.py")}
        source_version = build_deb.version()
        for options, expected in ((["--tag", "v0.1.2"], "0.1.2"), ([], source_version)):
            with self.subTest(options=options), tempfile.TemporaryDirectory() as tmp:
                output = Path(tmp) / "dist"
                def build(args, **kwargs):
                    self.assertEqual(args[:4], ["dpkg-deb", "--root-owner-group", "-Zxz", "--build"])
                    package = Path(args[-2])
                    self.assertIn(f"Version: {expected}\n", (package / "DEBIAN/control").read_text())
                    spec = importlib.util.spec_from_file_location("staged_package", package / "usr/lib/hrx-fancontrol/hrx_fancontrol/__init__.py")
                    module = importlib.util.module_from_spec(spec)
                    spec.loader.exec_module(module)
                    self.assertEqual(module.__version__, expected)
                    Path(args[-1]).write_bytes(b"mock deb archive")
                with patch.object(sys, "argv", ["build_deb.py", *options, "--output", str(output)]), \
                        patch.dict(os.environ, SOURCE_DATE_EPOCH="1700000000"), \
                        patch.object(build_deb.subprocess, "run", side_effect=build) as builder, \
                        patch.object(sys, "stdout", io.StringIO()):
                    build_deb.main()
                builder.assert_called_once()
                package = output / f"hrx-fancontrol_{expected}_all.deb"
                self.assertEqual(sorted(path.name for path in output.iterdir()), ["SHA256SUMS", package.name])
                self.assertEqual((output / "SHA256SUMS").read_text(),
                                 f"{hashlib.sha256(package.read_bytes()).hexdigest()}  {package.name}\n")
        for path, original in originals.items():
            self.assertEqual(path.read_bytes(), original)

    def test_release_entrypoints_allow_tag_version_without_reading_source_version(self):
        environment = dict(GITHUB_REPOSITORY="o/r", RELEASE_ID="123", RELEASE_TAG="v0.1.2", GITHUB_SHA="event")
        for action in ("validate", "upload"):
            with self.subTest(action=action), patch.dict(os.environ, environment), \
                    patch.object(sys, "argv", ["release.py", action]), \
                    patch.object(build_deb, "version", side_effect=AssertionError("Source version is not the release version")), \
                    patch.object(release, "run", side_effect=["event", "main", "event"]), \
                    patch.object(release.subprocess, "run") as command, \
                    patch.object(release, "release_info"), patch.object(release, "upload") as upload:
                release.main()
                self.assertEqual(command.call_args.args[0], ["git", "merge-base", "--is-ancestor", "event", "main"])
                if action == "upload":
                    self.assertEqual(upload.call_args.args[:3], ("o/r", "123", "v0.1.2"))
                else:
                    upload.assert_not_called()

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
