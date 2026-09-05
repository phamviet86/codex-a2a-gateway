#!/usr/bin/env python3
"""Publish only v0.4.0 after its exact main commit passed the CI workflow."""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any

TAG = "v0.4.0"
ASSETS = ["codex_a2a_gateway-0.4.0-py3-none-any.whl", "codex_a2a_gateway-0.4.0.tar.gz", "SHA256SUMS"]


def gh(*args: str) -> str:
    return subprocess.check_output(["gh", *args], text=True).strip()


def api(path: str, data: dict[str, Any] | None = None, *, optional: bool = False) -> Any:
    command = ["gh", "api", path]
    if data is not None:
        command += ["--method", "POST", "--input", "-"]
    result = subprocess.run(command, input=json.dumps(data) if data else None, text=True, capture_output=True)
    if result.returncode:
        if optional and "(HTTP 404)" in result.stderr:
            return None
        raise RuntimeError(f"GitHub API failed for {path}: {result.stderr}")
    return json.loads(result.stdout)


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main() -> None:
    repo, sha = os.environ["GH_REPO"], os.environ["RELEASE_SHA"]
    assert repo == "phamviet86/codex-a2a-gateway"
    event = json.loads(Path(os.environ["GITHUB_EVENT_PATH"]).read_text())
    run = event["workflow_run"]
    assert os.environ["GITHUB_EVENT_NAME"] == "workflow_run"
    assert run["conclusion"] == "success" and run["event"] == "push" and run["head_branch"] == "main"
    assert run["head_repository"]["full_name"] == repo and run["head_sha"] == sha
    assert run["name"] == "CI"
    assert "[release v0.4.0]" in run["head_commit"]["message"]
    assert subprocess.check_output(["git", "rev-parse", "HEAD"], text=True).strip() == sha
    prefix = f"repos/{repo}"
    assert api(f"{prefix}/git/ref/heads/main")["object"]["sha"] == sha, "main moved; stop before publication"

    ref = api(f"{prefix}/git/ref/tags/{TAG}", optional=True)
    if ref is None:
        tag = api(
            f"{prefix}/git/tags", {"tag": TAG, "message": "v0.4.0 durability release", "object": sha, "type": "commit"}
        )
        ref = api(f"{prefix}/git/refs", {"ref": f"refs/tags/{TAG}", "sha": tag["sha"]})
    assert ref["object"]["type"] == "tag", "expected an annotated tag"
    target = api(f"{prefix}/git/tags/{ref['object']['sha']}")["object"]
    assert target["type"] == "commit" and target["sha"] == sha, "tag target mismatch"

    release = api(f"{prefix}/releases/tags/{TAG}", optional=True)
    if release is None:
        gh(
            "release",
            "create",
            TAG,
            "--verify-tag",
            "--draft",
            "--title",
            "v0.4.0 — Durable bidirectional jobs",
            "--notes-file",
            "docs/release-notes-v0.4.0.md",
        )
        release = api(f"{prefix}/releases/tags/{TAG}")
    existing = {asset["name"]: asset for asset in release["assets"]}
    assert set(existing) <= set(ASSETS), "unexpected release assets"
    for name in ASSETS:
        local = Path("dist") / name
        if name in existing:
            assert existing[name]["digest"] == "sha256:" + digest(local), f"existing asset differs: {name}"
        else:
            assert release["draft"], "never mutate assets of a published release"
            gh("release", "upload", TAG, str(local))
    with tempfile.TemporaryDirectory() as directory:
        gh("release", "download", TAG, "--dir", directory)
        for name in ASSETS:
            assert digest(Path(directory) / name) == digest(Path("dist") / name), f"download differs: {name}"
        subprocess.run(["python", "scripts/write_sha256sums.py", "--check", directory], check=True)
    if release["draft"]:
        assert api(f"{prefix}/git/ref/heads/main")["object"]["sha"] == sha, "main moved before publish"
        gh("release", "edit", TAG, "--draft=false", "--latest", "--verify-tag")
    published = api(f"{prefix}/releases/tags/{TAG}")
    assert not published["draft"] and not published["prerelease"]
    assert {asset["name"] for asset in published["assets"]} == set(ASSETS)
    print(published["html_url"])


if __name__ == "__main__":
    main()
