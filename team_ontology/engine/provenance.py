"""证据溯源与评审元数据：AnnotatedPack = 严格 v1.0 pack + 证据注册表 + 元素状态。

导出时证据/元数据只进 sidecar（provenance.jsonl），绝不混入 Domain Pack JSON
（v1.0 schema 禁止 extra 字段）。
"""

from __future__ import annotations

import datetime as _dt
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from .schemas import OntologyPackDocument

Status = Literal["proposed", "approved", "rejected", "edited"]


class Evidence(BaseModel):
    chunk_id: str
    doc: str
    quote: str

    def as_dict(self) -> dict[str, Any]:
        return self.model_dump()


class ElementMeta(BaseModel):
    status: Status = "proposed"
    confidence: float = Field(default=1.0, ge=0.0, le=1.0)
    prompt_version: str = ""
    model: str = ""
    generated_at: str = ""
    votes: dict[str, int] = Field(default_factory=dict)


def utc_now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class AnnotatedPack(BaseModel):
    pack: OntologyPackDocument
    evidence: dict[str, list[Evidence]] = Field(default_factory=dict)
    meta: dict[str, ElementMeta] = Field(default_factory=dict)
    coverage: dict[str, Any] = Field(default_factory=dict)
    generated_at: str = Field(default_factory=utc_now)

    # ---- 构建辅助 ----

    def set_element(
        self,
        path: str,
        *,
        evidence: list[Evidence],
        prompt_version: str,
        model: str,
        confidence: float = 1.0,
        votes: dict[str, int] | None = None,
    ) -> None:
        self.evidence[path] = evidence
        self.meta[path] = ElementMeta(
            confidence=confidence,
            prompt_version=prompt_version,
            model=model,
            generated_at=utc_now(),
            votes=votes or {},
        )

    def element_paths(self, prefix: str) -> list[str]:
        return sorted(p for p in self.meta if p.startswith(prefix))

    # ---- 决策 ----

    def decide(self, path: str, decision: Literal["approve", "reject"]) -> None:
        if path not in self.meta:
            raise KeyError(f"unknown element path: {path}")
        self.meta[path].status = "approved" if decision == "approve" else "rejected"

    def edit(self, path: str, value: dict[str, Any]) -> None:
        """用新值替换元素（path 形如 concepts.0 / relations.1 / constraints.2 / workflows.3）。"""
        kind, index_str = path.split(".", 1)
        index = int(index_str)
        if kind not in {"concepts", "relations", "constraints", "workflows"}:
            raise ValueError(f"unsupported element kind: {kind}")
        items = list(getattr(self.pack, kind))
        if not (0 <= index < len(items)):
            raise KeyError(f"element index out of range: {path}")
        current = items[index]
        model_type = type(current)
        items[index] = model_type.model_validate(value)
        setattr(self.pack, kind, items)
        if path in self.meta:
            self.meta[path].status = "edited"

    # ---- 导出 ----

    def clean_pack_dict(self) -> dict[str, Any]:
        return self.pack.model_dump(by_alias=True, exclude_none=False)

    def provenance_rows(self) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        for path, evidences in sorted(self.evidence.items()):
            meta = self.meta.get(path)
            rows.append(
                {
                    "path": path,
                    "status": meta.status if meta else "proposed",
                    "confidence": meta.confidence if meta else None,
                    "evidence": [e.as_dict() for e in evidences],
                }
            )
        return rows


def save_annotated(pack: AnnotatedPack, path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    # by_alias=True：JSON 使用 v1.0 规范键名（如 constraints[].schema）
    tmp.write_text(pack.model_dump_json(indent=2, by_alias=True), encoding="utf-8")
    tmp.replace(path)


def load_annotated(path: Path) -> AnnotatedPack:
    return AnnotatedPack.model_validate_json(path.read_text(encoding="utf-8"), by_alias=True)


def evidence_from_chunks(chunks: list[dict], quote_chars: int = 160) -> list[Evidence]:
    result: list[Evidence] = []
    for chunk in chunks:
        text = chunk["text"].strip().replace("\n", " ")
        quote = text if len(text) <= quote_chars else text[:quote_chars] + "…"
        result.append(Evidence(chunk_id=chunk["chunk_id"], doc=chunk["doc"], quote=quote))
    return result
