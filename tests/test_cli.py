from __future__ import annotations

import subprocess
import sys

from codex_a2a_gateway import __version__
from codex_a2a_gateway.server import mcp


def test_cli_reports_package_version() -> None:
    completed = subprocess.run(
        [sys.executable, "-m", "codex_a2a_gateway.cli", "--version"],
        check=True,
        capture_output=True,
        text=True,
    )
    assert completed.stdout.strip() == f"cli.py {__version__}"


def test_mcp_metadata_reports_package_version() -> None:
    assert mcp.version == __version__
