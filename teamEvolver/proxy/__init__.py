"""teamEvolver service package."""

from __future__ import annotations

from typing import TYPE_CHECKING

from .attribution import (
    _extract_modified_skills_from_tool_calls,
    _extract_read_skills_from_tool_calls,
)

if TYPE_CHECKING:
    from .server import ProxyServer

__all__ = [
    "ProxyServer",
    "_extract_read_skills_from_tool_calls",
    "_extract_modified_skills_from_tool_calls",
]


def __getattr__(name: str):
    if name == "ProxyServer":
        from .server import ProxyServer

        return ProxyServer
    raise AttributeError(name)
