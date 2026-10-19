"""Router assembly preserves supplied metadata without promoting it into common rules."""
import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest
import yaml

from team_miner.tests.test_knowledge_skill_preparation import identity, put

SCRIPT = Path(__file__).parents[1] / "compiler_skills/skill-router-v4/scripts/assemble_router.py"
SPEC = importlib.util.spec_from_file_location("router_assembly_v4", SCRIPT)
ASSEMBLE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(ASSEMBLE)


def directory(root, roots=None):
    base = root / "__compile_staging__/pipeline"
    ids = []
    for index in [1, 2]:
        rid, sid = identity("entry" + str(index)), identity("range" + str(index))
        ids.append(rid)
        slug = "method-" + str(index)
        entry = {"slug": slug, "name": "方法" + str(index),
                 "dependency_root": (roots or ["viking://resources/source/library"] * 2)[index - 1],
                 "skill_path": slug + "/SKILL.md", "config_path": slug + "/references/config.json",
                 "gaps": ["原文尚未确认的条件"] if index == 1 else [],
                 "tasks": [{"id": "1", "title": "原任务"}],
                 "scenes": [{"id": "1", "mode": "本场景连续三轮才触发；不是通用要求"}]}
        put(base / "records" / (rid + ".json"), {"payload_ref": "payloads/" + rid, "source_refs": [sid]})
        put(base / "payloads" / (rid + ".json"), {"fields": {"kind": "skill-entry", "entry": entry}})
        put(base / "sources" / (sid + ".json"), {"uri": "viking://resources/test/source/original/" + slug + ".md"})
    caller = root / "__compile_staging__/drafts/writer"
    put(caller / "assignment.json", {"inputs": [{"id": rid} for rid in ids]})
    return caller, base, ids


def test_complete_catalog_and_yaml_body_do_not_generalize_local_rules(tmp_path):
    caller, _, ids = directory(tmp_path)
    result = ASSEMBLE.run(tmp_path, {"anchor_input_id": ids[1]})
    files = json.loads((caller / result["file_response_ref"]).read_text())["files"]
    assert len(files) == 2 and all(row["inputs"] == ids for row in files)
    assert all((caller / row["content_ref"]).is_file() for row in files)
    skill = (caller / ASSEMBLE.PREFIX / "SKILL.md").read_text()
    metadata = yaml.safe_load(skill.split("---", 2)[1])
    assert metadata["name"] == "instructor-skill-router" and metadata["description"]
    assert "连续三轮" not in skill
    catalog = json.loads((caller / ASSEMBLE.PREFIX / "references/skill-catalog.json").read_text())
    assert len(catalog["entries"]) == 2
    assert catalog["dependency_root"] == "viking://resources/source/library"
    assert all("dependency_root" not in entry for entry in catalog["entries"])
    assert catalog["entries"][0]["gaps"] == ["原文尚未确认的条件"]
    assert catalog["entries"][0]["tasks"][0]["id"] == catalog["entries"][0]["scenes"][0]["id"]
    assert catalog["entries"][0]["source_refs"] == ["viking://resources/test/source/original/method-1.md"]


def test_real_script_cli_accepts_flat_tool_data_and_discovers_workspace_from_invocation_path(tmp_path):
    caller, _, ids = directory(tmp_path)
    invocation = tmp_path / "__compile_staging__/scripts/current-call"
    put(invocation / "input.json", {"mode": "assemble", "anchor_input_id": ids[0]})
    result = subprocess.run([sys.executable, "-I", str(SCRIPT.resolve()),
                             str(invocation / "input.json"), str(invocation / "output.json")],
                            cwd=invocation, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    data = json.loads((invocation / "output.json").read_text())
    assert data["ready_to_emit"] and data["entry_count"] == 2
    assert (caller / data["file_response_ref"]).is_file()


def test_multiple_actual_dependency_roots_stay_per_entry(tmp_path):
    caller, _, ids = directory(tmp_path, roots=["viking://resources/a", "viking://resources/b"])
    ASSEMBLE.run(tmp_path, {"anchor_input_id": ids[0]})
    catalog = json.loads((caller / ASSEMBLE.PREFIX / "references/skill-catalog.json").read_text())
    assert catalog["dependency_root"] is None
    assert [e["dependency_root"] for e in catalog["entries"]] == ["viking://resources/a", "viking://resources/b"]


def test_bundle_metadata_uses_explicit_resource_uris_and_preserves_source_uncertainty():
    cfg = {"knowledge_point": {"id": "7", "name": "明确名称", "chapter": "章节甲"},
           "skill": {"path": "compiled_skill/knowledge-7/SKILL.md"},
           "tasks": [{"id": "1", "title": "原任务"}],
           "scenes": [{"id": "1", "difficulty": "hard", "knowledge_point_ids": ["明确名称"],
                       "interaction_mode": "连续三轮", "note": "场景1的背景未完整获取"}],
           "deferred": {"scene_1": "保留原有未确认说明"}}
    source = ("依赖根目录URI：viking://resources/library\n"
              "原SKILL资源URI：viking://resources/library/knowledge-7/SKILL.md\n"
              "原配置资源URI：viking://resources/library/knowledge-7/references/knowledge-config.json\n"
              "## 原始references/knowledge-config.json\n\n```json\n" + json.dumps(cfg, ensure_ascii=False) + "\n```\n")
    entry = ASSEMBLE.bundle_entry(source)
    assert entry["skill_path"] == "knowledge-7/SKILL.md"
    assert entry["config_path"] == "knowledge-7/references/knowledge-config.json"
    assert entry["scenes"][0]["knowledge_point_ids"] == []
    assert entry["scenes"][0]["knowledge_point_names"] == ["明确名称"]
    assert entry["scenes"][0]["mode"] == "未确认"
    assert entry["scenes"][0]["source_declared_mode"] == "连续三轮"
    assert entry["gaps"][0]["value"] == cfg["deferred"]


def test_prior_result_cannot_become_an_original_router_member(tmp_path):
    _, base, ids = directory(tmp_path)
    record = json.loads((base / "records" / (ids[0] + ".json")).read_text())
    put(base / "sources" / (record["source_refs"][0] + ".json"),
        {"uri": "viking://resources/test/source/revision/compiled_skill/router/SKILL.md"})
    with pytest.raises(ValueError, match="prior result cannot become"):
        ASSEMBLE.run(tmp_path, {"anchor_input_id": ids[0]})


@pytest.mark.parametrize("mutation", ["duplicate_slug", "escaped_path", "missing_gap_statement"])
def test_invalid_entry_cannot_be_silently_dropped_or_rebound(tmp_path, mutation):
    caller, base, ids = directory(tmp_path)
    path = base / "payloads" / (ids[1] + ".json")
    data = json.loads(path.read_text())
    entry = data["fields"]["entry"]
    if mutation == "duplicate_slug":
        entry["slug"] = "method-1"
    elif mutation == "escaped_path":
        entry["skill_path"] = "../other/SKILL.md"
    else:
        entry.pop("gaps")
    put(path, data)
    with pytest.raises(ValueError):
        ASSEMBLE.run(tmp_path, {"anchor_input_id": ids[0]})
    assert not (caller / ASSEMBLE.PREFIX).exists()
