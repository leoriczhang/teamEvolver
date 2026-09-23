"""草稿与发布：评审决策应用 + 版本治理（发布版本只读，再改即新草稿）。"""

from __future__ import annotations

import json
import shutil
from pathlib import Path

from ..pipeline.context import ProjectContext
from ..provenance import AnnotatedPack, load_annotated, utc_now

VERSIONS_FILE = "versions.jsonl"


def apply_decision(
    annotated: AnnotatedPack,
    path: str,
    decision: str,
    *,
    edited_value: dict | None = None,
) -> AnnotatedPack:
    if decision == "edit":
        if edited_value is None:
            raise ValueError("edit 决策需要 edited_value")
        annotated.edit(path, edited_value)
    elif decision in {"approve", "reject"}:
        annotated.decide(path, decision)
    else:
        raise ValueError(f"unknown decision: {decision}")
    return annotated


def append_decisions(ctx: ProjectContext, rows: list[dict]) -> None:
    with open(ctx.decisions_path, "a", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)


def export_reviewed_snapshot(ctx: ProjectContext, pack_id: str, *, require_all_approved: bool = True) -> Path:
    """发布：全部元素（除 rejected）须已批准；导出干净 JSON 并快照 annotated。"""
    annotated = load_annotated(ctx.annotated_path(pack_id))
    if require_all_approved:
        pending = [path for path, meta in annotated.meta.items() if meta.status not in {"approved", "rejected"}]
        if pending:
            raise ValueError(
                f"存在未审批元素（{len(pending)} 个，如 {pending[0]}），先完成评审或使用 require_all_approved=False"
            )

    version = annotated.pack.version
    published_dir = ctx.published_dir(pack_id)
    published_dir.mkdir(parents=True, exist_ok=True)

    from ..export.domain_pack import export_domain_pack

    pack_path = export_domain_pack(annotated, published_dir)
    snapshot = published_dir / f"annotated-{version}.json"
    shutil.copy2(ctx.annotated_path(pack_id), snapshot)

    versions_path = published_dir / VERSIONS_FILE
    row = {"version": version, "exported_at": utc_now(), "pack": pack_path.name}
    with open(versions_path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return pack_path


def list_versions(ctx: ProjectContext, pack_id: str) -> list[dict]:
    path = ctx.published_dir(pack_id) / VERSIONS_FILE
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines() if line.strip()]
