"""teamEvolver service package."""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from .server import ProxyServer

__all__ = ["ProxyServer"]


def __getattr__(name: str):
    if name == "ProxyServer":
        from .server import ProxyServer

        return ProxyServer
    raise AttributeError(name)
