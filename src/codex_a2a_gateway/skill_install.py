"""Install packaged guidance without depending on a checkout or executable PATH."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import sys
import tempfile
from importlib import resources
from pathlib import Path
from typing import Any

from . import __version__

SKILLS = ("codex-a2a-setup", "codex-a2a")
MANIFEST = ".codex-a2a-managed.json"
DISTRIBUTION = "codex-a2a-gateway"


def default_skill_root() -> Path:
    configured = os.environ.get("CODEX_HOME")
    return Path(configured).expanduser() / "skills" if configured else Path.home() / ".agents" / "skills"


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _runtime() -> bytes:
    # resolve() would turn a venv symlink into the system Python and lose the install.
    command = [str(Path(sys.executable).absolute()), "-m", "codex_a2a_gateway.cli"]
    return (
        json.dumps({"distribution": DISTRIBUTION, "version": __version__, "command": command}, indent=2) + "\n"
    ).encode()


def _safe_relative(value: str) -> bool:
    path = Path(value)
    return bool(value) and not path.is_absolute() and ".." not in path.parts and value != MANIFEST


def _has_symlink(path: Path, root: Path) -> bool:
    return any(item.is_symlink() for item in (path, *path.parents) if item == root or root in item.parents)


def _old_manifest(target: Path, name: str) -> dict[str, str] | None:
    marker = target / MANIFEST
    if marker.is_symlink():
        return None
    try:
        data = json.loads(marker.read_text())
        files = data["files"]
        if data["distribution"] != DISTRIBUTION or data["skill"] != name or not isinstance(files, dict):
            return None
        if not files or not all(
            isinstance(k, str) and _safe_relative(k) and isinstance(v, str) for k, v in files.items()
        ):
            return None
        return files
    except (OSError, ValueError, KeyError, TypeError):
        return None


def _files(name: str) -> dict[str, bytes]:
    asset = resources.files("codex_a2a_gateway") / "skills" / name
    with resources.as_file(asset) as source:
        files = {path.relative_to(source).as_posix(): path.read_bytes() for path in source.rglob("*") if path.is_file()}
    files["references/runtime.json"] = _runtime()
    return files


def install_skills(
    destination: Path | None = None, *, check: bool = False, dry_run: bool = False, replace: bool = False
) -> tuple[dict[str, Any], int]:
    root = (destination or default_skill_root()).expanduser().absolute()
    report: dict[str, Any] = {"destination": str(root), "check": check, "dry_run": dry_run, "skills": []}
    if root.is_symlink() or (root.exists() and not root.is_dir()):
        return {**report, "ok": False, "error": "Skill root must be a directory, not a symlink or file."}, 2
    plans: list[tuple[str, Path, dict[str, bytes], dict[str, str] | None, str]] = []
    for name in SKILLS:
        target = root / name
        files = _files(name)
        old = _old_manifest(target, name) if target.is_dir() and not target.is_symlink() else None
        state = "missing"
        reason = ""
        if target.is_symlink() or (target.exists() and not target.is_dir()):
            state, reason = "conflict", "target is a symlink or file"
        elif target.exists():
            if old is None:
                state, reason = "conflict", "existing directory is not managed by this installer"
            elif any(_has_symlink(target / path, target) for path in set(files) | set(old)):
                state, reason = "conflict", "managed path contains a symlink"
            elif any((target / path).exists() and not (target / path).is_file() for path in files):
                state, reason = "conflict", "managed file path is occupied by a directory"
            elif set(old) == set(files) and all(
                (target / path).is_file() and (target / path).read_bytes() == data for path, data in files.items()
            ):
                state = "unchanged"
            elif replace:
                state = "replace"
            else:
                state, reason = "conflict", "managed content differs; review then use --replace"
        plans.append((name, target, files, old, state))
        report["skills"].append({"name": name, "state": state, **({"reason": reason} if reason else {})})
    blocked = any(plan[-1] == "conflict" for plan in plans)
    report["ok"] = not blocked and (not check or all(plan[-1] == "unchanged" for plan in plans))
    if blocked or check or dry_run:
        return report, 0 if report["ok"] else 2
    root.mkdir(parents=True, exist_ok=True)
    for name, target, files, old, state in plans:
        if state == "unchanged":
            continue
        with tempfile.TemporaryDirectory(prefix=f".{name}-", dir=root) as temporary:
            staged = Path(temporary) / "staged"
            backup = Path(temporary) / "previous"
            if target.exists():
                shutil.copytree(target, staged, symlinks=True)
            else:
                staged.mkdir()
            # Keep unrelated additions. Remove obsolete managed files only when unchanged.
            for path, digest in (old or {}).items():
                candidate = staged / path
                if path not in files and candidate.is_file() and _digest(candidate.read_bytes()) == digest:
                    candidate.unlink()
            for path, data in files.items():
                output = staged / path
                output.parent.mkdir(parents=True, exist_ok=True)
                output.write_bytes(data)
            manifest = {"distribution": DISTRIBUTION, "skill": name, "files": {p: _digest(b) for p, b in files.items()}}
            (staged / MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
            if target.exists():
                target.rename(backup)
            try:
                staged.rename(target)
            except OSError:
                if backup.exists():
                    backup.rename(target)
                raise
    return report, 0
