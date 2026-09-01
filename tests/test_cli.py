from __future__ import annotations

import subprocess
import sys
from importlib.metadata import version

from codex_a2a_gateway import __version__
from codex_a2a_gateway.server import mcp


def test_package_version_is_current_release_version() -> None:
    assert __version__ == "0.2.2"
    assert version("codex-a2a-gateway") == __version__


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
