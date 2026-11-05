"""Volcengine Managed Agents protocol; no domain dependencies."""

from .client import MAClient, MAError

__all__ = ["MAClient", "MAError"]
