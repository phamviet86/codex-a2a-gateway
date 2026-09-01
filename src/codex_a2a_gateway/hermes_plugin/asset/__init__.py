"""Hermes standalone-plugin entry point."""

from typing import Any


def register(ctx: Any) -> None:
    """Register the five durable client tools for Hermes standalone loading."""
    # ``kind: standalone`` plugins are loaded through this entry point; Hermes
    # does not auto-import ``provides_tools`` in that mode.
    from .tools import register_tools

    register_tools(ctx)
