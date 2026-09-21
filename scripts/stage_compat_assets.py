#!/usr/bin/env python3
"""Stage pinned Hermes compatibility assets alongside the release distributions."""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from pathlib import Path

ASSETS = {
    "hermes-a2a-compat.patch": "compat/hermes/hermes-a2a-compat.patch",
    "hermes-a2a-compat.json": "compat/hermes/hermes-a2a-compat.json",
    "hermes_patch.py": "scripts/hermes_patch.py",
}


def stage(directory: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    manifest = json.loads((root / ASSETS["hermes-a2a-compat.json"]).read_text())
    patch_digest = hashlib.sha256((root / ASSETS["hermes-a2a-compat.patch"]).read_bytes()).hexdigest()
    if manifest["patch_sha256"] != patch_digest:
        raise ValueError("Hermes patch does not match compatibility manifest")
    directory.mkdir(parents=True, exist_ok=True)
    for name, source in ASSETS.items():
        target = directory / name
        content = (root / source).read_bytes()
        if target.exists() and target.read_bytes() != content:
            raise ValueError(f"refusing to overwrite different compatibility asset: {name}")
        shutil.copyfile(root / source, target)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("directory", type=Path)
    stage(parser.parse_args().directory)
