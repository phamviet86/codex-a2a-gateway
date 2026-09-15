from __future__ import annotations

import copy
import importlib.util
import io
import json
import shutil
import subprocess
import tarfile
import zipfile
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest


@pytest.fixture
def publisher() -> ModuleType:
    path = Path(__file__).parents[1] / "scripts/publish_v051.py"
    spec = importlib.util.spec_from_file_location("publish_v051", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_run() -> dict[str, Any]:
    return {
        "id": 123,
        "name": "CI",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "head_repository": {"full_name": "phamviet86/codex-a2a-gateway"},
        "head_sha": "a" * 40,
        "head_commit": {"message": "Fix Hermes continuation [release v0.5.1]"},
    }


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("name", "Other"),
        ("status", "in_progress"),
        ("conclusion", "failure"),
        ("event", "pull_request"),
        ("head_branch", "feature"),
        ("head_repository", {"full_name": "fork/project"}),
        ("head_sha", "b" * 40),
        ("head_commit", {"message": "not a release"}),
    ],
)
def test_publisher_rejects_unproven_ci(publisher: ModuleType, field: str, value: Any) -> None:
    run = valid_run()
    publisher.validate_run(publisher.REPOSITORY, "a" * 40, run)
    run[field] = value
    with pytest.raises(RuntimeError):
        publisher.validate_run(publisher.REPOSITORY, "a" * 40, run)


def test_publisher_rejects_a_foreign_repository(publisher: ModuleType) -> None:
    with pytest.raises(RuntimeError, match="repository"):
        publisher.validate_run("fork/project", "a" * 40, valid_run())


def test_publisher_never_retargets_an_existing_tag(publisher: ModuleType, monkeypatch: pytest.MonkeyPatch) -> None:
    responses = iter(
        [{"object": {"type": "tag", "sha": "tag-object"}}, {"object": {"type": "commit", "sha": "b" * 40}}]
    )
    monkeypatch.setattr(publisher, "api", lambda path: next(responses))
    with pytest.raises(RuntimeError, match="tag target differs"):
        publisher.verify_tag("repos/fixture", "a" * 40)


@pytest.mark.parametrize("mode", ["draft", "published", "published-missing-asset", "main-moved"])
def test_publication_orders_verification_before_publish(
    publisher: ModuleType, monkeypatch: pytest.MonkeyPatch, tmp_path: Path, mode: str
) -> None:
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("GH_REPO", publisher.REPOSITORY)
    monkeypatch.setenv("RELEASE_SHA", "a" * 40)
    monkeypatch.setenv("GITHUB_EVENT_NAME", "workflow_run")
    event = tmp_path / "event.json"
    event.write_text(json.dumps({"workflow_run": valid_run()}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    (tmp_path / "pyproject.toml").write_text('[project]\nversion = "0.5.1"\n')
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in publisher.ASSETS:
        (dist / name).write_text("fixture " + name)
    calls: list[str] = []
    release = {
        "draft": mode not in {"published", "published-missing-asset"},
        "prerelease": False,
        "assets": [{"name": name} for name in publisher.ASSETS] if mode == "published" else [],
        "html_url": "fixture",
    }
    main_reads = 0

    def fake_api(path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal main_reads
        if "/actions/runs/" in path:
            return copy.deepcopy(valid_run())
        if path.endswith("/git/ref/heads/main"):
            main_reads += 1
            sha = "b" * 40 if mode == "main-moved" and main_reads == 2 else "a" * 40
            return {"object": {"sha": sha}}
        if "/git/ref/tags/" in path:
            return {"object": {"type": "tag", "sha": "annotated"}}
        if path.endswith("/git/tags/annotated"):
            return {"object": {"type": "commit", "sha": "a" * 40}}
        if "/releases/tags/" in path:
            return {
                "draft": False,
                "prerelease": False,
                "assets": [{"name": n} for n in publisher.ASSETS],
                "html_url": "fixture",
            }
        raise AssertionError(path)

    def fake_gh(*args: str) -> str:
        calls.append(" ".join(args[:2]))
        if args[1] == "download":
            target = Path(args[args.index("--dir") + 1])
            for name in publisher.ASSETS:
                shutil.copyfile(dist / name, target / name)
        return ""

    def fake_run(command: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert command[1:3] == ["scripts/write_sha256sums.py", "--check"]
        calls.append("checksum " + command[-1])
        return subprocess.CompletedProcess(command, 0)

    monkeypatch.setattr(publisher, "api", fake_api)
    monkeypatch.setattr(publisher, "gh", fake_gh)
    monkeypatch.setattr(publisher, "find_release", lambda prefix: release)
    monkeypatch.setattr(publisher, "verify_download", lambda directory: calls.append("verify published download"))
    monkeypatch.setattr(publisher.subprocess, "check_output", lambda *a, **kw: "a" * 40 + "\n")
    monkeypatch.setattr(publisher.subprocess, "run", fake_run)
    if mode == "published":
        publisher.main()
        assert "verify published download" in calls
        assert "release upload" not in calls and "release edit" not in calls
    elif mode == "draft":
        publisher.main()
        assert calls.count("release upload") == 3
        assert calls.index("release download") < calls.index("release edit")
        assert calls[-2].startswith("checksum ") and calls[-1] == "release edit"
    else:
        expected = "published asset set mismatch" if mode == "published-missing-asset" else "main moved"
        with pytest.raises(RuntimeError, match=expected):
            publisher.main()
        assert "release edit" not in calls
        if mode == "published-missing-asset":
            assert "release upload" not in calls


@pytest.mark.parametrize("version", ["0.5.1", "0.4.0"])
def test_existing_download_checks_distribution_version(publisher: ModuleType, tmp_path: Path, version: str) -> None:
    with zipfile.ZipFile(tmp_path / publisher.ASSETS[0], "w") as wheel:
        wheel.writestr("codex_a2a_gateway-0.5.1.dist-info/METADATA", f"Name: codex-a2a-gateway\nVersion: {version}\n")
    with tarfile.open(tmp_path / publisher.ASSETS[1], "w:gz") as archive:
        content = b'[project]\nname = "codex-a2a-gateway"\nversion = "0.5.1"\n'
        info = tarfile.TarInfo("codex_a2a_gateway-0.5.1/pyproject.toml")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    (tmp_path / "SHA256SUMS").write_text(
        "".join(f"{publisher.digest(tmp_path / name)}  {name}\n" for name in sorted(publisher.ASSETS[:2]))
    )
    if version == "0.5.1":
        publisher.verify_download(tmp_path)
    else:
        with pytest.raises(RuntimeError, match="wheel version mismatch"):
            publisher.verify_download(tmp_path)


def test_release_workflow_checks_out_successful_main_push_sha() -> None:
    root = Path(__file__).parents[1]
    if not (root / ".git").exists() and (root / "PKG-INFO").is_file():
        pytest.skip("release workflow is checkout-only and excluded from the sdist")
    workflow = (root / ".github/workflows/release-v0.5.1.yml").read_text()
    for guard in (
        "workflow_run:",
        "workflows: [CI]",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        "contains(github.event.workflow_run.head_commit.message, '[release v0.5.1]')",
        "ref: ${{ github.event.workflow_run.head_sha }}",
        'test "$(git rev-parse HEAD)" = "$RELEASE_SHA"',
        "run: python scripts/publish_v051.py",
    ):
        assert guard in workflow
