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

    with tempfile.TemporaryDirectory(prefix="hermes-a2a-wheel-") as temporary:
        venv_root = Path(temporary) / "venv"
        venv.EnvBuilder(with_pip=True).create(venv_root)
        python = executable(venv_root, "python")
        run(str(python), "-m", "pip", "--disable-pip-version-check", "install", str(wheel))
        run(
            str(python),
            "-c",
            "from importlib.metadata import version, distribution; import hermes_a2a_gateway; "
            "import importlib.util; "
            "installed = version('hermes-a2a-gateway'); "
            "assert installed == hermes_a2a_gateway.__version__; "
            "entrypoints = distribution('hermes-a2a-gateway').entry_points; "
            "assert {e.name for e in entrypoints if e.group == 'console_scripts'} == {'hermes-a2a-gateway'}; "
            "assert importlib.util.find_spec('codex_a2a_gateway') is None; "
            "print('installed', installed, hermes_a2a_gateway.__file__)",
        )
        command = str(executable(venv_root, "hermes-a2a-gateway"))
        run(command, "--help")
        run(command, "--version")
        for removed in ("serve", "gateway", "doctor", "smoke", "install-skills", "install-hermes-plugin"):
            result = subprocess.run([command, removed], capture_output=True, cwd=temporary)
            assert result.returncode == 2, f"removed command accepted: {removed}"
        assert subprocess.run([command], capture_output=True, cwd=temporary).returncode == 2
        # Exercise the installed new facade outside the checkout. Discovery needs
        # neither credentials nor a model; missing native metadata must fail closed.
        probe = """
import asyncio
import sys
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from hermes_a2a_gateway.broker import run_broker
from hermes_a2a_gateway.client import run_client, client_doctor

async def main():
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'hermes_a2a_gateway.cli', 'client-mcp'])
    async with stdio_client(params) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        tools = await session.list_tools()
        expected = {'gateway_submit', 'gateway_get', 'gateway_wait',
                    'gateway_cancel', 'gateway_upload_artifact'}
        assert {tool.name for tool in tools.tools} == expected
        result = await session.call_tool('gateway_get', {'operation_id': 'no-native-origin'})
        assert result.is_error is True
asyncio.run(main())
print('installed client MCP discovery and metadata refusal passed')
"""
        subprocess.run([str(python), "-c", probe], cwd=temporary, check=True)

    print(f"clean wheel install passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
