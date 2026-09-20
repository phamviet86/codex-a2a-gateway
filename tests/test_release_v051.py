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


@pytest.fixture(params=["v051", "v060"])
def publisher(request: pytest.FixtureRequest) -> ModuleType:
    path = Path(__file__).parents[1] / f"scripts/publish_{request.param}.py"
    spec = importlib.util.spec_from_file_location("publish_v051", path)
    assert spec and spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def valid_run(version: str = "0.5.1") -> dict[str, Any]:
    return {
        "id": 123,
        "name": "CI",
        "status": "completed",
        "conclusion": "success",
        "event": "push",
        "head_branch": "main",
        "head_repository": {"full_name": "phamviet86/hermes-a2a-gateway"},
        "head_sha": "a" * 40,
        "head_commit": {"message": f"Publish [release v{version}]"},
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
    run = valid_run(publisher.VERSION)
    publisher.validate_run(publisher.REPOSITORY, "a" * 40, run)
    run[field] = value
    with pytest.raises(RuntimeError):
        publisher.validate_run(publisher.REPOSITORY, "a" * 40, run)


def test_publisher_rejects_a_foreign_repository(publisher: ModuleType) -> None:
    with pytest.raises(RuntimeError, match="repository"):
        publisher.validate_run("fork/project", "a" * 40, valid_run(publisher.VERSION))


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
    event.write_text(json.dumps({"workflow_run": valid_run(publisher.VERSION)}))
    monkeypatch.setenv("GITHUB_EVENT_PATH", str(event))
    (tmp_path / "pyproject.toml").write_text(f'[project]\nversion = "{publisher.VERSION}"\n')
    monkeypatch.setattr("codex_a2a_gateway.__version__", publisher.VERSION)
    dist = tmp_path / "dist"
    dist.mkdir()
    for name in publisher.ASSETS:
        (dist / name).write_text("fixture " + name)
    calls: list[str] = []
    release = {
        "draft": mode not in {"published", "published-missing-asset"},
        "prerelease": "b" in publisher.VERSION,
        "assets": [{"name": name} for name in publisher.ASSETS] if mode == "published" else [],
        "html_url": "fixture",
    }
    main_reads = 0

    def fake_api(path: str, *args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal main_reads
        if "/actions/runs/" in path:
            return copy.deepcopy(valid_run(publisher.VERSION))
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
                "prerelease": "b" in publisher.VERSION,
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


@pytest.mark.parametrize("version", ["current", "0.4.0"])
def test_existing_download_checks_distribution_version(publisher: ModuleType, tmp_path: Path, version: str) -> None:
    version = publisher.VERSION if version == "current" else version
    with zipfile.ZipFile(tmp_path / publisher.ASSETS[0], "w") as wheel:
        wheel.writestr("codex_a2a_gateway-0.5.1.dist-info/METADATA", f"Name: codex-a2a-gateway\nVersion: {version}\n")
    with tarfile.open(tmp_path / publisher.ASSETS[1], "w:gz") as archive:
        content = f'[project]\nname = "codex-a2a-gateway"\nversion = "{publisher.VERSION}"\n'.encode()
        info = tarfile.TarInfo("codex_a2a_gateway-0.5.1/pyproject.toml")
        info.size = len(content)
        archive.addfile(info, io.BytesIO(content))
    (tmp_path / "SHA256SUMS").write_text(
        "".join(f"{publisher.digest(tmp_path / name)}  {name}\n" for name in sorted(publisher.ASSETS[:2]))
    )
    if version == publisher.VERSION:
        publisher.verify_download(tmp_path)
    else:
        with pytest.raises(RuntimeError, match="wheel version mismatch"):
            publisher.verify_download(tmp_path)


def test_release_workflow_checks_out_successful_main_push_sha(publisher: ModuleType) -> None:
    root = Path(__file__).parents[1]
    if not (root / ".git").exists() and (root / "PKG-INFO").is_file():
        pytest.skip("release workflow is checkout-only and excluded from the sdist")
    workflow = (root / f".github/workflows/release-v{publisher.VERSION}.yml").read_text()
    for guard in (
        "workflow_run:",
        "workflows: [CI]",
        "github.event.workflow_run.conclusion == 'success'",
        "github.event.workflow_run.event == 'push'",
        "github.event.workflow_run.head_branch == 'main'",
        "github.event.workflow_run.head_repository.full_name == github.repository",
        f"contains(github.event.workflow_run.head_commit.message, '[release v{publisher.VERSION}]')",
        "ref: ${{ github.event.workflow_run.head_sha }}",
        'test "$(git rev-parse HEAD)" = "$RELEASE_SHA"',
        f"run: python scripts/{Path(publisher.__file__).name}",
    ):
        assert guard in workflow
