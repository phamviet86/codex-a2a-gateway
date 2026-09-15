from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from codex_a2a_gateway.skill_install import MANIFEST, SKILLS, default_skill_root, install_skills


def snapshot(root: Path) -> dict[str, tuple[bytes, int]]:
    return {
        path.relative_to(root).as_posix(): (path.read_bytes(), path.stat().st_mtime_ns)
        for path in root.rglob("*")
        if path.is_file()
    }


def test_install_is_idempotent_and_preserves_unrelated_files(tmp_path: Path) -> None:
    root = tmp_path / "skills with spaces"
    report, code = install_skills(root)
    assert code == 0 and report["ok"]
    unrelated = root / SKILLS[0] / "personal-notes.txt"
    unrelated.write_text("keep this")
    other = root / "other-skill"
    other.mkdir()
    (other / "SKILL.md").write_text("unrelated")
    before = snapshot(root)
    assert install_skills(root)[1] == 0
    assert install_skills(root, check=True)[1] == 0
    assert snapshot(root) == before
    runtime = json.loads((root / SKILLS[0] / "references/runtime.json").read_text())
    assert runtime["command"] == [os.path.abspath(sys.executable), "-m", "codex_a2a_gateway.cli"]
    completed = subprocess.run(
        [*runtime["command"], "--version"], cwd=tmp_path, capture_output=True, text=True, check=True
    )
    assert "0.5.1" in completed.stdout


@pytest.mark.parametrize("flags", [{"check": True}, {"dry_run": True}])
def test_inspection_never_creates_destination(tmp_path: Path, flags: dict[str, bool]) -> None:
    root = tmp_path / "absent" / "skills"
    report, code = install_skills(root, **flags)
    assert code == (2 if flags.get("check") else 0)
    assert all(item["state"] == "missing" for item in report["skills"])
    assert not root.parent.exists()


def test_conflict_preflight_and_managed_replacement(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    assert install_skills(root)[1] == 0
    modified = root / SKILLS[1] / "SKILL.md"
    modified.write_text("user change")
    unrelated = root / SKILLS[1] / "unrelated.txt"
    unrelated.write_text("keep")
    before = snapshot(root)
    assert install_skills(root)[1] == 2
    assert install_skills(root, check=True)[1] == 2
    assert install_skills(root, dry_run=True, replace=True)[1] == 0
    assert snapshot(root) == before
    assert install_skills(root, replace=True)[1] == 0
    assert modified.read_text().startswith("---")
    assert unrelated.read_text() == "keep"
    assert install_skills(root, check=True)[1] == 0


def test_unmanaged_conflict_blocks_all_writes_even_with_replace(tmp_path: Path) -> None:
    root = tmp_path / "skills"
    target = root / SKILLS[1]
    target.mkdir(parents=True)
    (target / "SKILL.md").write_text("not ours")
    before = snapshot(root)
    assert install_skills(root, replace=True)[1] == 2
    assert not (root / SKILLS[0]).exists()
    assert snapshot(root) == before


@pytest.mark.parametrize("link_kind", ["target", "managed-file", "managed-parent", "root", "manifest"])
def test_symlinks_are_never_followed_for_managed_writes(tmp_path: Path, link_kind: str) -> None:
    root = tmp_path / "skills"
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "sentinel").write_text("safe")
    if link_kind == "root":
        root.symlink_to(outside, target_is_directory=True)
    elif link_kind == "target":
        root.mkdir()
        (root / SKILLS[0]).symlink_to(outside, target_is_directory=True)
    else:
        install_skills(root)
        target = root / SKILLS[0]
        if link_kind == "managed-file":
            (target / "SKILL.md").unlink()
            (target / "SKILL.md").symlink_to(outside / "sentinel")
        elif link_kind == "manifest":
            (target / MANIFEST).unlink()
            (target / MANIFEST).symlink_to(outside / "sentinel")
        else:
            import shutil

            shutil.rmtree(target / "references")
            (target / "references").symlink_to(outside, target_is_directory=True)
    before = snapshot(outside)
    assert install_skills(root, replace=True)[1] == 2
    assert snapshot(outside) == before


def test_default_destination_contract(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.delenv("CODEX_HOME", raising=False)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    assert default_skill_root() == tmp_path / ".agents/skills"
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "custom codex"))
    assert default_skill_root() == tmp_path / "custom codex/skills"
