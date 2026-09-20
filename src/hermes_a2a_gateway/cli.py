"""Public entry points for the client/server gateway."""

from __future__ import annotations

import argparse
import json

from . import __version__


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Hermes A2A Gateway client/server transport")
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("broker", help="Run the authenticated PostgreSQL broker")
    commands.add_parser("client", help="Run the local daemon and optional managed SSH tunnel")
    commands.add_parser("client-mcp", help="Run the Desktop MCP facade over stdio")
    commands.add_parser("client-doctor", help="Inspect client transport and native delivery readiness")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    if args.command == "broker":
        from .broker import run_broker

        run_broker()
    elif args.command == "client":
        from .client import run_client

        run_client()
    elif args.command == "client-mcp":
        from .client_mcp import run_client_mcp

        run_client_mcp()
    elif args.command == "client-doctor":
        from .client import client_doctor

        result = client_doctor()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        raise SystemExit(0 if result.get("ok") else 1)


if __name__ == "__main__":
    main()
