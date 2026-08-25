#!/usr/bin/env python3
"""Fail if built distributions contain local/runtime artifacts."""

from __future__ import annotations

import argparse
import tarfile
import zipfile
from pathlib import Path

FORBIDDEN_NAMES = {
    ".coverage",
    ".ds_store",
    ".env",
}
FORBIDDEN_PARTS = {
    ".git",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
}
FORBIDDEN_SUFFIXES = {
    ".bak",
    ".db",
    ".log",
    ".pem",
    ".pyc",
    ".sqlite",
    ".sqlite3",
}


def members(path: Path) -> list[str]:
    if path.suffix == ".whl":
        with zipfile.ZipFile(path) as archive:
            return archive.namelist()
    if path.name.endswith(".tar.gz"):
        with tarfile.open(path, "r:gz") as archive:
            return archive.getnames()
    raise ValueError(f"unsupported distribution: {path}")


def unsafe(name: str) -> bool:
    parts = {part.lower() for part in Path(name).parts}
    leaf = Path(name).name.lower()
    return bool(parts & FORBIDDEN_PARTS or leaf in FORBIDDEN_NAMES or Path(leaf).suffix in FORBIDDEN_SUFFIXES)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    args = parser.parse_args()
    distributions = sorted([*args.directory.glob("*.whl"), *args.directory.glob("*.tar.gz")])
    if not distributions:
        parser.error("no wheel or sdist found")
    violations = [(path.name, name) for path in distributions for name in members(path) if unsafe(name)]
    if violations:
        for distribution, name in violations:
            print(f"forbidden distribution member: {distribution}: {name}")
        return 1
    for path in distributions:
        print(f"checked {path.name}: {len(members(path))} members")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
