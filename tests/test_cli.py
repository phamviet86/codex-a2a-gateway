from __future__ import annotations

import subprocess
import sys

from codex_a2a_gateway import __version__


def test_cli_reports_package_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "codex_a2a_gateway.cli", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == f"cli.py {__version__}"
