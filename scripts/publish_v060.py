#!/usr/bin/env python3
"""Publish v0.6.0b1 from exactly the successful main/push CI commit, never a frozen older SHA."""

from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tarfile
import tempfile
import tomllib
import zipfile
from email.parser import Parser
from pathlib import Path
from typing import Any

VERSION = "0.6.0b1"
TAG = "v" + VERSION
REPOSITORY = "phamviet86/codex-a2a-gateway"
ASSETS = [f"codex_a2a_gateway-{VERSION}-py3-none-any.whl", f"codex_a2a_gateway-{VERSION}.tar.gz", "SHA256SUMS"]


def require(condition: bool, message: str) -> None:
    if not condition:
        raise RuntimeError(message)


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True).strip()


def api(path: str, data: dict[str, Any] | None = None, *, optional: bool = False) -> Any:
    command = ["gh", "api", path]
    if data is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(
        command, input=json.dumps(data) if data is not None else None, text=True, capture_output=True
    )
    if result.returncode:
        if optional and "(HTTP 404)" in result.stderr:
            return None
        raise RuntimeError(f"GitHub API failed for {path}: {result.stderr}")
    return json.loads(result.stdout)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def validate_run(repo: str, sha: str, run: dict[str, Any]) -> None:
    require(repo == REPOSITORY, "unexpected repository")
    require(re.fullmatch(r"[0-9a-f]{40}", sha) is not None, "invalid release SHA")
    require(run.get("name") == "CI", "unexpected source workflow")
    require(run.get("status") == "completed" and run.get("conclusion") == "success", "CI did not pass")
    require(run.get("event") == "push" and run.get("head_branch") == "main", "CI is not a main push")
    require(run.get("head_repository", {}).get("full_name") == repo, "foreign source repository")
    require(run.get("head_sha") == sha, "CI SHA does not match release SHA")
    require(f"[release {TAG}]" in run.get("head_commit", {}).get("message", ""), "release marker absent")


def find_release(prefix: str) -> dict[str, Any] | None:
    # Write-token listing includes drafts; by-tag discovery does not.
    for page in range(1, 101):
        releases = api(f"{prefix}/releases?per_page=100&page={page}")
        matches = [release for release in releases if release["tag_name"] == TAG]
        if matches:
            require(len(matches) == 1, "multiple releases for the same tag")
            return matches[0]
        if len(releases) < 100:
            return None
    raise RuntimeError("release listing incomplete; refusing a duplicate")


def verify_tag(prefix: str, sha: str) -> None:
    ref = api(f"{prefix}/git/ref/tags/{TAG}")
    require(ref["object"]["type"] == "tag", "expected annotated tag")
    target = api(f"{prefix}/git/tags/{ref['object']['sha']}")["object"]
    require(target["type"] == "commit" and target["sha"] == sha, "tag target differs from tested SHA")


def verify_download(directory: Path) -> None:
    require({path.name for path in directory.iterdir()} == set(ASSETS), "download asset set mismatch")
    subprocess.run([sys.executable, "scripts/write_sha256sums.py", "--check", str(directory)], check=True)
    with zipfile.ZipFile(directory / ASSETS[0]) as wheel:
        members = [name for name in wheel.namelist() if name.endswith(".dist-info/METADATA")]
        require(len(members) == 1, "wheel metadata missing or ambiguous")
        metadata = Parser().parsestr(wheel.read(members[0]).decode())
        require(metadata["Name"] == "codex-a2a-gateway" and metadata["Version"] == VERSION, "wheel version mismatch")
    with tarfile.open(directory / ASSETS[1]) as archive:
        members = [member for member in archive.getmembers() if member.name.endswith("/pyproject.toml")]
        require(len(members) == 1 and members[0].isfile(), "sdist project metadata missing or ambiguous")
        source = archive.extractfile(members[0])
        require(source is not None, "sdist metadata unreadable")
        if source is None:
            raise RuntimeError("sdist metadata unreadable")
        project = tomllib.loads(source.read().decode())["project"]
        require(project["name"] == "codex-a2a-gateway" and project["version"] == VERSION, "sdist version mismatch")


def main() -> None:
    repo, sha = os.environ["GH_REPO"], os.environ["RELEASE_SHA"]
    require(os.environ["GITHUB_EVENT_NAME"] == "workflow_run", "requires workflow_run event")
    run = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())["workflow_run"]
    validate_run(repo, sha, run)
    require(subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == sha, "checkout SHA differs")
    version = tomllib.loads(Path("pyproject.toml").read_text())["project"]["version"]
    require(version == VERSION, "source version mismatch")
    from codex_a2a_gateway import __version__

    require(__version__ == VERSION, "installed runtime version mismatch")
    prefix = f"repos/{repo}"
    validate_run(repo, sha, api(f"{prefix}/actions/runs/{run['id']}"))
    require(api(f"{prefix}/git/ref/heads/main")["object"]["sha"] == sha, "main moved")
    subprocess.run([sys.executable, "scripts/write_sha256sums.py", "--check", "dist"], check=True)
    require({path.name for path in Path("dist").iterdir()} == set(ASSETS), "unexpected local release assets")

    if api(f"{prefix}/git/ref/tags/{TAG}", optional=True) is None:
        tag = api(
            f"{prefix}/git/tags",
            {"tag": TAG, "message": f"{TAG} durable Desktop client and broker", "object": sha, "type": "commit"},
        )
        api(f"{prefix}/git/refs", {"ref": f"refs/tags/{TAG}", "sha": tag["sha"]})
    verify_tag(prefix, sha)
    release = find_release(prefix)
    if release is None:
        gh(
            "release",
            "create",
            TAG,
            "--verify-tag",
            "--draft",
            "--prerelease",
            "--title",
            "v0.6.0b1 — durable Desktop client and broker",
            "--notes-file",
            "docs/release-notes-v0.6.0b1.md",
        )
        release = find_release(prefix)
        require(release is not None, "draft not visible after creation")
    if release is None:  # Keep the narrowed type explicit for static tooling.
        raise RuntimeError("missing release")
    if not release["draft"]:
        require(release["prerelease"], "existing release is not a prerelease")
        require({asset["name"] for asset in release["assets"]} == set(ASSETS), "published asset set mismatch")
        # Already-published assets are immutable. Tooling drift must not require a
        # new build to be byte-identical to that verified historical artifact.
        with tempfile.TemporaryDirectory() as directory:
            gh("release", "download", TAG, "--dir", directory)
            verify_download(Path(directory))
        verify_tag(prefix, sha)
        require(api(f"{prefix}/git/ref/heads/main")["object"]["sha"] == sha, "main moved before verification")
        print(release["html_url"])
        return
    existing = {asset["name"]: asset for asset in release["assets"]}
    require(set(existing) <= set(ASSETS), "unexpected remote assets")
    for name in ASSETS:
        local = Path("dist") / name
        if name in existing:
            require(existing[name].get("digest") == "sha256:" + digest(local), f"existing asset differs: {name}")
        else:
            require(release["draft"], "never modify published release assets")
            gh("release", "upload", TAG, str(local))
    with tempfile.TemporaryDirectory() as directory:
        gh("release", "download", TAG, "--dir", directory)
        require({path.name for path in Path(directory).iterdir()} == set(ASSETS), "download asset set mismatch")
        for name in ASSETS:
            require(digest(Path(directory) / name) == digest(Path("dist") / name), f"download differs: {name}")
        subprocess.run([sys.executable, "scripts/write_sha256sums.py", "--check", directory], check=True)
    # Check immutable identity and current main again before making the draft public.
    verify_tag(prefix, sha)
    require(api(f"{prefix}/git/ref/heads/main")["object"]["sha"] == sha, "main moved before publication")
    if release["draft"]:
        gh("release", "edit", TAG, "--draft=false", "--prerelease", "--latest=false", "--verify-tag")
    published = api(f"{prefix}/releases/tags/{TAG}")
    require(not published["draft"] and published["prerelease"], "release is not published")
    require({asset["name"] for asset in published["assets"]} == set(ASSETS), "published asset set mismatch")
    verify_tag(prefix, sha)
    print(published["html_url"])


if __name__ == "__main__":
    main()
