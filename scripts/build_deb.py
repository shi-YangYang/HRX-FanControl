#!/usr/bin/env python3
"""Build the native Debian package without pip or machine-specific files."""
import argparse
import ast
import hashlib
import os
from pathlib import Path
import re
import shutil
import subprocess
import tempfile

ROOT = Path(__file__).resolve().parents[1]


def version(root=ROOT):
    project = (root / "pyproject.toml").read_text()
    match = re.search(r'^version = "([0-9]+\.[0-9]+\.[0-9]+)"$', project, re.M)
    if not match:
        raise ValueError("pyproject.toml must contain a stable X.Y.Z version")
    expected = match.group(1)
    tree = ast.parse((root / "src/hrx_fancontrol/__init__.py").read_text())
    values = [ast.literal_eval(n.value) for n in tree.body if isinstance(n, ast.Assign)
              and any(isinstance(t, ast.Name) and t.id == "__version__" for t in n.targets)]
    if values != [expected]:
        raise ValueError("pyproject.toml and package __version__ must match")
    return expected


def stage(destination, release_version, root=ROOT):
    def put(name, content, mode=0o644):
        path = destination / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content)
        path.chmod(mode)

    package = destination / "usr/lib/hrx-fancontrol/hrx_fancontrol"
    package.mkdir(parents=True)
    source = root / "src/hrx_fancontrol"
    for path in sorted(source.rglob("*")):
        relative = path.relative_to(source)
        if any(part == "__pycache__" or part.endswith((".egg-info", ".dist-info"))
               for part in relative.parts):
            continue
        if path.suffix in (".py", ".yaml") and path.is_file():
            target = package / relative
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target)
            target.chmod(0o644)
    put("usr/bin/hrx-fancontrol", '''#!/usr/bin/python3
import sys
sys.path.insert(0, "/usr/lib/hrx-fancontrol")
from hrx_fancontrol.main import main
raise SystemExit(main())
''', 0o755)
    unit = (root / "packaging/systemd/hrx-fancontrol.service").read_text()
    unit = unit.replace("/opt/hrx-fancontrol/.venv/bin/hrx-fancontrol", "/usr/bin/hrx-fancontrol")
    unit = unit.replace("WorkingDirectory=/opt/hrx-fancontrol", "WorkingDirectory=/var/lib/hrx-fancontrol")
    if "/opt/" in unit:
        raise ValueError("Unconverted source-install path in systemd unit")
    put("usr/lib/systemd/system/hrx-fancontrol.service", unit)
    put("etc/hrx-fancontrol/config.yaml", "logging:\n  directory: /var/log/hrx-fancontrol\n")
    put("usr/share/doc/hrx-fancontrol/copyright", (root / "LICENSE").read_text())
    for name in ("hr650x-guide.md", "development.md"):
        put("usr/share/doc/hrx-fancontrol/" + name, (root / "docs" / name).read_text())
    put("usr/share/doc/hrx-fancontrol/examples/hr650x-observed-sensors.txt",
        (root / "examples/hr650x-observed-sensors.txt").read_text())
    put("DEBIAN/control", f'''Package: hrx-fancontrol
Version: {release_version}
Section: admin
Priority: optional
Architecture: all
Maintainer: HRX-FanControl maintainers <noreply@github.com>
Depends: python3 (>= 3.9), python3-yaml (>= 6.0), python3-yaml (<< 7), ipmitool, init-system-helpers
Homepage: https://github.com/shi-YangYang/HRX-FanControl
Description: Temperature monitoring and fan control for Lenovo HR650X
 Read-only monitoring and explicitly enabled calibrated local fan control.
''')
    put("DEBIAN/conffiles", "/etc/hrx-fancontrol/config.yaml\n")
    for name in ("preinst", "postinst", "prerm", "postrm"):
        put("DEBIAN/" + name, (root / "packaging/debian" / name).read_text(), 0o755)
    for directory in destination.rglob("*"):
        if directory.is_dir():
            directory.chmod(0o755)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--tag", help="Release tag vX.Y.Z; must match source version")
    parser.add_argument("--output", type=Path, default=ROOT / "dist")
    args = parser.parse_args()
    release_version = version()
    if args.tag and args.tag != "v" + release_version:
        parser.error("Release tag must be v" + release_version)
    args.output.mkdir(parents=True, exist_ok=True)
    output = args.output.resolve() / f"hrx-fancontrol_{release_version}_all.deb"
    epoch = os.environ.get("SOURCE_DATE_EPOCH") or subprocess.check_output(
        ["git", "log", "-1", "--format=%ct"], cwd=ROOT, text=True).strip()
    with tempfile.TemporaryDirectory(prefix="hrx-deb-") as temp:
        destination = Path(temp) / "package"
        stage(destination, release_version)
        for path in destination.rglob("*"):
            os.utime(path, (int(epoch), int(epoch)))
        os.utime(destination, (int(epoch), int(epoch)))
        subprocess.run(["dpkg-deb", "--root-owner-group", "-Zxz", "--build", str(destination), str(output)],
                       check=True, env=dict(os.environ, SOURCE_DATE_EPOCH=epoch))
    checksum = hashlib.sha256(output.read_bytes()).hexdigest()
    (args.output / "SHA256SUMS").write_text(f"{checksum}  {output.name}\n")
    print(output)


if __name__ == "__main__":
    main()
