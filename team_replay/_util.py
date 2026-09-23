"""Small deterministic helpers shared inside the Replay module."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import PurePosixPath
from typing import Any, Iterable, Mapping

_LIST_PREFIX_RE = re.compile(r"^\s*(?:[-*+]|\d+[.)])\s+")


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
    value = str(rel_path or "").strip().replace("\\", "/")
    parts = PurePosixPath(value).parts
    if not value or not parts or PurePosixPath(value).is_absolute() or re.match(r"^[A-Za-z]:", value) or any(part in {"", ".", ".."} for part in parts):
        raise ValueError(f"Unsafe artifact path: {rel_path!r}")
    return "/".join(parts)


def flatten_requirements(raw: Any) -> list[str]:
    values = raw if isinstance(raw, (list, tuple)) else [raw]
    result: list[str] = []
    seen: set[str] = set()
    for value in values:
        if isinstance(value, Mapping):
            value = value.get("text") or value.get("requirement") or ""
        for line in str(value or "").splitlines():
            text = _LIST_PREFIX_RE.sub("", line).strip()
            if text and text not in seen:
                seen.add(text)
                result.append(text)
    return result


def checklist_items(
    requirements: Iterable[Any],
    trajectory_requirements: Iterable[Any] = (),
) -> list[dict[str, Any]]:
    output = flatten_requirements(list(requirements))
    trajectory = flatten_requirements(list(trajectory_requirements))
    return [
        *[
            {"id": f"R{index:02d}", "text": text, "kind": "output"}
            for index, text in enumerate(output, start=1)
        ],
        *[
            {"id": f"T{index:02d}", "text": text, "kind": "trajectory"}
            for index, text in enumerate(trajectory, start=1)
        ],
    ]
