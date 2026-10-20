# ruff: noqa: F401
"""Component transport validation is independent of the mined business domain."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import native_components
from team_miner.skill_miner.native_components import PROTOCOL, assemble_components
from tests.miner.skill_miner.fixtures import build_document

SOURCE = "viking://resources/frozen/operations.md"

OTHER_SOURCE = "viking://resources/frozen/recovery.md"

def write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

def package(root, name="service-recovery", *, sources=None):
    sources = sources or [SOURCE]
    candidate = root / "compiled_skill" / name
    component = root / "analysis/components" / name
    candidate.mkdir(parents=True)
    method = {"schema_version": "skill-miner.method.v1", "name": name,
              "source_refs": sources, "task": "恢复中断服务", "trigger": "健康探针失败",
              "inputs": ["运行记录"], "outputs": ["恢复记录"], "boundary": "仅当前服务",
              "rationale": "独立触发与终态", "questions": []}
    write_json(candidate / "METHOD.json", method)
    (candidate / "SKILL.md").write_text("# 服务恢复\n\n先确认影响范围，再按来源执行。\n", encoding="utf-8")
    (candidate / "EVALUATION.md").write_text("# 验证说明\n\n这些产物仍需实际业务复核。\n", encoding="utf-8")
    reference = f"compiled_skill/{name}/references/configuration.md"
    (root / reference).parent.mkdir()
    (root / reference).write_text("# 参数配置\n\n保留原始服务条件。\n", encoding="utf-8")
    dataset = build_document(name, [{"query": f"诊断第 {index + 1} 个已记录故障。",
                                     "source_session_ids": sources} for index in range(12)])
    cases = dataset.pop("cases")
    manifest = {"schema_version": PROTOCOL, "name": name, "dataset": dataset,
                "references": [{"path": reference, "source_refs": sources}], "cases": [],
                "coverage": [{"source_ref": source, "reference_paths": [reference],
                              "case_ids": [case["case_id"] for case in cases],
                              "disposition": "retained", "reason": "保留配置和有来源的场景"} for source in sources]}
    for index, case in enumerate(cases):
        path = f"analysis/components/{name}/cases/case-{index + 1:02}.json"
        write_json(root / path, case)
        manifest["cases"].append({"path": path, "case_id": case["case_id"], "source_refs": sources})
    write_json(component / "manifest.json", manifest)
    return manifest, {**dataset, "cases": cases}

def save_manifest(root, manifest):
    write_json(root / "analysis/components" / manifest["name"] / "manifest.json", manifest)

def snapshot(root):
    return {p.relative_to(root).as_posix(): p.read_bytes() for p in root.rglob("*") if p.is_file()}

def reject_without_writes(root, match, *, sources=None, required=True):
    before = snapshot(root)
    with pytest.raises(ValueError, match=match):
        assemble_components(root, set(sources or [SOURCE]), required=required)
    assert snapshot(root) == before
