#!/usr/bin/env python3
"""Check/apply/revert the exact, versioned Hermes compatibility patch.

Standalone release utility. Stop Hermes before apply/revert; the utility never
restarts services, alters credentials, or changes files outside the manifest.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
from pathlib import Path


class PatchError(ValueError):
    """A fixed, non-sensitive operator diagnostic."""


def digest(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def git(root: Path, *args: str) -> str:
    result = subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True, check=False)
    if result.returncode:
        raise PatchError("Git precondition or patch operation failed; no conflicting source may be overwritten")
    return result.stdout.strip()


def execute(root: Path, manifest_path: Path, patch_path: Path, action: str) -> str:
    root = root.resolve(strict=True)
    manifest = json.loads(manifest_path.read_text())
    if digest(patch_path) != manifest["patch_sha256"]:
        raise PatchError("Patch checksum mismatch")
    if git(root, "rev-parse", "HEAD") != manifest["upstream_commit"]:
        raise PatchError("Unsupported Hermes commit")
    paths = list(manifest["files"])
    if not paths:
        raise PatchError("Empty manifest")
    changed_paths = {
        line.split("\t")[-1] for line in git(root, "apply", "--numstat", str(patch_path.resolve())).splitlines()
    }
    if changed_paths != set(paths):
        raise PatchError("Patch paths do not match manifest")
    states = set()
    for relative, hashes in manifest["files"].items():
        path = root / relative
        if Path(relative).is_absolute() or ".." in Path(relative).parts:
            raise PatchError("Unsafe manifest path")
        if any(parent.is_symlink() for parent in [path, *path.parents] if parent != root.parent):
            raise PatchError("Symlink source is unsupported")
        current = digest(path)
        if current == hashes["original_sha256"]:
            states.add("original")
        elif current == hashes["patched_sha256"]:
            states.add("patched")
        else:
            raise PatchError("Conflicting Hermes source; preserve changes and resolve manually")
    if len(states) != 1:
        raise PatchError("Mixed patch state; resolve manually")
    if git(root, "diff", "--cached", "--name-only", "--", *paths):
        raise PatchError("Target files have staged changes")
    state = states.pop()
    if action == "check" or (action == "apply" and state == "patched") or (action == "revert" and state == "original"):
        return state
    if action not in {"apply", "revert"}:
        raise PatchError("Unsupported action")
    reverse = ["--reverse"] if action == "revert" else []
    git(root, "apply", "--check", *reverse, str(patch_path.resolve()))
    git(root, "apply", *reverse, str(patch_path.resolve()))
    expected = "patched" if action == "apply" else "original"
    for relative, hashes in manifest["files"].items():
        if digest(root / relative) != hashes[f"{expected}_sha256"]:
            raise PatchError("Post-apply checksum mismatch; stop before restarting Hermes")
    return expected


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["check", "apply", "revert"])
    parser.add_argument("--root", type=Path, required=True)
    here = Path(__file__).resolve().parent
    default = here if (here / "hermes-a2a-compat.json").exists() else here.parent / "compat" / "hermes"
    parser.add_argument("--manifest", type=Path, default=default / "hermes-a2a-compat.json")
    parser.add_argument("--patch", type=Path, default=default / "hermes-a2a-compat.patch")
    args = parser.parse_args()
    try:
        state = execute(args.root, args.manifest, args.patch, args.action)
    except (ValueError, OSError, KeyError, TypeError) as exc:
        # Source content and subprocess stderr are deliberately never printed.
        print(f"Patch refused: {str(exc) if isinstance(exc, PatchError) else type(exc).__name__}")
        return 1
    print(json.dumps({"state": state, "action": args.action}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
