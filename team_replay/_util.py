"""Small deterministic helpers shared inside the Replay module."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any


def stable_hash(value: Any) -> str:
    if isinstance(value, (dict, list)):
        raw = json.dumps(
            value,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    else:
        raw = str(value or "")
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def normalize_artifact_rel_path(rel_path: str) -> str:
    if not isinstance(rel_path, str):
        raise ValueError("Artifact path must be text")
    value = rel_path.strip()
    parts = PurePosixPath(value).parts
    if (
        not value or not parts or PurePosixPath(value).is_absolute() or re.match(r"^[A-Za-z]:", value)
        or "\\" in value or ":" in value
        or any(part in {"", ".", ".."} or part.startswith(".") for part in value.split("/"))
    ):
        raise ValueError(f"Unsafe artifact path: {rel_path!r}")
    return "/".join(parts)
