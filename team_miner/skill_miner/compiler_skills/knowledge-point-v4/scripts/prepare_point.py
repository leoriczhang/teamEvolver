"""Skill-owned source preparation. No model calls, publication, or TE integration code."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path

ID = re.compile(r"[0-9a-f]{24}")
TABLES = {"知识点.md", "对练任务.md", "对练场景.md", "课程章节.md", "对练用户.md"}


def digest(text):
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def inside(root, path):
    if path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise ValueError("Prepared path is outside the current Compile workspace")
    return path


def load(root, path):
    return json.loads(inside(root, path).read_text(encoding="utf-8"))


def write(root, path, value):
    inside(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_bytes(text.encode("utf-8"))


def own_assignment(root, ids):
    paths = list((root / "__compile_staging__/drafts").glob("*/assignment.json"))
    if len(paths) > 512:
        raise ValueError("Too many assignments in this Compile workspace")
    found = []
    for path in paths:
        data = load(root, path)
        if {row["id"] for row in data.get("inputs", [])} == set(ids):
            found.append(path.parent)
    if len(found) != 1:
        raise ValueError("Cannot identify exactly one caller assignment")
    return found[0]


def sources(root, ids):
    result = {}
    for rid in ids:
        if not isinstance(rid, str) or not ID.fullmatch(rid):
            raise ValueError("Invalid assigned source range ID")
        result[rid] = load(root, root / "__compile_staging__/pipeline/sources" / (rid + ".json"))
    return result


def join_ranges(parts):
    ordered = sorted(parts, key=lambda pair: pair[1]["start_char"])
    chunks, end = [], 0
    for rid, part in ordered:
        if part["start_char"] != end or part["end_char"] != end + len(part["text"]):
            raise ValueError("Assigned source ranges have a gap, overlap, or inconsistent bounds")
        chunks.append(part["text"])
        end = part["end_char"]
    return "".join(chunks)


def rows(text, uri):
    lines = text.splitlines()
    header = None
    result = []
    for line_number, line in enumerate(lines, 1):
        if not line.strip().startswith("|"):
            continue
        cells = [cell.strip() for cell in re.split(r"(?<!\\)\|", line.strip().strip("|"))]
        if header is None:
            header = cells
            continue
        if not cells or not cells[0].isdigit():
            continue
        fields = dict(zip(header, cells)) if len(header) == len(cells) else None
        result.append({"id": cells[0], "cells": cells, "fields": fields,
                       "source": {"uri": uri, "line": line_number, "record_id": cells[0],
                                  "record_sha256": digest(line)}, "raw_record": line})
    return result


def field(row, name):
    if row["fields"] is None or name not in row["fields"]:
        raise ValueError("A required original table column is missing or ambiguous: " + name)
    return row["fields"][name]


def members(value, names):
    if value in names:
        return [value]
    try:
        parsed = json.loads(value)
    except (ValueError, TypeError):
        parsed = re.split(r"[,，、;；\n]+", value)
    if not isinstance(parsed, list):
        raise ValueError("Knowledge-point membership is not an explicit value or list")
    parts = [str(part).strip() for part in parsed if str(part).strip()]
    if not parts or any(part not in names for part in parts):
        raise ValueError("Unresolved knowledge-point membership: " + value[:100])
    return parts


def source_index(root, data):
    ids = data["source_range_ids"]
    caller = own_assignment(root, ids)
    parts = sources(root, ids)
    uris = {p["uri"] for p in parts.values()}
    if len(uris) != 1:
        raise ValueError("A source-index call must contain one assigned file")
    uri = uris.pop()
    relative = uri.partition("/source/")[2]
    if not relative:
        raise ValueError("Expected a frozen native source URI")
    payload = {"source_file": relative, "uri": uri, "range_count": len(parts)}
    if relative.startswith("revision/"):
        payload.update(kind="context-locator", is_factual=False, role="previous-output-comparison-only")
        match = re.search(r"knowledge-(\d+)(?:/|\.)", relative)
        if match:
            payload["knowledge_point_id"] = match[1]
    else:
        text = join_ranges(list(parts.items()))
        payload.update(content_chars=len(text), sha256=digest(text))
        if relative.startswith(("supplements/", "human-review/")):
            payload.update(kind="revision-request", is_factual=False, role="current-revision-request", feedback=text)
        elif relative.startswith("original/"):
            payload.update(kind="source-access-locator", is_factual=True, role="complete-original-file")
            if Path(relative).name == "知识点.md":
                payload["knowledge_catalog"] = [{"id": r["id"], "name": field(r, "知识点标题"),
                                                 "chapter": field(r, "所属章节")} for r in rows(text, uri)]
        else:
            raise ValueError("Unknown frozen source role")
    response = {"records": [{"inputs": ids, "scope": {}, "evidence_spans": [],
                             "routing_text": relative, "payload": payload}]}
    reference = "prepared/source-index.json"
    write(root, caller / reference, response)
    return {"record_response_ref": reference, "role": payload["kind"], "source_file": relative,
            "range_count": len(parts), "knowledge_catalog": payload.get("knowledge_catalog", [])}


def original_files(root, record, parts):
    metadata = {}
    base = root / "__compile_staging__/pipeline"
    for parent in record["parents"]:
        parent_record = load(root, base / "records" / (parent + ".json"))
        value = load(root, base / (parent_record["payload_ref"] + ".json"))["fields"]
        if value.get("role") == "complete-original-file":
            metadata[value["uri"]] = value
    groups = {}
    for rid, part in parts.items():
        if "/source/original/" in part["uri"]:
            groups.setdefault(part["uri"], []).append((rid, part))
    result = {}
    for uri, group in groups.items():
        text = join_ranges(group)
        meta = metadata.get(uri)
        if (meta is None or len(group) != meta["range_count"] or len(text) != meta["content_chars"]
                or digest(text) != meta["sha256"]):
            raise ValueError("Complete original-file permissions or hashes are missing: " + uri)
        name = uri.rsplit("/", 1)[-1]
        result[name] = {"uri": uri, "text": text, "sha256": digest(text),
                        "range_ids": [rid for rid, _ in group]}
    missing = TABLES - set(result)
    policy = [name for name in result if name.startswith("模拟教官系统提示词")]
    if missing or len(policy) != 1 or len(result) != 6:
        raise ValueError("Every point must inherit all six complete originals; missing=" + repr(sorted(missing)))
    return result, policy[0]


def select_point(files, policy_name, knowledge_id):
    tables = {name: rows(value["text"], value["uri"]) for name, value in files.items() if name in TABLES}
    catalog = {field(row, "知识点标题"): row["id"] for row in tables["知识点.md"]}
    chosen = [r for r in tables["知识点.md"] if r["id"] == knowledge_id]
    if len(chosen) != 1:
        raise ValueError("The assigned knowledge point does not exist exactly once in the original table")
    point = chosen[0]
    name = field(point, "知识点标题")
    tasks, scenes = [], []
    for row in tables["对练任务.md"]:
        bound = members(field(row, "所属知识点"), catalog)
        if name in bound:
            tasks.append({"id": row["id"], "title": field(row, "任务标题"),
                          "description": field(row, "任务描述（AI）"), "reminder": field(row, "任务话术提醒（AI）"),
                          "sort_order": field(row, "任务排序"), "knowledge_point_ids": [catalog[n] for n in bound],
                          "source": row["source"], "source_fields": row["fields"]})
    for row in tables["对练场景.md"]:
        bound = members(field(row, "所属知识点"), catalog)
        if name in bound:
            scenes.append({"id": row["id"], "title": field(row, "场景标题"),
                           "title_detail": field(row, "场景标题详情"), "brief": field(row, "场景简要说明"),
                           "background": field(row, "场景背景"), "background_ai": field(row, "场景背景（AI）"),
                           "ai_task": field(row, "AI的场景任务（AI）"), "goal": field(row, "目标"),
                           "difficulty": field(row, "难度"), "customer_key": field(row, "客户key"),
                           "knowledge_point_ids": [catalog[n] for n in bound],
                           "source": row["source"], "source_fields": row["fields"]})
    if not tasks or not scenes:
        raise ValueError("The original tables provide no tasks or scenes; no example fallback is allowed")
    chapter = field(point, "所属章节")
    chapters = [row for row in tables["课程章节.md"] if field(row, "章节标题") == chapter]
    if not chapters:
        raise ValueError("The original chapter binding cannot be resolved")
    return {"schema_version": "knowledge-instructor.config.v1",
            "knowledge_point": {"id": knowledge_id, "name": name, "title": name, "chapter": chapter,
                                "goal": field(point, "知识点目标"), "description": field(point, "知识点描述"),
                                "max_practice_duration": field(point, "知识点的最大练习时长"),
                                "source": point["source"], "source_fields": point["fields"]},
            "tasks": tasks, "scenes": scenes, "chapters": chapters, "coach_source_rows": tables["对练用户.md"],
            "instructor_policy": {"role": "runtime_rules_only_examples_are_not_business_configuration",
                                  "text": files[policy_name]["text"], "source_uri": files[policy_name]["uri"],
                                  "sha256": files[policy_name]["sha256"]},
            "source_manifest": [{k: value[k] for k in ("uri", "sha256", "range_ids")} for value in files.values()]}


def prepare_point(root, data):
    rid = data["input_record_id"]
    if not isinstance(rid, str) or not ID.fullmatch(rid):
        raise ValueError("Invalid assigned point record ID")
    caller = own_assignment(root, [rid])
    base = root / "__compile_staging__/pipeline"
    record = load(root, base / "records" / (rid + ".json"))
    payload = load(root, base / (record["payload_ref"] + ".json"))["fields"]
    kid = str(payload.get("knowledge_id", payload.get("knowledge_point_id", "")))
    operation = payload.get("operation")
    if not re.fullmatch(r"\d{1,12}", kid) or operation not in {"create", "revise"}:
        raise ValueError("A point preparation requires one explicit original knowledge ID and operation")
    parts = sources(root, record["source_refs"])
    files, policy_name = original_files(root, record, parts)
    config = select_point(files, policy_name, kid)
    prefix = f"compiled_skill/knowledge-{kid}"
    config_ref = prefix + "/references/knowledge-config.json"
    skill_ref = prefix + "/SKILL.md"
    analysis_ref = f"analysis/knowledge-batches/knowledge-{kid}.json"
    if operation == "create":
        write(root, caller / config_ref, config)
        name = config["knowledge_point"]["name"]
        seed = ("---\nname: knowledge-" + kid + "\ndescription: "
                + json.dumps(name + "的教官对练：按真实任务和场景配置进行教学、引导和纠错。", ensure_ascii=False)
                + "\n---\n\n# " + name + "\n\n"
                + "先读取 [知识点配置](references/knowledge-config.json)。"
                + "本Skill仅执行该配置的knowledge_point、tasks和scenes。\n"
                + "任务与场景的原始ID、描述、背景和难度以配置为准，不能替换成系统提示词的示例或其他知识点内容。\n"
                + "instructor_policy.text是教官运行规则，其中示例只说明格式和流程，不是当前知识点的业务数据。\n\n"
                + "## 教学执行流程\n\n__MINER_TEACHING_METHOD__\n")
        if not (caller / skill_ref).exists():
            write(root, caller / skill_ref, seed)
        publish = [config_ref, analysis_ref]
    else:
        target = "/source/revision/" + skill_ref
        old = [(source_id, p) for source_id, p in parts.items() if p["uri"].endswith(target)]
        if not old:
            raise ValueError("The target point's complete previous SKILL.md was not assigned")
        if not (caller / skill_ref).exists():
            write(root, caller / skill_ref, join_ranges(old))
        write(root, caller / "prepared/fresh-knowledge-config.json", config)
        publish = [analysis_ref]
    write(root, caller / "prepared/instructor-rules.txt", files[policy_name]["text"])
    report = {"knowledge_id": kid, "operation": operation, "task_ids": [t["id"] for t in config["tasks"]],
              "scene_ids": [s["id"] for s in config["scenes"]], "sources": config["source_manifest"],
              "old_output_is_factual_evidence": False, "benchmark_generated": False}
    write(root, caller / analysis_ref, report)
    return {"knowledge_id": kid, "operation": operation, "skill_content_ref": skill_ref,
            "config_content_ref": config_ref if operation == "create" else "prepared/fresh-knowledge-config.json",
            "analysis_content_ref": analysis_ref, "script_prepared_publish_files": publish,
            "instructor_rules_ref": "prepared/instructor-rules.txt", "knowledge_point": config["knowledge_point"],
            "task_ids": report["task_ids"], "scene_ids": report["scene_ids"],
            "tasks": [{k: t[k] for k in ("id", "title", "description", "reminder")} for t in config["tasks"]],
            "scenes": [{k: s[k] for k in ("id", "title", "difficulty", "background", "background_ai", "goal")}
                       for s in config["scenes"]]}


def run(root, data):
    if data.get("mode") == "index":
        return source_index(root, data)
    if data.get("mode") == "prepare":
        return prepare_point(root, data)
    raise ValueError("Unknown preparation mode")


def main():
    input_path, output_path = map(Path, sys.argv[1:3])
    staging = next((p for p in input_path.resolve().parents if p.name == "__compile_staging__"), None)
    if staging is None:
        raise ValueError("Skill preparation must run inside its current Compile workspace")
    result = run(staging.parent, json.loads(input_path.read_text(encoding="utf-8")))
    write(staging.parent, output_path, result)


if __name__ == "__main__":
    main()
