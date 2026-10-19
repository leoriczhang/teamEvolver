"""Mechanical source preservation in the compiler Skill's own preparation script."""
import hashlib
import importlib.util
import json
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "compiler_skills/knowledge-point-v4/scripts/prepare_point.py"
SPEC = importlib.util.spec_from_file_location("knowledge_skill_preparation", SCRIPT)
PREP = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(PREP)


def identity(value):
    return hashlib.sha256(value.encode()).hexdigest()[:24]


def put(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")


def table(header, data):
    return "\n".join(["| " + " | ".join(header) + " |", "| " + " | ".join("---" for _ in header) + " |",
                      *["| " + " | ".join(row) + " |" for row in data]]) + "\n"


class Workspace:
    def __init__(self, root):
        self.root = root
        self.base = root / "__compile_staging__/pipeline"
        self.drafts = root / "__compile_staging__/drafts"
        self.sources, self.indexes = [], []
        self.ids_by_file = {}
        documents = {
            "知识点.md": table(["id", "知识点标题", "知识点目标", "知识点描述",
                             "知识点的最大练习时长", "知识点排序", "所属章节"],
                             [["1", "甲", "目标甲", "描述甲", "600", "1", "章节"],
                              ["2", "甲乙", "目标乙", "描述乙", "600", "2", "章节"]]),
            "对练任务.md": table(["id", "任务标题", "任务描述（AI）", "任务话术提醒（AI）", "任务排序", "所属知识点"],
                              [["11", "甲任务", "原答案含\"引号\"", "提醒", "1", "甲"],
                               ["1", "乙任务", "乙答案", "提醒", "1", "甲乙"]]),
            "对练场景.md": table(["", "场景标题", "场景标题详情", "场景简要说明", "场景背景", "场景背景（AI）",
                              "AI的场景任务（AI）", "目标", "难度", "客户key", "所属知识点"],
                              [["21", "共享场景", "详情", "简要", "背景", "AI背景", "条件", "目标",
                                "easy", "coach", '["甲","甲乙"]'],
                               ["1", "乙场景", "详情", "简要", "背景乙", "AI背景乙", "条件", "目标",
                                "hard", "coach", "甲乙"]]),
            "课程章节.md": table(["id", "章节标题", "章节描述"], [["1", "章节", "原章节约束"]]),
            "对练用户.md": table(["id", "客户key", "客户姓名"], [["1", "coach", "教练"]]),
            "模拟教官系统提示词.md": "运行规则：只输出JSON。示例任务999不是原始任务表。\n",
        }
        for name, text in documents.items():
            uri = "viking://resources/test/source/original/" + name
            ids = self.add_source(uri, text)
            self.sources.extend(ids)
            self.ids_by_file[name] = ids
            folder = self.drafts / ("index-" + identity(name))
            put(folder / "assignment.json", {"inputs": [{"id": rid} for rid in ids]})
            result = PREP.run(root, {"mode": "index", "source_range_ids": ids})
            response = json.loads((folder / result["record_response_ref"]).read_text())
            rid = identity("index:" + name)
            self.indexes.append(rid)
            put(self.base / "records" / (rid + ".json"),
                {"parents": ids, "source_refs": ids, "payload_ref": "payloads/" + rid})
            put(self.base / "payloads" / (rid + ".json"), {"fields": response["records"][0]["payload"]})

    def add_source(self, uri, text):
        ids = []
        for start in range(0, len(text), 37):
            rid = identity(uri + str(start))
            ids.append(rid)
            put(self.base / "sources" / (rid + ".json"),
                {"uri": uri, "text": text[start:start + 37], "start_char": start,
                 "end_char": min(len(text), start + 37)})
        return ids

    def point(self, kid="1", operation="create", extra_indexes=(), extra_sources=()):
        rid = identity("point:" + kid + ":" + operation)
        put(self.base / "records" / (rid + ".json"),
            {"parents": self.indexes + list(extra_indexes), "source_refs": self.sources + list(extra_sources),
             "payload_ref": "payloads/" + rid})
        put(self.base / "payloads" / (rid + ".json"),
            {"fields": {"knowledge_id": kid, "operation": operation}})
        folder = self.drafts / ("point-" + rid)
        put(folder / "assignment.json", {"inputs": [{"id": rid}]})
        return rid, folder


def test_original_tables_own_ids_and_multi_point_scenes_override_prompt_examples(tmp_path):
    workspace = Workspace(tmp_path)
    rid, folder = workspace.point()
    result = PREP.run(tmp_path, {"mode": "prepare", "input_record_id": rid})
    config = json.loads((folder / result["config_content_ref"]).read_text())
    assert [row["id"] for row in config["tasks"]] == ["11"]
    assert [row["id"] for row in config["scenes"]] == ["21"]
    assert config["scenes"][0]["knowledge_point_ids"] == ["1", "2"]
    assert config["tasks"][0]["description"] == '原答案含"引号"'
    assert "999" in config["instructor_policy"]["text"]
    assert len(config["source_manifest"]) == 6
    (folder / result["skill_content_ref"]).write_text("已经写好的教学流程", encoding="utf-8")
    PREP.run(tmp_path, {"mode": "prepare", "input_record_id": rid})
    assert (folder / result["skill_content_ref"]).read_text() == "已经写好的教学流程"


def test_incomplete_original_permissions_fail_without_creating_fake_configuration(tmp_path):
    workspace = Workspace(tmp_path)
    rid, folder = workspace.point()
    record_path = workspace.base / "records" / (rid + ".json")
    record = json.loads(record_path.read_text())
    record["source_refs"] = [r for r in record["source_refs"] if r not in workspace.ids_by_file["对练场景.md"]]
    put(record_path, record)
    with pytest.raises(ValueError, match="six complete originals"):
        PREP.run(tmp_path, {"mode": "prepare", "input_record_id": rid})
    assert not (folder / "compiled_skill").exists()


def test_revision_index_has_no_business_body_and_preparation_copies_old_skill_bytes(tmp_path):
    workspace = Workspace(tmp_path)
    old_text = "旧正文第一行\r\n第二行保持原样\r\n"
    ids = workspace.add_source("viking://resources/test/source/revision/compiled_skill/knowledge-1/SKILL.md", old_text)
    folder = workspace.drafts / "old-skill-index"
    put(folder / "assignment.json", {"inputs": [{"id": rid} for rid in ids]})
    indexed = PREP.run(tmp_path, {"mode": "index", "source_range_ids": ids})
    response = json.loads((folder / indexed["record_response_ref"]).read_text())
    fields = response["records"][0]["payload"]
    assert fields["kind"] == "context-locator" and fields["is_factual"] is False
    assert old_text not in json.dumps(fields, ensure_ascii=False)
    index_id = identity("old-skill-index")
    put(workspace.base / "records" / (index_id + ".json"),
        {"parents": ids, "source_refs": ids, "payload_ref": "payloads/" + index_id})
    put(workspace.base / "payloads" / (index_id + ".json"), {"fields": fields})
    rid, caller = workspace.point(operation="revise", extra_indexes=[index_id], extra_sources=ids)
    prepared = PREP.run(tmp_path, {"mode": "prepare", "input_record_id": rid})
    assert (caller / prepared["skill_content_ref"]).read_bytes() == old_text.encode()
    assert prepared["script_prepared_publish_files"] == [prepared["analysis_content_ref"]]
    assert not (caller / "compiled_skill/knowledge-1/references/knowledge-config.json").exists()


def test_source_gap_is_reported_instead_of_silently_dropping_text(tmp_path):
    workspace = Workspace(tmp_path)
    ids = workspace.ids_by_file["对练任务.md"]
    with pytest.raises(ValueError, match="gap"):
        PREP.join_ranges([(rid, PREP.sources(tmp_path, [rid])[rid]) for rid in ids[1:]])
