"""生成质量扩展校验 — 超出 HugAgentOS parity 的检查，服务于"机器生成、人工把关"。

检查项：
- 别名/名称全局唯一（HugAgentOS 验收清单要求但 schema 未实现）；
- closed_values 重复与空白项；
- asset trigger 的 ``ontology:ConceptId`` 标签必须引用已定义概念；
- 工作流触发词跨工作流重叠 / 相互为子串（容易误触发或歧义）；
- 孤立概念提示（未被任何关系/约束/资产标签引用）；
- 语料术语覆盖率：统计术语 vs 概念名称/别名，输出未覆盖高频词。
"""

from __future__ import annotations

import re
from collections.abc import Iterable
from typing import Any

from ..schemas import OntologyPackDocument
from .hugagentos_parity import ValidationIssue, ValidationReport

ONTOLOGY_TAG_PREFIX = "ontology:"


def normalize_term(term: str) -> str:
    """术语规范化键：小写、去空白。用于别名/触发词的冲突检测。"""
    return re.sub(r"\s+", "", term).lower()


def _alias_index(document: OntologyPackDocument) -> dict[str, list[tuple[int, str]]]:
    """规范化词面 -> [(concept_index, 原始词面)]。每个概念的名称与别名都计入。"""
    index: dict[str, list[tuple[int, str]]] = {}
    for i, concept in enumerate(document.concepts):
        for surface in [concept.name, *concept.aliases]:
            key = normalize_term(surface)
            if key:
                index.setdefault(key, []).append((i, surface))
    return index


class ExtendedValidator:
    def __init__(self, *, term_stats: dict[str, float] | None = None):
        """term_stats: {词面: 权重/词频}，用于覆盖率检查（可选）。"""
        self.term_stats = term_stats or {}

    def validate(self, document: OntologyPackDocument) -> ValidationReport:
        errors: list[ValidationIssue] = []
        warnings: list[ValidationIssue] = []
        infos: list[ValidationIssue] = []

        self._check_aliases(document, warnings)
        self._check_closed_values(document, errors, warnings)
        self._check_asset_tags(document, errors, warnings)
        self._check_trigger_collisions(document, warnings)
        self._check_orphans(document, infos)
        if self.term_stats:
            self._check_coverage(document, infos)

        return ValidationReport(not errors, errors, warnings, infos)

    def _check_aliases(self, document: OntologyPackDocument, warnings: list[ValidationIssue]) -> None:
        index = _alias_index(document)
        for key, entries in sorted(index.items()):
            concept_indexes = {c for c, _ in entries}
            if len(concept_indexes) > 1:
                surfaces = "、".join(repr(s) for _, s in entries)
                warnings.append(
                    ValidationIssue(
                        "warning",
                        "concepts",
                        f"词面冲突：{surfaces} 归一化后相同，被多个概念使用，需人工裁决",
                    )
                )

    def _check_closed_values(
        self,
        document: OntologyPackDocument,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        for i, concept in enumerate(document.concepts):
            if not concept.closed_values:
                continue
            path = f"concepts.{i}.closed_values"
            seen: dict[str, str] = {}
            for value in concept.closed_values:
                if not value.strip():
                    errors.append(ValidationIssue("error", path, "存在空白受控取值"))
                    continue
                key = normalize_term(value)
                if key in seen:
                    warnings.append(ValidationIssue("warning", path, f"受控取值重复：{value!r} 与 {seen[key]!r}"))
                else:
                    seen[key] = value

    def _check_asset_tags(
        self,
        document: OntologyPackDocument,
        errors: list[ValidationIssue],
        warnings: list[ValidationIssue],
    ) -> None:
        concept_ids = {c.id for c in document.concepts}
        for i, workflow in enumerate(document.workflows):
            for j, trigger in enumerate(workflow.asset_triggers):
                for tag in trigger.tags_any:
                    if not tag.startswith(ONTOLOGY_TAG_PREFIX):
                        warnings.append(
                            ValidationIssue(
                                "warning",
                                f"workflows.{i}.asset_triggers.{j}.tags_any",
                                f"标签 {tag!r} 不是 ontology:ConceptId 形式，不会作为受控本体标签生效",
                            )
                        )
                        continue
                    concept_id = tag[len(ONTOLOGY_TAG_PREFIX) :]
                    if concept_id not in concept_ids:
                        errors.append(
                            ValidationIssue(
                                "error",
                                f"workflows.{i}.asset_triggers.{j}.tags_any",
                                f"标签引用了未定义的概念 {concept_id!r}",
                            )
                        )

    def _check_trigger_collisions(self, document: OntologyPackDocument, warnings: list[ValidationIssue]) -> None:
        owners: dict[str, list[int]] = {}
        for i, workflow in enumerate(document.workflows):
            for trigger in workflow.triggers:
                key = normalize_term(trigger)
                if key:
                    owners.setdefault(key, []).append(i)

        for key, workflow_indexes in sorted(owners.items()):
            if len(set(workflow_indexes)) > 1:
                warnings.append(
                    ValidationIssue(
                        "warning",
                        "workflows",
                        f"触发词 {key!r} 被多个工作流使用，命中时存在歧义",
                    )
                )

        all_triggers = [(i, t) for i, w in enumerate(document.workflows) for t in w.triggers]
        for a, (wi, ti) in enumerate(all_triggers):
            ti_key = normalize_term(ti)
            for wj, tj in all_triggers[a + 1 :]:
                tj_key = normalize_term(tj)
                if (wi != wj or ti != tj) and ti_key and tj_key and (ti_key in tj_key or tj_key in ti_key):
                    warnings.append(
                        ValidationIssue(
                            "warning",
                            f"workflows.{wi}",
                            f"触发词 {ti!r} 与 workflows.{wj} 的 {tj!r} 互为子串，可能造成误触发",
                        )
                    )

    def _check_orphans(self, document: OntologyPackDocument, infos: list[ValidationIssue]) -> None:
        referenced: set[str] = set()
        for relation in document.relations:
            referenced.update((relation.subject, relation.object))
        for constraint in document.constraints:
            if constraint.concept_id:
                referenced.add(constraint.concept_id)
        for workflow in document.workflows:
            for trigger in workflow.asset_triggers:
                for tag in trigger.tags_any:
                    if tag.startswith(ONTOLOGY_TAG_PREFIX):
                        referenced.add(tag[len(ONTOLOGY_TAG_PREFIX) :])
        for i, concept in enumerate(document.concepts):
            if concept.id not in referenced:
                infos.append(
                    ValidationIssue(
                        "info",
                        f"concepts.{i}",
                        f"概念 {concept.id} 未被任何关系/约束/资产标签引用",
                    )
                )

    def _check_coverage(self, document: OntologyPackDocument, infos: list[ValidationIssue]) -> None:
        covered: set[str] = set()
        for concept in document.concepts:
            for surface in [concept.name, *concept.aliases]:
                covered.add(normalize_term(surface))
        uncovered = [
            (term, weight)
            for term, weight in sorted(self.term_stats.items(), key=lambda kv: -kv[1])
            if normalize_term(term) not in covered
        ]
        total = len(self.term_stats)
        if total:
            coverage = (total - len(uncovered)) / total
            infos.append(
                ValidationIssue(
                    "info",
                    "coverage",
                    f"语料术语覆盖率 {coverage:.1%}（{total - len(uncovered)}/{total}）",
                )
            )
        for term, weight in uncovered[:20]:
            infos.append(ValidationIssue("info", "coverage", f"未覆盖术语：{term!r}（权重 {weight:g}）"))


def concept_covers_term(concepts: Iterable[Any], term: str) -> bool:
    """term 是否被任一概念的 name/aliases 词面覆盖（规范化后完全匹配）。"""
    key = normalize_term(term)
    return any(normalize_term(c.name) == key or key in {normalize_term(a) for a in c.aliases} for c in concepts)
