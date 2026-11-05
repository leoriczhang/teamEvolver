"""Skill-owned mechanical assembly; model records supply semantics, never host matching."""
from __future__ import annotations

import hashlib
import json
import re
import sys
from pathlib import Path, PurePosixPath

RECORD_ID = re.compile(r"[0-9a-f]{24}")
SLUG = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*")
PREFIX = "compiled_skill/instructor-skill-router"
BODY = """---
name: instructor-skill-router
description: 从资源目录选择现有Skill，按需加载完整约束并隔离任务与场景状态。
---

# 现有 Skill 的使用入口

先读取 [完整资源目录](references/skill-catalog.json)。目录是导航，源Skill仍作为外部依赖保存。

## 选择与加载

1. 按调用方明确的Skill名称或标识选择目录条目。课程材料中知识点、任务、场景的ID属于不同
   命名空间；章节包含多个知识点，知识点包含多个任务，场景可关联多个知识点，按目录真实关系选择。
   其他领域按其自身关系使用。目标有歧义或缺少影响选择的信息时先确认，不自行设置默认难度。
2. 目录顶层dependency_root非空时，以它为根拼接条目的skill_path及config_path；根为null时，
   使用各条目明确给出的dependency_root。路径指向外部资源，不是本入口内置文件。
   当前环境不能读取依赖时，先说明缺少哪个资源位置，不假称已加载或已调用其他Agent/API。
3. 只加载选中的Skill完整正文及本次必要配置。目录中的mode等摘要只作导航，具体动作、完成要求、
   角色、人物、数值、条件、例外及输出契约均以实际已读的被选资源为准，不一次加载整个集合。
4. 先查看条目的gaps。历史生成产物不自动成为核实后的业务事实；所需背景、答案或互动模式存在
   缺口时，保留其来源定位，说明当前哪个必要动作无法确定并确认。不借相邻成员、其他难度或示例补事实。

## 执行与切换

5. 在明确的训练范围内执行被选Skill，加载相应任务和场景的完整要求，不能只替换背景文本。
   互动模式、追问次数、数字要求、难度含义、完成门槛和输出字段只在来源明确规定的适用范围内生效。
   本入口不另设统一次数、评分或通过标准，不把目录摘要当作新的业务规则。
6. 使用本次输入的对话历史和上轮状态，保持Skill、知识点、任务及场景的作用域。切换时保留
   各任务自己的完成记录，不把其他任务的完成结果、人物或案例数值带入新场景。
7. 遵守实际已读的被选Skill输出协议。路由尚未确定时先处理选择问题，不伪装成正在带练；
   本入口不重新承担被引用Skill的完整执行正文。
"""


def inside(root, path):
    if path.is_symlink() or root.resolve() not in path.resolve().parents:
        raise ValueError("Router path is outside this Compile workspace")
    return path


def load(root, path):
    return json.loads(inside(root, path).read_text(encoding="utf-8"))


def write(root, path, value):
    inside(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    text = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2) + "\n"
    path.write_text(text, encoding="utf-8")


def path_under(root, relative):
    if (not isinstance(relative, str) or not relative or "\\" in relative
            or PurePosixPath(relative).is_absolute() or ".." in PurePosixPath(relative).parts
            or any(p in {"", "."} for p in relative.split("/"))):
        raise ValueError("A dependency path must be an explicit relative path")
    return root.rstrip("/") + "/" + relative


def assignment_for(root, data):
    anchor = data.get("anchor_input_id")
    if not isinstance(anchor, str) or not RECORD_ID.fullmatch(anchor):
        raise ValueError("A real assigned anchor record ID is required")
    assignments = list((root / "__compile_staging__/drafts").glob("*/assignment.json"))
    if len(assignments) > 512:
        raise ValueError("Too many assignments")
    found = []
    for path in assignments:
        assignment = load(root, path)
        if any(r["id"] == anchor for r in assignment.get("inputs", [])):
            found.append((path.parent, assignment))
    if len(found) != 1:
        raise ValueError("Cannot identify exactly one assigned router collection")
    caller, assignment = found[0]
    ids = [r["id"] for r in assignment["inputs"]]
    if not 1 <= len(ids) <= 256 or len(ids) != len(set(ids)):
        raise ValueError("The directory needs a bounded unique input collection")
    return caller, ids


def bundle_entry(text):
    def header(label):
        matches = re.findall(r"(?m)^" + re.escape(label) + r"[:：]([^\r\n]+)$", text)
        if len(matches) != 1:
            raise ValueError("The source bundle needs one explicit resource header: " + label)
        return matches[0].strip()

    root = header("依赖根目录URI").rstrip("/")
    skill_uri, config_uri = header("原SKILL资源URI"), header("原配置资源URI")
    if not skill_uri.startswith(root + "/") or not config_uri.startswith(root + "/"):
        raise ValueError("Original Skill resources do not belong to the declared dependency root")
    slug = PurePosixPath(skill_uri).parent.name
    marker = "## 原始references/knowledge-config.json"
    if text.count(marker) != 1:
        raise ValueError("The source bundle needs exactly one complete original configuration")
    section = text.split(marker, 1)[1]
    opening = section.find("```json")
    if opening < 0:
        raise ValueError("The original configuration has no explicit JSON boundary")
    cfg, _ = json.JSONDecoder().raw_decode(section[opening + len("```json"):].lstrip())
    point = cfg.get("knowledge_point") or cfg.get("knowledge") or {}
    kid = cfg.get("knowledge_id", point.get("id"))
    name = cfg.get("knowledge_name", point.get("name", point.get("title")))
    if kid is None or not name or slug != "knowledge-" + str(kid):
        raise ValueError("Original bundle identity and configuration do not agree")
    tasks = [{"id": str(t.get("task_id", t.get("id"))),
              "title": t.get("task_title", t.get("title", t.get("name")))} for t in cfg["tasks"]]
    if any(t["id"] == "None" or not t["title"] for t in tasks):
        raise ValueError("An original task has no explicit identity or title")
    gaps = [{"source_field": k, "value": cfg[k]} for k in ("unresolved", "deferred", "deferred_items")
            if cfg.get(k)]
    scenes = []
    for source in cfg["scenes"]:
        sid = str(source.get("scene_id", source.get("id")))
        if sid == "None" or "difficulty" not in source:
            raise ValueError("An original scene has no explicit identity or difficulty")
        memberships = source.get("knowledge_point_ids", source.get("knowledge_points"))
        if memberships is None:
            memberships = [str(kid)]
            basis = "本场景位于该知识点配置中；来源未另列跨知识点归属。"
        else:
            basis = "保留来源显式归属字段，名称只在目录存在唯一对应项时转换为知识点ID。"
        if not isinstance(memberships, list):
            raise ValueError("Original scene membership is not an explicit list")
        declared = source.get("interaction_mode", cfg.get("interaction_mode"))
        def concerns_scene(value):
            if isinstance(value, dict):
                if str(value.get("scene_id", "")) == sid:
                    return True
                return any(concerns_scene(k) or concerns_scene(v) for k, v in value.items())
            if isinstance(value, list):
                return any(concerns_scene(v) for v in value)
            return bool(re.search(r"(?:场景\s*|(?<![a-zA-Z])scene[_ .-]*)" + re.escape(sid) + r"(?!\d)",
                                  str(value), re.I))

        related = [g for g in gaps if concerns_scene(g["value"])]
        note = source.get("note")
        if note:
            related.append({"source_field": "scenes." + sid + ".note", "value": note})
            gaps.append(related[-1])
        scenes.append({"id": sid, "difficulty": source["difficulty"],
                       "knowledge_point_ids": [str(v) for v in memberships if str(v).isdigit()],
                       "knowledge_point_names": [str(v) for v in memberships if not str(v).isdigit()],
                       "membership_basis": basis, "source_membership": memberships,
                       "mode": "未确认" if related or not declared else declared,
                       "source_declared_mode": declared,
                       "mode_basis": "历史配置的声明仅作导航；已有缺口时须读取相应完整原文核实。"})
    if len({t["id"] for t in tasks}) != len(tasks) or len({s["id"] for s in scenes}) != len(scenes):
        raise ValueError("Original task or scene identities are repeated within one Skill")
    return {"slug": slug, "knowledge_id": str(kid), "name": name,
            "chapter": cfg.get("chapter", point.get("chapter")),
            "tasks": tasks, "scenes": scenes, "gaps": gaps, "dependency_root": root,
            "skill_path": skill_uri[len(root) + 1:], "config_path": config_uri[len(root) + 1:]}


def index(root, data):
    caller, ids = assignment_for(root, data)
    pipeline = root / "__compile_staging__/pipeline"
    parts = []
    for rid in ids:
        if not RECORD_ID.fullmatch(rid):
            raise ValueError("Invalid original range ID")
        part = load(root, pipeline / "sources" / (rid + ".json"))
        if "/source/original/" not in part["uri"]:
            raise ValueError("Initial router indexing accepts only original bundles")
        parts.append(part)
    if len({p["uri"] for p in parts}) != 1:
        raise ValueError("Each indexing assignment must contain one complete original bundle")
    chunks, end = [], 0
    for part in sorted(parts, key=lambda p: p["start_char"]):
        if part["start_char"] != end or part["end_char"] != end + len(part["text"]):
            raise ValueError("The assigned original bundle has missing or overlapping ranges")
        chunks.append(part["text"])
        end = part["end_char"]
    entry = bundle_entry("".join(chunks))
    entry["source_refs"] = [parts[0]["uri"]]
    entry["source_bundle_sha256"] = hashlib.sha256("".join(chunks).encode()).hexdigest()
    reference = "prepared/router-entry.json"
    write(root, caller / reference, {"records": [{"inputs": ids, "scope": {}, "evidence_spans": [],
        "routing_text": entry["slug"], "payload": {"kind": "skill-entry", "entry": entry}}]})
    return {"ready_to_emit": True, "record_response_ref": reference, "slug": entry["slug"],
            "next_tool_call": {"name": "emit", "arguments": {"result_ref": reference}}}


def run(root, data):
    if data.get("mode", "assemble") == "index":
        return index(root, data)
    if data.get("mode", "assemble") != "assemble":
        raise ValueError("Unknown router preparation mode")
    caller, ids = assignment_for(root, data)
    pipeline = root / "__compile_staging__/pipeline"
    entries, slugs = [], set()
    for rid in ids:
        if not RECORD_ID.fullmatch(rid):
            raise ValueError("Invalid assigned directory record ID")
        record = load(root, pipeline / "records" / (rid + ".json"))
        fields = load(root, pipeline / (record["payload_ref"] + ".json"))["fields"]
        if fields.get("kind") != "skill-entry":
            raise ValueError("Initial router assembly accepts only source Skill directory records")
        entry = dict(fields.get("entry", fields))
        entry.pop("kind", None)
        slug = entry.get("slug")
        if not isinstance(slug, str) or not SLUG.fullmatch(slug) or len(slug) > 64 or slug in slugs:
            raise ValueError("A source Skill slug is missing, invalid, or repeated")
        slugs.add(slug)
        dependency = entry.get("dependency_root")
        if not isinstance(dependency, str) or not dependency.strip():
            raise ValueError("A source Skill dependency root is missing")
        for field in ["skill_path", "config_path"]:
            path_under(dependency, entry[field])
        if not entry.get("name") or "gaps" not in entry:
            raise ValueError("Every directory entry needs its actual name and explicit gaps")
        refs = set()
        for sid in record.get("source_refs", []):
            if not isinstance(sid, str) or not RECORD_ID.fullmatch(sid):
                raise ValueError("Invalid assigned original range ID")
            source = load(root, pipeline / "sources" / (sid + ".json"))
            if "/source/original/" not in source["uri"]:
                raise ValueError("A prior result cannot become an original router member")
            refs.add(source["uri"])
        if not refs:
            raise ValueError("A directory member has no actual original source")
        entry["source_refs"] = sorted(refs)
        entries.append(entry)
    entries.sort(key=lambda e: e["slug"])
    identities = {}
    for entry in entries:
        if entry.get("knowledge_id"):
            identities.setdefault(entry["name"], []).append(str(entry["knowledge_id"]))
    for entry in entries:
        for scene in entry.get("scenes", []):
            mapped = list(scene.get("knowledge_point_ids", []))
            unknown = []
            for name in scene.get("knowledge_point_names", []):
                matching = identities.get(name, [])
                if len(matching) == 1:
                    mapped.extend(matching)
                else:
                    unknown.append(name)
            scene["knowledge_point_ids"] = list(dict.fromkeys(mapped))
            if unknown:
                entry["gaps"].append({"source_field": "scene-membership", "scene_id": scene["id"],
                    "value": "目录没有唯一对应知识点ID，保留来源名称：" + "、".join(unknown)})
    roots = {entry["dependency_root"] for entry in entries}
    common_root = next(iter(roots)) if len(roots) == 1 else None
    if common_root is not None:
        for entry in entries:
            entry.pop("dependency_root")
    catalog = {"schema_version": "existing-skill.catalog.v1", "dependency_root": common_root, "entries": entries}
    skill_path, catalog_path = PREFIX + "/SKILL.md", PREFIX + "/references/skill-catalog.json"
    write(root, caller / skill_path, BODY)
    write(root, caller / catalog_path, catalog)
    reference = "prepared/router-files.json"
    write(root, caller / reference, {"files": [
        {"path": p, "content_ref": p, "inputs": ids} for p in [skill_path, catalog_path]
    ]})
    return {"ready_to_emit": True, "file_response_ref": reference, "entry_count": len(entries),
            "catalog_sha256": hashlib.sha256((caller / catalog_path).read_bytes()).hexdigest(),
            "next_tool_call": {"name": "emit", "arguments": {"result_ref": reference}}}


def main():
    request = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = run(Path(request["workspace"]).resolve(), request["data"])
    Path(sys.argv[2]).write_text(json.dumps(result, ensure_ascii=False), encoding="utf-8")


if __name__ == "__main__":
    main()
