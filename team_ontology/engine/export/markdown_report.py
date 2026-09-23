"""SPEC.md：人类可读的 Domain Pack 规格说明（含证据摘要与覆盖率）。"""

from __future__ import annotations

from pathlib import Path

from ..provenance import AnnotatedPack
from ..validation.extended import normalize_term


def export_markdown_report(annotated: AnnotatedPack, out_dir: Path) -> Path:
    pack = annotated.pack
    lines: list[str] = [
        f"# {pack.name}（{pack.pack_id} v{pack.version}）",
        "",
        f"- 领域：{pack.domain}",
        f"- 说明：{pack.description or '-'}",
        f"- 概念 {len(pack.concepts)} ｜ 关系 {len(pack.relations)} "
        f"｜ 约束 {len(pack.constraints)} ｜ 工作流 {len(pack.workflows)}",
        f"- 生成时间：{annotated.generated_at}",
        "",
        "## 概念",
        "",
    ]
    for i, concept in enumerate(pack.concepts):
        parent = f"（父概念：{concept.parent_id}）" if concept.parent_id else ""
        aliases = f" 别名：{'、'.join(concept.aliases)}" if concept.aliases else ""
        closed = f" 受控取值：{'、'.join(concept.closed_values)}" if concept.closed_values else ""
        lines.append(f"### {concept.id} — {concept.name} `{concept.risk}`")
        lines.append(f"{concept.definition}{aliases}{closed}{parent}")
        for evidence in annotated.evidence.get(f"concepts.{i}", []):
            lines.append(f"> 证据 [{evidence.doc}]：{evidence.quote}")
        lines.append("")

    lines.append("## 关系")
    lines.append("")
    for i, relation in enumerate(pack.relations):
        forbidden = "（禁止）" if relation.forbidden else ""
        lines.append(
            f"- **{relation.subject}** —{relation.predicate}→ **{relation.object}**{forbidden}："
            f"{relation.description or '-'}"
        )
        for evidence in annotated.evidence.get(f"relations.{i}", []):
            lines.append(f"  > 证据 [{evidence.doc}]：{evidence.quote}")
    lines.append("")

    lines.append("## 约束")
    lines.append("")
    for i, constraint in enumerate(pack.constraints):
        lines.append(f"- **{constraint.name}**（{constraint.id}，mode={constraint.mode}，risk={constraint.risk}）")
        lines.append(f"  - 失败说明：{constraint.message}")
        if constraint.suggestion:
            lines.append(f"  - 修正建议：{constraint.suggestion}")
        for evidence in annotated.evidence.get(f"constraints.{i}", []):
            lines.append(f"  > 证据 [{evidence.doc}]：{evidence.quote}")
    lines.append("")

    lines.append("## 工作流")
    lines.append("")
    for i, workflow in enumerate(pack.workflows):
        lines.append(f"### {workflow.name}（{workflow.id}，review={workflow.review_level}，risk={workflow.risk}）")
        lines.append(f"- 触发词：{'、'.join(workflow.triggers)}")
        if workflow.required_tools:
            lines.append(f"- 必需工具：{'、'.join(workflow.required_tools)}")
        if workflow.forbidden_tools:
            lines.append(f"- 禁止工具：{'、'.join(workflow.forbidden_tools)}")
        if workflow.output_tags:
            lines.append(f"- 输出标签：{'、'.join(workflow.output_tags)}")
        for evidence in annotated.evidence.get(f"workflows.{i}", []):
            lines.append(f"> 证据 [{evidence.doc}]：{evidence.quote}")
        lines.append("")

    coverage = annotated.coverage.get("terms") or {}
    if coverage:
        covered = {normalize_term(c.name) for c in pack.concepts} | {
            normalize_term(a) for c in pack.concepts for a in c.aliases
        }
        total = len(coverage)
        covered_count = sum(1 for term in coverage if normalize_term(term) in covered)
        lines.append("## 术语覆盖率")
        lines.append("")
        lines.append(f"{covered_count}/{total}（{covered_count / total:.1%}）")
        lines.append("")

    path = out_dir / "SPEC.md"
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
