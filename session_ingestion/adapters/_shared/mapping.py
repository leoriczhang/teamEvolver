"""Shared helpers for applying tenant adapter mapping patches."""

from __future__ import annotations

from typing import Any


def merge_mapping(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    """Deep-merge a mapping patch over a converted Session or turn."""
    result = dict(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = merge_mapping(result[key], value)
        else:
            result[key] = value
    return result
