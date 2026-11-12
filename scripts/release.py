#!/usr/bin/env python3
"""Validate a GitHub Release and upload only matching or missing assets."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
from build_deb import version


def run(*args):
    return subprocess.check_output(args, text=True).strip()


def api(path):
    return json.loads(run("gh", "api", path))


def release_info(repo, release_id, tag):
    release = api(f"repos/{repo}/releases/{release_id}")
    if release.get("draft") or release.get("immutable") or release["tag_name"] != tag:
        raise ValueError("Release must be published, mutable, and use the expected tag; immutable releases cannot accept assets")
    return release


def validate_tag(tag, sha):
    if not re.fullmatch(r"v(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)\.(?:0|[1-9][0-9]*)", tag):
        raise ValueError("Expected stable Release tag vX.Y.Z")
    if tag != "v" + version():
        raise ValueError("Release tag does not match the source version")
    if run("git", "rev-parse", "HEAD") != sha:
        raise ValueError("Checkout does not match the release event SHA")
    subprocess.run(["git", "fetch", "--no-tags", "origin",
                    "refs/heads/main:refs/remotes/origin/main",
                    f"refs/tags/{tag}:refs/hrx-release-tag"], check=True)
    main_sha = run("git", "rev-parse", "refs/remotes/origin/main")
    tag_sha = run("git", "rev-parse", "refs/hrx-release-tag^{commit}")
    if tag_sha != sha:
        raise ValueError("Release tag moved since the event")
    subprocess.run(["git", "merge-base", "--is-ancestor", sha, main_sha], check=True)


def upload(repo, release_id, tag, directory):
    release = release_info(repo, release_id, tag)
    assets = {asset["name"]: asset for asset in release["assets"]}
    files = sorted(directory.glob("*.deb")) + [directory / "SHA256SUMS"]
    if len(files) != 2 or not all(p.is_file() for p in files):
        raise ValueError("Expected exactly one deb and SHA256SUMS")
    pending = []
    # Preflight every collision before uploading anything; never clobber a Release.
    with tempfile.TemporaryDirectory() as tmp:
        for path in files:
            existing = assets.get(path.name)
            if existing:
                downloaded = Path(tmp) / path.name
                with downloaded.open("wb") as out:
                    subprocess.run(["gh", "api", "-H", "Accept: application/octet-stream",
                                    f"repos/{repo}/releases/assets/{existing['id']}"], stdout=out, check=True)
                if hashlib.sha256(downloaded.read_bytes()).digest() != hashlib.sha256(path.read_bytes()).digest():
                    raise ValueError("Existing Release asset differs: " + path.name)
            else:
                pending.append(path)
    for path in pending:
        # Address the Release ID, not a tag that could be moved to another Release.
        url = release["upload_url"].split("{")[0]
        subprocess.run(["gh", "api", "--method", "POST", "-H", "Content-Type: application/octet-stream",
                        url + "?name=" + path.name, "--input", str(path)], check=True, stdout=subprocess.DEVNULL)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["validate", "upload"])
    parser.add_argument("--directory", type=Path, default=Path("dist"))
    args = parser.parse_args()
    repo, release_id, tag = (os.environ[k] for k in ("GITHUB_REPOSITORY", "RELEASE_ID", "RELEASE_TAG"))
    if args.action == "validate":
        release_info(repo, release_id, tag)
        validate_tag(tag, os.environ["GITHUB_SHA"])
    else:
        validate_tag(tag, os.environ["GITHUB_SHA"])
        upload(repo, release_id, tag, args.directory)


if __name__ == "__main__":
    main()
