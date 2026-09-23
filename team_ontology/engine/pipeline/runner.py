"""流水线编排：阶段调度 + 组装 + 校验 + 修复重试 + 运行记录。"""

from __future__ import annotations

import json
from collections.abc import Callable
from pathlib import Path
from typing import Any

from ..config import ProjectConfig
from ..corpus.loader import load_tool_catalog
from ..provenance import save_annotated, utc_now
from ..validation.extended import ExtendedValidator
from ..validation.hugagentos_parity import DomainPackValidator, ValidationIssue
from .assemble import assemble_pack
from .context import ProjectContext
from .stage_relations import generate_relations
from .stage_taxonomy import generate_taxonomy
from .stage_terms import generate_concepts

ALL_STAGES = ("terms", "taxonomy", "relations", "constraints", "workflows")

# 阶段 → 产出工件名
STAGE_ARTIFACTS: dict[str, tuple[str, ...]] = {
    "terms": ("concepts",),
    "taxonomy": ("taxonomy_edges",),
    "relations": ("relations",),
    "constraints": ("iron_law_constraints", "tool_constraints"),
    "workflows": ("workflows",),
}


def _stage_done(ctx: ProjectContext, stage: str) -> bool:
    return all(ctx.has_artifact(name) for name in STAGE_ARTIFACTS[stage])


def _stage_function(stage: str) -> Callable:
    if stage == "terms":
        return generate_concepts
    if stage == "taxonomy":
        return generate_taxonomy
    if stage == "relations":
        return generate_relations
    if stage == "constraints":
        from .stage_constraints import generate_constraints

        return generate_constraints
    if stage == "workflows":
        from .stage_workflows import generate_workflows

        return generate_workflows
    raise ValueError(f"unknown stage: {stage}")


def _error_stages(errors: list[ValidationIssue]) -> set[str]:
    stages: set[str] = set()
    for error in errors:
        path = error.path
        if "cycle" in error.message or path.startswith("taxonomy"):
            stages.add("taxonomy")
            stages.add("terms")
        elif "references unknown concept" in error.message:
            stages.add("terms")
            stages.add("relations")
            stages.add("constraints")
        elif path.startswith("concepts."):
            stages.add("terms")
        elif path.startswith("relations."):
            stages.add("relations")
        elif path.startswith("constraints."):
            stages.add("constraints")
        elif path.startswith("workflows."):
            stages.add("workflows")
        else:
            stages.add("terms")
    return stages


class PipelineRunner:
    def __init__(self, ctx: ProjectContext, llm: Any):
        self.ctx = ctx
        self.llm = llm

    def run(
        self,
        config: ProjectConfig,
        *,
        stages: tuple[str, ...] = ALL_STAGES,
        force: bool = False,
        repair_rounds: int = 2,
    ) -> dict[str, Any]:
        if "terms" in stages and not self.ctx.has_artifact("terms"):
            raise RuntimeError("缺少术语统计工件，请先运行 `oe ingest`")
        if any(s in stages for s in ("taxonomy", "relations", "constraints", "workflows")) and (
            not self.ctx.has_artifact("concepts") and "terms" not in stages
        ):
            raise RuntimeError("缺少概念工件，请先运行 terms 阶段")

        prompt_versions: dict[str, str] = {}
        tool_schemas: dict[str, dict] = {}
        if config.tool_catalog_path:
            tool_schemas = load_tool_catalog(Path(config.tool_catalog_path))

        def run_stage(stage: str, repair_notes: list[str] | None = None) -> None:
            function = _stage_function(stage)
            started = utc_now()
            try:
                function(self.ctx, self.llm, config=config, repair_notes=repair_notes)
                self._record(stage, "ok", started)
            except Exception as exc:
                self._record(stage, "failed", started, error=str(exc))
                raise

        for stage in stages:
            if force or not _stage_done(self.ctx, stage):
                run_stage(stage)

        # 组装 + 校验 + 修复循环
        term_stats = {t["term"]: t["weight"] for t in self.ctx.read_jsonl("terms")}
        annotated = None
        last_report: dict[str, Any] = {"valid": False}
        for round_index in range(repair_rounds + 1):
            annotated = assemble_pack(self.ctx, config=config, model=self.llm.model, prompt_versions=prompt_versions)
            document, parity = DomainPackValidator().validate(annotated.clean_pack_dict(), tool_schemas=tool_schemas)
            extended = ExtendedValidator(term_stats=term_stats).validate(document) if document is not None else None
            errors = parity.errors + ([e for e in extended.errors if e.severity == "error"] if extended else [])
            warnings = parity.warnings + ([e for e in extended.warnings if e.severity == "warning"] if extended else [])
            infos = [e for e in (extended.infos if extended else []) if e.severity == "info"]
            last_report = {
                "valid": not errors,
                "errors": [e.as_dict() for e in errors],
                "warnings": [e.as_dict() for e in warnings],
                "infos": [e.as_dict() for e in infos],
                "round": round_index,
            }
            if not errors:
                break
            if round_index >= repair_rounds:
                break
            affected = _error_stages(errors) & set(stages)
            if not affected:
                break
            # 按流水线顺序重跑（terms 先于 taxonomy/relations，避免概念变化后下游悬空）
            for stage in [s for s in ALL_STAGES if s in affected]:
                notes = [f"{e.path or 'pack'}: {e.message}" for e in errors if _stage_owns_error(stage, e)]
                if notes:
                    run_stage(stage, repair_notes=notes)

        # 最终组装（修复后可能变化）
        annotated = assemble_pack(self.ctx, config=config, model=self.llm.model, prompt_versions=prompt_versions)
        save_annotated(annotated, self.ctx.annotated_path(config.pack_id))
        return last_report

    def _record(self, stage: str, status: str, started: str, *, error: str | None = None) -> None:
        row = {
            "stage": stage,
            "status": status,
            "model": self.llm.model,
            "started_at": started,
            "finished_at": utc_now(),
            "error": error,
        }
        path = self.ctx.artifact_path("runs")
        with open(path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")


def _stage_owns_error(stage: str, error: ValidationIssue) -> bool:
    path = error.path
    if stage == "taxonomy":
        return path.startswith("taxonomy") or "cycle" in error.message
    if stage == "terms":
        return path.startswith("concepts.") or path == "pack"
    if stage in {"relations", "constraints"} and path == "pack" and "unknown concept" in error.message:
        return True
    return path.startswith(f"{stage}.")
