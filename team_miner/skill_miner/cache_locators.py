"""Portable host-only locators; Evidence and generated artifact bytes stay intact."""

from __future__ import annotations

from pathlib import Path

from .storage import file_path

_PREFIX = "mining-cache://"
_FIELDS = {"local_path", "compiler_path", "input_manifest", "staged_state_path"}


def locators(value, root: Path, job_id: str, *, portable: bool):
    if isinstance(value, list):
        return [locators(item, root, job_id, portable=portable) for item in value]
    if not isinstance(value, dict):
        return value
    result = {}
    marker = f"/mining_jobs/{job_id}/"
    for key, item in value.items():
        if key in _FIELDS and isinstance(item, str):
            if portable and Path(item).is_absolute() and marker in item:
                suffix = file_path(item.split(marker, 1)[1])
                item = _PREFIX + job_id + "/" + suffix
            elif not portable and item.startswith(_PREFIX):
                if not item.startswith(_PREFIX + job_id + "/"):
                    raise ValueError("mining cache locator belongs to another job")
                item = str(root / file_path(item[len(_PREFIX + job_id + "/"):]))
        else:
            item = locators(item, root, job_id, portable=portable)
        result[key] = item
    return result
