#!/usr/bin/env python3
"""Write or verify SHA256SUMS for distributions and pinned compatibility assets."""

from __future__ import annotations

import argparse
import hashlib
from pathlib import Path


def digest(path: Path) -> str:
    hasher = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def distributions(directory: Path) -> list[Path]:
    items = sorted([*directory.glob("*.whl"), *directory.glob("*.tar.gz")])
    if (
        len([item for item in items if item.suffix == ".whl"]) != 1
        or len([item for item in items if item.name.endswith(".tar.gz")]) != 1
    ):
        raise ValueError("expected exactly one wheel and one source distribution")
    extras = [directory / name for name in ("hermes-a2a-compat.patch", "hermes-a2a-compat.json", "hermes_patch.py")]
    if any(path.exists() for path in extras):
        if not all(path.is_file() for path in extras):
            raise ValueError("incomplete Hermes compatibility asset set")
        items.extend(extras)
    return sorted(items)


def manifest_text(items: list[Path]) -> str:
    return "".join(f"{digest(item)}  {item.name}\n" for item in items)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("directory", type=Path)
    parser.add_argument("--check", action="store_true", help="verify an existing SHA256SUMS manifest")
    args = parser.parse_args()
    directory = args.directory.resolve(strict=True)
    manifest = directory / "SHA256SUMS"
    expected = manifest_text(distributions(directory))

    if args.check:
        if not manifest.is_file() or manifest.read_text(encoding="utf-8") != expected:
            print(f"SHA256SUMS does not match release distributions in {directory}")
            return 1
        print(f"verified {manifest}")
        return 0

    manifest.write_text(expected, encoding="utf-8")
    print(f"wrote {manifest}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
