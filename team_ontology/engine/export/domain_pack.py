"""导出：干净 Domain Pack v1.0 JSON（主产物）与 provenance.jsonl（证据侧车）。"""

from __future__ import annotations

import json
from pathlib import Path

from ..provenance import AnnotatedPack


def export_domain_pack(annotated: AnnotatedPack, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    payload = annotated.clean_pack_dict()
    path = out_dir / f"{payload['pack_id']}-{payload['version']}.json"
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return path


def export_provenance(annotated: AnnotatedPack, out_dir: Path) -> Path:
    out_dir.mkdir(parents=True, exist_ok=True)
    path = out_dir / "provenance.jsonl"
    with open(path, "w", encoding="utf-8") as fh:
        fh.writelines(json.dumps(row, ensure_ascii=False) + "\n" for row in annotated.provenance_rows())
    return path


def export_all(annotated: AnnotatedPack, out_dir: Path) -> dict[str, Path]:
    paths: dict[str, Path] = {
        "domain_pack": export_domain_pack(annotated, out_dir),
        "provenance": export_provenance(annotated, out_dir),
    }
    from .markdown_report import export_markdown_report

    paths["spec"] = export_markdown_report(annotated, out_dir)
    return paths
