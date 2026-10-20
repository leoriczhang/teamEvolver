"""Text decoding shared by the Skill miner's domain and platform boundary."""

from __future__ import annotations


def _content_text(result) -> str:
    if isinstance(result, dict):
        return str(result.get("content") or result.get("text") or "")
    return str(result or "")


def content_editable(uri: str) -> bool:
    return uri.lower().endswith((".md", ".markdown", ".txt", ".json", ".yaml", ".yml", ".csv"))
