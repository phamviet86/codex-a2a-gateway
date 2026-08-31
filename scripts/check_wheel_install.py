#!/usr/bin/env python3
"""Install a built wheel into a clean venv and exercise its public commands."""

from __future__ import annotations

import argparse
import os
import subprocess
import tempfile
import venv
from pathlib import Path


def executable(venv_root: Path, name: str) -> Path:
    directory = "Scripts" if os.name == "nt" else "bin"
    suffix = ".exe" if os.name == "nt" else ""
    return venv_root / directory / f"{name}{suffix}"


def run(*command: str) -> None:
    subprocess.run(command, check=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path)
    args = parser.parse_args()
    wheel = args.wheel.resolve(strict=True)

    with tempfile.TemporaryDirectory(prefix="codex-a2a-wheel-") as temporary:
        venv_root = Path(temporary) / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_root)
        python = executable(venv_root, "python")
        run(str(python), "-m", "pip", "--disable-pip-version-check", "install", str(wheel))
        run(
            str(python),
            "-c",
            "from importlib.metadata import version; import codex_a2a_gateway; "
            "print('installed', version('codex-a2a-gateway'), codex_a2a_gateway.__file__)",
        )
        for command in ("codex-a2a-gateway", "codex-hermes-a2a-bridge"):
            run(str(executable(venv_root, command)), "--help")
            run(str(executable(venv_root, command)), "--version")

    print(f"clean wheel install passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
