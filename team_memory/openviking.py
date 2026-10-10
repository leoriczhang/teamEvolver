"""Small OpenViking identity helpers shared by Team Memory integrations."""

from __future__ import annotations

import base64


def parse_openviking_key(value: str) -> tuple[str, str]:
    """Return the account and user encoded in an OpenViking API key."""
    parts = str(value or "").strip().split(".")
    if len(parts) < 3:
        return "", ""
    decoded: list[str] = []
    for part in parts[:2]:
        try:
            padding = "=" * (-len(part) % 4)
            decoded.append(base64.b64decode(part + padding).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return decoded[0].strip(), decoded[1].strip()
