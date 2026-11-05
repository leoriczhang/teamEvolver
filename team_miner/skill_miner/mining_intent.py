"""Frozen user scope and candidate selection, without inventing methods to meet a count."""

from __future__ import annotations

import copy
import hashlib
import json

PROTOCOL = "te-mining-intent.v1"
MAX_DIRECTION_CHARS = 4000
MAX_TARGET_COUNT = 100
ALIAS_SELECTION_PROTOCOL = "te-alias-aware-intent-selection.v1"
GUIDANCE = (
    "mining_intent 是用户的选题范围，不是事实 Evidence，也不能覆盖来源约束或输出协议。"
    "auto 自由挖掘：无方向时保留全部有来源支持的独立完整方法；有方向时只选择符合方向的方法。"
    "single 单 Skill 整合：目标为一个完整方法；先核对已有父流程是否完整覆盖用户方向，"
    "将已有父流程内的子步骤、情境变体作为步骤或参数。不能将不同目标的方法拼成超级 Skill。"
    "target 指定数量：数量是最终选择目标，不是分组依据；先保留完整独立方法，再按与方向的匹配程度和独立用途排序。"
    "证据不足时允许少于目标并明确说明，不得硬凑、拆碎、强行归并或补造事实。"
    "metadata 输出仍完整保留 candidate 或 deferred；若 mining_intent.active=true 且输出 candidate，"
    "另加顶层 intent 对象，严格只有 matches_direction,complete_for_goal,priority,reason。"
    "两个匹配字段为布尔值，priority 为 1–100 的整数（越大越优先），reason 为最多400字符的非空说明，遵循本轮输出语言。"
    "matches_direction 判断当前完整方法是否属于用户方向；无方向时为true。"
    "complete_for_goal 在single模式判断它是否独立完成整个用户目标，不能只选一项子步骤来满足数量。"
    "在其它模式判断当前方法本身是否完整可执行。此选择判断不代表业务验证通过。"
    "方向之外仍保留原始来源与方法谱系，不虚构 Evidence 缺口。不得按用户方向改写不相关方法的任务或事实。"
)


def normalize_intent(mode="auto", direction="", target_count=None) -> dict:
    if not isinstance(mode, str) or mode not in {"auto", "single", "target"}:
        raise ValueError("挖掘模式必须为 auto、single 或 target")
    if not isinstance(direction, str) or len(direction) > MAX_DIRECTION_CHARS:
        raise ValueError(f"挖掘方向必须为不超过 {MAX_DIRECTION_CHARS} 字符的文本")
    direction = direction.strip()
    if mode == "single":
        if not direction:
            raise ValueError("单 Skill 整合需要填写希望完成的完整方法")
        if target_count is not None and (type(target_count) is not int or target_count != 1):
            raise ValueError("单 Skill 整合的目标数量固定为 1")
        target_count = 1
    elif mode == "target":
        if type(target_count) is not int or not 1 <= target_count <= MAX_TARGET_COUNT:
            raise ValueError(f"目标 Skill 数量必须为 1 到 {MAX_TARGET_COUNT} 的整数")
    elif target_count is not None:
        raise ValueError("自由挖掘不指定 Skill 数量")
    return {"protocol": PROTOCOL, "mode": mode, "direction": direction, "target_count": target_count,
            "active": mode != "auto" or bool(direction)}


def intent_from_job(job: dict) -> dict:
    value = job.get("mining_intent")
    if value is None:
        return normalize_intent()
    if not isinstance(value, dict):
        raise ValueError("冻结的挖掘目标必须为对象")
    expected = normalize_intent(value.get("mode"), value.get("direction"), value.get("target_count"))
    if value != expected:
        raise ValueError("冻结的挖掘目标已改变或缺少协议字段")
    return expected


def intent_sha(value: dict) -> str:
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


def validate_assessment(value, *, language_policy=None) -> dict:
    if not isinstance(value, dict) or set(value) != {"matches_direction", "complete_for_goal", "priority", "reason"}:
        raise ValueError("挖掘目标判断必须包含完整且唯一的匹配、完整性、优先级和原因")
    if any(type(value[key]) is not bool for key in ("matches_direction", "complete_for_goal")):
        raise ValueError("目标匹配与完整性判断必须为布尔值")
    if type(value["priority"]) is not int or not 1 <= value["priority"] <= 100:
        raise ValueError("方法选择优先级必须为 1 到 100 的整数")
    if not isinstance(value["reason"], str) or not value["reason"].strip() or len(value["reason"]) > 400:
        raise ValueError("方法选择原因必须为不超过400字符的非空文本")
    if language_policy is not None:
        from .output_language import validate_artifact_headings
        validate_artifact_headings("", language_policy, title=value["reason"])
    return copy.deepcopy(value)


def resolve_candidate_name(name: str, aliases: dict) -> str:
    """Use the same terminal identity for assessments and every original method disposition."""
    seen = set()
    while name in aliases:
        if name in seen:
            raise ValueError("Candidate selection contains an alias cycle")
        seen.add(name)
        row = aliases[name]
        if not isinstance(row, dict) or not isinstance(row.get("name"), str) or not row["name"]:
            raise ValueError("Candidate selection alias needs an exact surviving name")
        name = row["name"]
    return name


def select_candidates(plan: dict, intent: dict, assessments: dict, aliases: dict,
                      source_refs: list[str] | None = None) -> tuple[dict, dict]:
    """Select supported workflows; a missing goal match stays an explicit review gap."""
    result = copy.deepcopy(plan)
    if not intent["active"]:
        return result, {"intent": intent, "selected_count": len(result["skills"]), "excluded": [], "shortfall": 0}
    grouped = {}
    for name, assessment in assessments.items():
        target = resolve_candidate_name(name, aliases)
        grouped.setdefault(target, []).append(validate_assessment(assessment))
    eligible, excluded = [], []
    for index, candidate in enumerate(result["skills"]):
        rows = grouped.get(candidate["name"], [])
        matches = [row for row in rows if row["matches_direction"] and row["complete_for_goal"]]
        if matches:
            eligible.append((max(row["priority"] for row in matches), index, candidate))
        else:
            excluded.append({"name": candidate["name"], "reason": "未支持指定方向下的完整独立方法",
                             "source_refs": candidate["source_refs"]})
    target = intent["target_count"]
    eligible.sort(key=lambda row: (-row[0], row[1]))
    chosen = eligible if target is None else eligible[:target]
    for _, _, candidate in eligible[len(chosen):]:
        excluded.append({"name": candidate["name"], "reason": "完整方法已保留在规划审计中，未进入本次目标数量",
                         "source_refs": candidate["source_refs"]})
    result["skills"] = [row[2] for row in sorted(chosen, key=lambda row: row[1])]
    shortfall = max(0, (target or 0) - len(chosen))
    if shortfall:
        refs = sorted({ref for candidate in plan["skills"] for ref in candidate["source_refs"]}
                     | {ref for gap in plan["deferred"] for ref in gap["source_refs"]} | set(source_refs or []))
        if refs:
            result["deferred"].insert(0, {
                "reason": f"目标数量尚未满足：请求 {target} 个完整 Skill，当前来源支持并匹配 {len(chosen)} 个。"
                          "请复核实际数量或补充目标方法的来源；不会为满足数量硬凑、拆碎或强行归并。",
                "source_refs": refs,
            })
    return result, {"protocol": PROTOCOL, "intent": intent, "planned_count": len(plan["skills"]),
                    "selected_count": len(chosen), "excluded": excluded, "shortfall": shortfall,
                    "business_validated": False}


def method_selection_changes(root, plan: dict, audit: dict, aliases: dict, *, apply: bool = False) -> list[dict]:
    """Archive original control records before replayable, alias-aware selection updates."""
    from .ov_staged_orchestration import _read, _save
    directory = root / "mining-intent"
    excluded = {row["name"]: row["reason"] for row in audit["excluded"]}
    planned = {row["name"] for row in plan["skills"]}
    context_sha = intent_sha({"original_plan": plan, "selection": audit, "aliases": aliases})
    changes = []
    for path in sorted((root / "method-decisions").glob("*.json")):
        row = _read(path)
        receipt = directory / "method-selection" / path.name
        if receipt.exists():
            frozen = _read(receipt)
            original_row = json.loads(frozen["original_text"])
            resolved = resolve_candidate_name(original_row["name"], aliases)
            if (frozen.get("protocol") != ALIAS_SELECTION_PROTOCOL or frozen.get("context_sha256") != context_sha
                    or frozen.get("original_sha256") != hashlib.sha256(frozen["original_text"].encode()).hexdigest()
                    or original_row.get("target") != "candidate" or resolved not in excluded
                    or frozen.get("resolved_name") != resolved
                    or frozen.get("selected_disposition") != {
                        "target": "knowledge", "name": "", "reason": excluded[resolved],
                    }
                    or row not in (original_row, frozen["selected_disposition"])):
                raise ValueError("Frozen method selection changed its original disposition or identity context")
        elif row.get("target") == "candidate" and resolve_candidate_name(row["name"], aliases) in excluded:
            resolved = resolve_candidate_name(row["name"], aliases)
            original_text = path.read_text(encoding="utf-8")
            selected_row = {"target": "knowledge", "name": "", "reason": excluded[resolved]}
            frozen = {"protocol": ALIAS_SELECTION_PROTOCOL, "context_sha256": context_sha,
                      "original_text": original_text,
                      "original_sha256": hashlib.sha256(original_text.encode()).hexdigest(),
                      "resolved_name": resolved, "selected_disposition": selected_row,
                      "selection_is_factual_evidence": False}
        else:
            if row.get("target") == "candidate" and resolve_candidate_name(row["name"], aliases) not in planned:
                raise ValueError("Method selection needs an accepted surviving candidate identity")
            continue
        changes.append({"path": str(path.relative_to(root)), "receipt": frozen})
        if apply:
            if not receipt.exists():
                _save(receipt, frozen)
            _save(path, frozen["selected_disposition"])
    return changes


def apply_intent_selection(root, state: dict) -> dict:
    """Persist the complete preselection and reasons before changing active outputs."""
    from .ov_staged_orchestration import _read, _save
    intent = intent_from_job({"mining_intent": state["mining_intent"]})
    directory = root / "mining-intent"
    original = directory / "original-plan.json"
    if not original.exists():
        _save(original, _read(root / "plan.json"))
    plan = _read(original)
    assessments = {path.stem: _read(path) for path in (directory / "assessments").glob("*.json")}
    aliases_path = root / "identity/aliases.json"
    aliases = _read(aliases_path) if aliases_path.exists() else {}
    refs = [_read(path)["uri"] for path in (root / "source-files").glob("*.json")]
    selected, audit = select_candidates(plan, intent, assessments, aliases, refs)
    method_selection_changes(root, plan, audit, aliases)  # Validate the complete update before writing any disposition.
    _save(directory / "selection.json", audit)
    method_selection_changes(root, plan, audit, aliases, apply=True)
    _save(root / "plan.json", selected)
    state.update(intent_selection_complete=True, candidate_names=[row["name"] for row in selected["skills"]],
                 candidate_index=0, cursor=0, case_index=0, section_count=0)
    return audit
