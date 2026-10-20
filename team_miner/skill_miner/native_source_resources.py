"""Resolve declared source copies from verified frozen text, never from live OV.

This module does not choose sources or interpret their business content. The
compiler declares which resource to retain; the host only binds its exact URI
to this job's immutable input bytes.
"""

from __future__ import annotations

import hashlib
from pathlib import Path


def frozen_source_resources(root: Path, job: dict, through_round: int) -> dict[str, bytes]:
    from .ov_compile_mining import RUNS_ROOT
    from .ov_round_orchestration import _cached_inputs

    root = Path(root).resolve()
    result: dict[str, bytes] = {}
    for number in range(1, through_round + 1):
        directory = root / "round_inputs" / f"round-{number}"
        entries = _cached_inputs(directory)
        if entries is None:
            raise ValueError("原文资源装配缺少本任务的冻结来源清单")
        for item in entries:
            if item.get("kind") != "source":
                continue  # Human answers and generated context are not original source copies.
            path = Path(item["local_path"])
            if (path.is_symlink() or not path.is_file() or directory.resolve() not in path.resolve().parents
                    or any(parent.is_symlink() for parent in path.parents
                           if parent != root and root in parent.parents)):
                raise ValueError("原文资源要求本任务内的普通冻结文件")
            raw = path.read_bytes()
            if hashlib.sha256(raw).hexdigest() != item["sha256"]:
                raise ValueError("原文资源冻结 SHA 校验失败")
            aliases = [f"{RUNS_ROOT}/{job['job_id']}/rounds/round-{number}/source/{item['relative_path']}"]
            if item.get("origin_uri"):
                aliases.append(item["origin_uri"])
            for uri in aliases:
                if uri in result and result[uri] != raw:
                    raise ValueError("同一原文 URI 对应多个冻结版本，不能猜测复制版本")
                result[uri] = raw
    return result
