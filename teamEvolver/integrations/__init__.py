"""Integrations with external agent runtimes (Hermes)."""

from __future__ import annotations

from .hermes import (
    configure_hermes,
    inspect_hermes_config,
    restore_hermes_config,
)

__all__ = [
    "configure_hermes",
    "inspect_hermes_config",
    "restore_hermes_config",
]
