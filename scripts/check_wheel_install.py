#!/usr/bin/env python3
"""Install a built wheel into a clean venv and exercise its public commands."""

from __future__ import annotations

import argparse
import json
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
            "from importlib import resources; "
            "installed = version('codex-a2a-gateway'); "
            "assert installed == codex_a2a_gateway.__version__, (installed, codex_a2a_gateway.__version__); "
            "asset = resources.files('codex_a2a_gateway.hermes_plugin') / 'asset'; "
            "assert (asset / 'plugin.yaml').is_file() and (asset / 'tools.py').is_file(); "
            "print('installed', installed, codex_a2a_gateway.__file__)",
        )
        for command in ("codex-a2a-gateway", "codex-hermes-a2a-bridge"):
            run(str(executable(venv_root, command)), "--help")
            run(str(executable(venv_root, command)), "--version")
        # Exercise the installed new facade outside the checkout. Discovery needs
        # neither credentials nor a model; missing native metadata must fail closed.
        probe = """
import asyncio
import sys
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
from codex_a2a_gateway.broker import run_broker
from codex_a2a_gateway.client import run_client, client_doctor

async def main():
    params = StdioServerParameters(command=sys.executable,
        args=['-m', 'codex_a2a_gateway.cli', 'client-mcp'])
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
        # Run outside the checkout, with an isolated skill root and no user config mutation.
        skill_root = Path(temporary) / "isolated skills"
        command = str(executable(venv_root, "codex-a2a-gateway"))
        arguments = [command, "install-skills", "--dest", str(skill_root)]
        subprocess.run([*arguments, "--dry-run"], cwd=temporary, check=True)
        assert not skill_root.exists()
        subprocess.run(arguments, cwd=temporary, check=True)
        subprocess.run(arguments, cwd=temporary, check=True)
        subprocess.run([*arguments, "--check"], cwd=temporary, check=True)
        for name in ("codex-a2a-setup", "codex-a2a"):
            runtime = json.loads((skill_root / name / "references/runtime.json").read_text())
            runtime_python = Path(runtime["command"][0])
            # macOS may spell the same temp root /var or /private/var. Resolve
            # only the parent: resolving the interpreter can erase venv identity.
            assert runtime_python.is_absolute()
            assert (runtime_python.parent.resolve(strict=True), runtime_python.name) == (
                python.parent.resolve(strict=True),
                python.name,
            )
            subprocess.run([*runtime["command"], "--version"], cwd=temporary, check=True)

    print(f"clean wheel install passed: {wheel.name}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
