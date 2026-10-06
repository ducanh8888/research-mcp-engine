"""Capability routing and per-account availability."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .execute import Engine, ToolError

__all__ = ["Engine", "ToolError"]


def __getattr__(name: str):
    if name in __all__:
        from . import execute
        return getattr(execute, name)
    raise AttributeError(name)
