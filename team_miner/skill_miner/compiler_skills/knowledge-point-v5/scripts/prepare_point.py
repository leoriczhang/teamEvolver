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


def anchored_assignment(root, anchor):
    if not isinstance(anchor, str) or not ID.fullmatch(anchor):
        raise ValueError("Invalid assigned anchor record ID")
    paths = list((root / "__compile_staging__/drafts").glob("*/assignment.json"))
    if len(paths) > 512:
        raise ValueError("Too many assignments")
    found = []
    for path in paths:
        data = load(root, path)
        if any(row["id"] == anchor for row in data.get("inputs", [])):
            found.append((path.parent, data))
    if len(found) != 1:
        raise ValueError("Cannot identify exactly one planning assignment")
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


TEACHING_FLOW = """## 执行流程

1. 完整读取知识点配置中的 instructor_policy.text，执行其中的教官运行规则及 JSON 输出协议。
   先排除其情境示例，再把当前选定场景的 background、background_ai、ai_task、goal 和本知识点的
   tasks 代入场景背景、任务及完成要求。不得将提示词示例的人物、金额或任务复制为本轮事实。
2. 以调用方指定的 scene_id 选择 scenes 中适用于当前 knowledge_point.id 的场景；有 task_id 时，
   只接受 tasks 中属于当前知识点的任务，未指定任务则按实际任务顺序练习。场景可以关联多个知识点，
   只加载当前知识点的任务，不推断每个任务独占一个场景。缺少或不适用的选择先确认，不猜默认场景。
   这项参数确认是 Skill 的调用约定，不是原材料中的业务事实。
3. 根据选定场景背景确定互动分支：纯知识引导、带案例但不扮演客户、明确要求扮演客户。
   只有背景明确要求扮演客户时才扮演；出现客户资料本身不等于要求扮演。按原规则保持分支，
   暂停扮演和恢复扮演使用明确的切换话术。扮演期间自称“我”、称呼代理人“你”，切换示例仅用
   “我们先暂停扮演，我回到教练角色”和“好，我们继续”，不得用客户角色姓名自称或称呼代理人。
4. 按场景要求发起对话：代理人先寒暄时，本轮只简短自然回应，待其回应后再进入训练；需要案例
   开场白时完整交代实际案例，不提前报标准答案；无案例开场要求时围绕当前任务提出第一个问题。
5. 每轮先依据完整历史、上一轮任务状态及当前任务的真实完成要求分析完成度，再生成一致的教官回复。
   条件、例外和豁免原样遵守。需要纠错不等于任务未完成；评分标准和加分项不进入完成要求或教官提问。
   difficulty 只保留原标签用于场景选择，不能仅凭 easy/hard 改变完整性、数字准确性、追问深度或通过门槛；
   只有场景原文本身明确规定的差异才执行。只读当前任务的原始标准，不自创要求或统一门槛。
6. 逐任务引导、确认、纠错和推进，遵守原规则的确认流程及禁忌词立即纠正的例外。扮演客户时，
   正确但未讲完的内容按场景要求自然回应，不主动透露剩余答案；连续无法正确回复的处理条件
   以当前背景和共用规则为准。完成任务后推进下一个未完成任务，全部完成后收尾，不额外加题。
   偏离只针对代理人最新一句话和所有未完成任务判断；无明确含义消息按原规则回应。
7. 每轮仅输出合法 JSON，顶层恰为 reply、completionDegree、taskDeviation。reply 不超过三句话；
   completionDegree 与本轮实际任务集合一致，每项只有 task、complete、evaluation、reference；
   complete 和 taskDeviation 为整数 0 或 1，不为 null；taskDeviation 始终在顶层。
   字符串正确转义，不添加评分、额外字段、Markdown 包裹或 JSON 外的解释。其余细节执行完整原规则。
"""


def point_skill(config):
    point = config["knowledge_point"]
    kid, name = point["id"], point["name"]
    text = ("---\nname: knowledge-" + kid + "\ndescription: "
            + json.dumps(name + "的教官对练：按专属任务、场景及原规则引导、纠错与判断完成度。", ensure_ascii=False)
            + "\n---\n\n# " + name + "\n\n"
            + "先完整读取 [知识点配置](references/knowledge-config.json)。\n"
            + "本 Skill 只执行当前 knowledge_point、tasks 和 scenes；原始配置是事实来源，"
            + "instructor_policy.text 提供完整运行规则，其中情境示例不作为业务配置。\n\n"
            + "本知识点原始 ID：" + kid + "。所属章节：" + point["chapter"] + "。\n"
            + "任务 ID：" + "、".join(t["id"] for t in config["tasks"]) + "。\n"
            + "适用场景 ID：" + "、".join(s["id"] for s in config["scenes"]) + "。\n\n")
    return text + TEACHING_FLOW


def plan_collection(root, data):
    caller, assignment = anchored_assignment(root, data["anchor_input_id"])
    base = root / "__compile_staging__/pipeline"
    records = {}
    for item in assignment["inputs"]:
        rid = item["id"]
        record = load(root, base / "records" / (rid + ".json"))
        fields = load(root, base / (record["payload_ref"] + ".json"))["fields"]
        records[rid] = fields
    originals = {rid: value for rid, value in records.items() if value.get("role") == "complete-original-file"}
    if len(originals) != 6:
        raise ValueError("A planning assignment must include six complete originals")
    catalogs = [v["knowledge_catalog"] for v in originals.values() if "knowledge_catalog" in v]
    if len(catalogs) != 1:
        raise ValueError("Exactly one original knowledge catalog is required")
    catalog = {p["id"] for p in catalogs[0]}
    points = data["points"]
    if not isinstance(points, list) or not 1 <= len(points) <= 64:
        raise ValueError("An explicit bounded write_set is required")
    inputs, seen = list(originals), set()
    for point in points:
        kid = str(point["knowledge_id"])
        if kid not in catalog or kid in seen or point.get("operation") not in {"create", "revise"}:
            raise ValueError("Invalid, unknown, or duplicate knowledge point in write_set")
        seen.add(kid)
        if point["operation"] == "revise":
            suffix = "/source/revision/compiled_skill/knowledge-" + kid + "/SKILL.md"
            old = [rid for rid, v in records.items() if v.get("kind") == "context-locator"
                   and v.get("uri", "").endswith(suffix)]
            if len(old) != 1:
                raise ValueError("The selected point needs exactly one prior SKILL locator: " + kid)
            inputs.extend(old)
            resource_prefix = "/source/revision/compiled_skill/knowledge-" + kid + "/references/"
            inputs.extend(rid for rid, value in records.items() if value.get("kind") == "context-locator"
                          and resource_prefix in value.get("uri", "") and value["uri"].endswith(".md"))
    inputs.extend(rid for rid, value in records.items() if value.get("kind") == "revision-request")
    response = {"records": [{"inputs": list(dict.fromkeys(inputs)), "scope": {}, "evidence_spans": [],
                             "routing_text": "explicit knowledge-point write_set",
                             "payload": {"kind": "point-collection", "points": points}}]}
    reference = "prepared/dispatch.json"
    write(root, caller / reference, response)
    return {"record_response_ref": reference, "write_set": sorted(seen), "writer_jobs": 1,
            "inherited_original_files": len(originals)}


def collection(root, rid):
    if not isinstance(rid, str) or not ID.fullmatch(rid):
        raise ValueError("Invalid assigned collection record ID")
    caller = own_assignment(root, [rid])
    base = root / "__compile_staging__/pipeline"
    record = load(root, base / "records" / (rid + ".json"))
    payload = load(root, base / (record["payload_ref"] + ".json"))["fields"]
    points = payload.get("points")
    if not isinstance(points, list) or not 1 <= len(points) <= 64:
        raise ValueError("A collection must contain a bounded explicit write_set")
    seen = set()
    for point in points:
        kid = str(point.get("knowledge_id", ""))
        if (not re.fullmatch(r"\d{1,12}", kid) or kid in seen
                or point.get("operation") not in {"create", "revise"}):
            raise ValueError("Invalid or repeated write_set member")
        seen.add(kid)
    return caller, record, points


def prepare_collection(root, data):
    rid = data["input_record_id"]
    caller, record, points = collection(root, rid)
    parts = sources(root, record["source_refs"])
    files, policy_name = original_files(root, record, parts)
    state, details = {"input_record_id": rid, "files": [], "points": [], "previous_resources": {}}, []
    for point in points:
        kid, operation = str(point["knowledge_id"]), point["operation"]
        config = select_point(files, policy_name, kid)
        prefix = "compiled_skill/knowledge-" + kid
        skill = prefix + "/SKILL.md"
        config_ref = prefix + "/references/knowledge-config.json"
        analysis = "analysis/knowledge-batches/knowledge-" + kid + ".json"
        if operation == "create":
            write(root, caller / skill, point_skill(config))
            write(root, caller / config_ref, config)
            state["files"].extend([skill, config_ref])
            old_digest = None
        else:
            target = "/source/revision/" + skill
            old_parts = [(sid, value) for sid, value in parts.items() if value["uri"].endswith(target)]
            if not old_parts:
                raise ValueError("The selected point's previous SKILL was not assigned: " + kid)
            old_text = join_ranges(old_parts)
            old_digest = digest(old_text)
            if not (caller / skill).exists():
                write(root, caller / skill, old_text)
            old_resources = {}
            for sid, value in parts.items():
                relative = value["uri"].partition("/source/revision/")[2]
                if relative.startswith(prefix + "/references/") and relative.endswith(".md"):
                    old_resources.setdefault(relative, []).append((sid, value))
            for relative, fragments in old_resources.items():
                old_resource = join_ranges(fragments)
                state["previous_resources"][relative] = digest(old_resource)
                if not (caller / relative).exists():
                    write(root, caller / relative, old_resource)
            write(root, caller / ("prepared/knowledge-" + kid + "/knowledge-config.json"), config)
        report = {"knowledge_id": kid, "operation": operation,
                  "task_ids": [t["id"] for t in config["tasks"]],
                  "scene_ids": [s["id"] for s in config["scenes"]],
                  "sources": config["source_manifest"], "old_output_is_factual_evidence": False,
                  "benchmark_generated": False}
        write(root, caller / analysis, report)
        state["files"].append(analysis)
        state["points"].append({"knowledge_id": kid, "operation": operation,
                                "skill_ref": skill, "previous_skill_sha256": old_digest})
        details.append({"knowledge_id": kid, "operation": operation, "task_ids": report["task_ids"],
                        "scene_ids": report["scene_ids"], "skill_ref": skill})
    write(root, caller / "prepared/collection-state.json", state)
    result = manifest(root, {"input_record_id": rid, "extra_files": []})
    return {**result, "points": details, "generated_rules": "source-bound-canonical-flow-no-model-paraphrase",
            "note": "create files are complete; revise only edits selected old SKILL and requested new resources"}


def manifest(root, data):
    rid = data["input_record_id"]
    caller, _, points = collection(root, rid)
    state = load(root, caller / "prepared/collection-state.json")
    if state["input_record_id"] != rid:
        raise ValueError("Prepared collection identity changed")
    paths = list(state["files"])
    selected = {str(p["knowledge_id"]): p["operation"] for p in points}
    for point in state["points"]:
        if point["operation"] == "revise":
            path = caller / point["skill_ref"]
            if hashlib.sha256(inside(root, path).read_bytes()).hexdigest() != point["previous_skill_sha256"]:
                paths.append(point["skill_ref"])
    for path, previous in state["previous_resources"].items():
        if hashlib.sha256(inside(root, caller / path).read_bytes()).hexdigest() != previous:
            paths.append(path)
    extras = data.get("extra_files", [])
    if not isinstance(extras, list) or len(extras) > 64:
        raise ValueError("Additional resources must be a bounded list")
    for extra in extras:
        if (not isinstance(extra, str) or ".." in Path(extra).parts
                or not re.fullmatch(r"compiled_skill/knowledge-\d{1,12}/references/[a-z0-9-]+\.md", extra)
                or extra.split("/")[1].removeprefix("knowledge-") not in selected):
            raise ValueError("Additional resource is outside this write_set")
        paths.append(extra)
    paths = list(dict.fromkeys(paths))
    for path in paths:
        if not inside(root, caller / path).is_file():
            raise ValueError("A returned file is missing: " + path)
    response = {"files": [{"path": path, "content_ref": path, "inputs": [rid]} for path in paths]}
    reference = "prepared/files.json"
    write(root, caller / reference, response)
    return {"file_response_ref": reference, "file_count": len(paths), "write_set": sorted(selected)}


def run(root, data):
    if data.get("mode") == "index":
        return source_index(root, data)
    if data.get("mode") == "prepare_collection":
        return prepare_collection(root, data)
    if data.get("mode") == "plan_collection":
        return plan_collection(root, data)
    if data.get("mode") == "manifest":
        return manifest(root, data)
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


