"""Pure, bounded additive Skill bodies; source attribution is not semantic approval.

The host freezes exact source units and their method/context roles. A patch can add
an atom or attach a condition, example or unresolved conflict to an existing step.
It cannot replace, delete, merge or silently weaken an earlier accepted atom.
"""

from __future__ import annotations

import copy
import hashlib
import json
import re
from typing import Any

from team_skills.library.frontmatter import render_skill_md

from .output_language import _natural_text, validate_artifact_headings

PROTOCOL = "te-skill-body-patch.v1"
OUTPUT_SCHEMA = "skill-miner.body-patch.v1"
CAPACITY_SCHEMA = "skill-miner.body-patch.capacity.v1"
MAX_OUTPUT_CHARS = 12000
MAX_CONTROLS_CHARS = 40000
MAX_ATOMS = 256
MAX_STATE_CHARS = 60000
MAX_ASSIGNMENTS = 128
MAX_SOURCE_UNITS = 1024
MAX_ATOM_TEXT_CHARS = 1200
_ADD_KINDS = frozenset({"step", "limit", "example", "conflict"})
_ATTACH_KINDS = frozenset({"condition", "example", "conflict"})
_POLICY = {"language": "zh-CN"}
_RULE = (
    "当前 input_ids 必须逐项列出本批已查看的全部原文单元，不得漏项、重复或增加。"
    "support_ids 只选择当前原文单元。旧 atom 目录是定位上下文，不是新的 Evidence。"
    "已有相同步骤的新条件必须 attach 到其稳定 ID，勿因另一人物或来源重复 add 同一步骤。"
    "add step 必须至少有一个 operative 原文单元支持，context 不能独立授权新增步骤。"
    "attach 只能指向已有或本批新增的 step；条件、例外必须随所属动作保留。"
    "只允许新增和标记未决 conflict，不允许覆盖、删除、合并旧 atom 或自动裁定冲突事实。"
    "新 ID 从 next_atom_id 起，按 add 然后 attach 的顺序连续编号。"
    "step_order 必须是全部已有及新增 step ID 的严格全排列，表示执行顺序，不是来源遍历顺序。"
    "可以据本批 Evidence 调整顺序但不得删步骤或条件；无新增的顺序调整需在 no_change_reason 说明依据。"
    "title 首次生成后冻结；后续必须原样返回。全部面向人的标题、文字和理由必须中文。"
    "无新增时 add/attach 均为空，并填写中文 no_change_reason；有新增时该字段为空字符串。"
    "容量不足返回完整 capacity schema，禁止截断来源、旧状态、条件或半份 patch。"
)


class BodyPatchError(ValueError):
    """A frozen assignment, prior body or model patch violates its exact contract."""


class BodyPatchCapacityError(BodyPatchError):
    """The complete additive result exceeds a declared bound; no atom was dropped."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)


def _sha(value: Any) -> str:
    return hashlib.sha256(_json(value).encode()).hexdigest()


def _digest(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"[a-f0-9]{64}", value) is not None


def _text(value: Any, maximum: int, label: str, *, empty: bool = False) -> str:
    if not isinstance(value, str) or len(value) > maximum or (not empty and not value.strip()):
        raise BodyPatchError(f"{label} must be {'0' if empty else '1'}-{maximum} characters")
    return value


def _chinese(value: Any, maximum: int, label: str, *, empty: bool = False) -> str:
    text = _text(value, maximum, label, empty=empty)
    if text:
        natural = _natural_text(text)
        if any(char.isalpha() for char in natural) and not any("\u3400" <= char <= "\u9fff" for char in natural):
            raise BodyPatchError(f"{label} 必须使用中文；机器标识、代码和来源引用可保持原样")
        validate_artifact_headings(text, _POLICY)
    return text


def _ids(value: Any, label: str, *, empty: bool = False) -> list[str]:
    if (not isinstance(value, list) or (not value and not empty)
            or any(not isinstance(x, str) or not x or x.strip() != x or len(x) > 160 for x in value)
            or len(set(value)) != len(value)):
        raise BodyPatchError(f"{label} must contain exact unique nonempty IDs")
    return value


def _candidate(candidate: Any) -> dict:
    if not isinstance(candidate, dict):
        raise BodyPatchError("A complete frozen candidate is required")
    if not isinstance(candidate.get("name"), str) or not re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", candidate["name"]):
        raise BodyPatchError("Candidate name must retain its machine slug")
    _chinese(candidate.get("task"), 8000, "candidate.task")
    for field in ("trigger", "boundary"):
        if field in candidate:
            _chinese(candidate[field], 8000, "candidate." + field, empty=True)
    for field in ("inputs", "outputs"):
        if field in candidate:
            if not isinstance(candidate[field], list) or len(candidate[field]) > 64:
                raise BodyPatchError("Candidate inputs/outputs must be bounded text lists")
            for value in candidate[field]:
                _chinese(value, 2000, "candidate." + field)
    if len(_json(candidate)) > 20000:
        raise BodyPatchCapacityError("Complete candidate exceeds 20000 characters")
    return copy.deepcopy(candidate)


def _limits(max_atoms: int, max_state_chars: int, max_controls_chars: int, max_output_chars: int) -> dict:
    values = dict(max_atoms=max_atoms, max_state_chars=max_state_chars,
                  max_controls_chars=max_controls_chars, max_output_chars=max_output_chars)
    ceilings = dict(max_atoms=MAX_ATOMS, max_state_chars=MAX_STATE_CHARS,
                    max_controls_chars=MAX_CONTROLS_CHARS, max_output_chars=MAX_OUTPUT_CHARS)
    for key, value in values.items():
        if type(value) is not int or not 1 <= value <= ceilings[key]:
            raise BodyPatchError(f"{key} must be within 1-{ceilings[key]}")
    return values


def _seal(state: dict) -> dict:
    state["state_sha256"] = _sha({k: v for k, v in state.items() if k != "state_sha256"})
    return state


def new_body_state(candidate: dict) -> dict:
    """Create a fresh body, bound to the complete frozen candidate metadata."""
    candidate = _candidate(candidate)
    return _seal({"protocol": PROTOCOL, "candidate_sha256": _sha(candidate), "title": "",
                  "atoms": [], "step_order": [], "sources": {}, "receipts": []})


def _source(unit: Any, role: str) -> dict:
    if not isinstance(unit, dict):
        raise BodyPatchError("Original source units must be objects")
    _ids([unit.get("id")], "source.id")
    content = _text(unit.get("content"), 48000, "source.content")
    if (not isinstance(unit.get("source_uri"), str) or not unit["source_uri"].startswith("viking://")
            or len(unit["source_uri"]) > 2000
            or any(type(unit.get(k)) is not int for k in ("start_char", "end_char"))
            or not 0 <= unit["start_char"] < unit["end_char"]
            or unit["end_char"] - unit["start_char"] != len(content)
            or unit.get("sha256") != hashlib.sha256(content.encode()).hexdigest()
            or not _digest(unit.get("source_sha256"))):
        raise BodyPatchError("Source unit requires exact original offsets/content/SHA and source SHA")
    return {key: unit[key] for key in ("id", "source_uri", "start_char", "end_char", "sha256", "source_sha256")} | {
        "role": role,
    }


def validate_body_state(state: Any, candidate: dict | None = None) -> dict:
    """Check persisted structure and self-hash; the host must also verify its frozen file SHA."""
    fields = {"protocol", "candidate_sha256", "title", "atoms", "step_order", "sources", "receipts", "state_sha256"}
    if (not isinstance(state, dict) or set(state) != fields or state["protocol"] != PROTOCOL
            or state["state_sha256"] != _sha({k: v for k, v in state.items() if k != "state_sha256"})):
        raise BodyPatchError("Body state protocol or complete state hash changed")
    if candidate is not None and state["candidate_sha256"] != _sha(_candidate(candidate)):
        raise BodyPatchError("Body belongs to a different frozen candidate")
    if not _digest(state["candidate_sha256"]):
        raise BodyPatchError("Body candidate hash must retain its complete digest")
    _chinese(state["title"], 200, "title", empty=True)
    if state["title"]:
        validate_artifact_headings("", _POLICY, title=state["title"])
    atoms, sources, receipts = state["atoms"], state["sources"], state["receipts"]
    if (not isinstance(atoms, list) or len(atoms) > MAX_ATOMS
            or not isinstance(sources, dict) or len(sources) > MAX_SOURCE_UNITS
            or not isinstance(receipts, list) or len(receipts) > MAX_ASSIGNMENTS
            or len(_json(state)) > MAX_STATE_CHARS):
        raise BodyPatchCapacityError("Complete body state exceeds its bounded storage contract")
    for identity, source in sources.items():
        _ids([identity], "stored source ID")
        if (not isinstance(source, dict) or set(source) != {
            "id", "source_uri", "start_char", "end_char", "sha256", "source_sha256", "role",
        } or source["id"] != identity or source["role"] not in ("operative", "context")
                or not isinstance(source["source_uri"], str) or not source["source_uri"].startswith("viking://")
                or not _digest(source["sha256"]) or not _digest(source["source_sha256"])
                or type(source["start_char"]) is not int or type(source["end_char"]) is not int
                or not 0 <= source["start_char"] < source["end_char"]):
            raise BodyPatchError("Stored source support metadata changed")
    steps = set()
    for index, atom in enumerate(atoms, 1):
        if (not isinstance(atom, dict)
                or set(atom) != {"id", "kind", "parent_id", "text", "support_ids"}
                or atom["id"] != f"a{index:04d}"):
            raise BodyPatchError("Body atoms must retain stable contiguous IDs and exact fields")
        _chinese(atom["text"], MAX_ATOM_TEXT_CHARS, "atom.text")
        support = _ids(atom["support_ids"], "atom.support_ids")
        if any(identity not in sources for identity in support):
            raise BodyPatchError("Body atom has unknown original support")
        if atom["parent_id"] is None:
            if not isinstance(atom["kind"], str) or atom["kind"] not in _ADD_KINDS:
                raise BodyPatchError("A top-level atom has an invalid kind")
        elif (not isinstance(atom["parent_id"], str) or atom["parent_id"] not in steps
              or not isinstance(atom["kind"], str) or atom["kind"] not in _ATTACH_KINDS):
            raise BodyPatchError("Attached atoms must retain an earlier step parent")
        if atom["kind"] == "step":
            if not any(sources[x].get("role") == "operative" for x in support):
                raise BodyPatchError("Context alone cannot authorize a step")
            steps.add(atom["id"])
    order = _ids(state["step_order"], "step_order", empty=True)
    if set(order) != steps:
        raise BodyPatchError("step_order must contain every step exactly once")
    if atoms and not state["title"]:
        raise BodyPatchError("A nonempty body must have its frozen Chinese title")
    assignments, covered = set(), set()
    for receipt in receipts:
        if (not isinstance(receipt, dict) or set(receipt) != {
            "assignment_id", "assignment_sha256", "output_sha256", "input_ids",
        } or not isinstance(receipt["assignment_id"], str) or not receipt["assignment_id"].strip()
                or receipt["assignment_id"] in assignments or not _digest(receipt["assignment_sha256"])
                or not _digest(receipt["output_sha256"])):
            raise BodyPatchError("Body receipts must retain unique exact assignments")
        ids = _ids(receipt["input_ids"], "receipt.input_ids")
        if covered.intersection(ids):
            raise BodyPatchError("A source unit cannot be silently processed twice")
        assignments.add(receipt["assignment_id"])
        covered.update(ids)
    if not set(sources) <= covered:
        raise BodyPatchError("Stored source support lacks an accepted coverage receipt")
    return copy.deepcopy(state)


def _assemble(candidate: dict, state: dict, evidence: list[dict], operative_ids: list[str],
              context_ids: list[str], assignment_id: str, limits: dict) -> tuple[dict, dict]:
    candidate, state = _candidate(candidate), validate_body_state(state, candidate)
    _text(assignment_id, 200, "assignment_id")
    if not isinstance(evidence, list) or not 1 <= len(evidence) <= MAX_SOURCE_UNITS:
        raise BodyPatchError("A patch needs bounded complete original source units")
    inputs = _ids([row.get("id") if isinstance(row, dict) else None for row in evidence], "input_ids")
    operative, context = set(_ids(operative_ids, "operative_ids", empty=True)), set(
        _ids(context_ids, "context_ids", empty=True)
    )
    if operative.intersection(context) or operative | context != set(inputs):
        raise BodyPatchError("Exact source roles must partition every assigned source unit")
    if any(row["assignment_id"] == assignment_id for row in state["receipts"]):
        raise BodyPatchError("An accepted assignment cannot be rebuilt against a later state")
    covered = {x for row in state["receipts"] for x in row["input_ids"]}
    if covered.intersection(inputs):
        raise BodyPatchError("Assigned original source units already have accepted coverage")
    sources = {unit["id"]: _source(unit, "operative" if unit["id"] in operative else "context") for unit in evidence}
    authority = {"protocol": PROTOCOL, "candidate": candidate, "base_state": state,
                 "evidence": copy.deepcopy(evidence), "operative_ids": list(operative_ids),
                 "context_ids": list(context_ids), "assignment_id": assignment_id, "limits": limits}
    assignment_sha = _sha(authority)
    controls = {
        "protocol": PROTOCOL, "output_schema": OUTPUT_SCHEMA, "capacity_schema": CAPACITY_SCHEMA,
        "assignment_sha256": assignment_sha, "base_state_sha256": state["state_sha256"],
        "candidate": {k: candidate[k] for k in ("name", "task", "trigger", "inputs", "outputs", "boundary")
                      if k in candidate},
        "input_ids": inputs, "operative_ids": list(operative_ids), "context_ids": list(context_ids),
        "title": state["title"], "atom_directory": copy.deepcopy(state["atoms"]),
        "step_order": list(state["step_order"]),
        "directory_is_evidence": False, "directory_text_complete": True,
        "next_atom_id": f"a{len(state['atoms']) + 1:04d}", "limits": limits, "rule": _RULE,
    }
    manifest = {**authority, "assignment_sha256": assignment_sha, "sources": sources,
                "controls_sha256": _sha(controls)}
    manifest["manifest_sha256"] = _sha(manifest)
    return controls, manifest


def build_body_patch(candidate: dict, state: dict, evidence: list[dict], *, operative_ids: list[str],
                     context_ids: list[str], assignment_id: str, max_atoms: int = MAX_ATOMS,
                     max_state_chars: int = MAX_STATE_CHARS, max_controls_chars: int = MAX_CONTROLS_CHARS,
                     max_output_chars: int = MAX_OUTPUT_CHARS) -> dict:
    """Freeze a complete assignment. A capacity response never contains clipped controls."""
    limits = _limits(max_atoms, max_state_chars, max_controls_chars, max_output_chars)
    controls, manifest = _assemble(candidate, state, evidence, operative_ids, context_ids, assignment_id, limits)
    size = len(_json(controls))
    reason = ""
    if size > max_controls_chars:
        reason = "完整旧步骤目录及本批来源索引超过输入容量，需调整本批范围或显式处理容量，不能裁剪旧条件。"
    elif len(state["atoms"]) > max_atoms or len(_json(state)) > max_state_chars:
        reason = "完整已有候选正文超过本次容量；不能丢弃旧步骤。"
    elif len(state["receipts"]) >= MAX_ASSIGNMENTS:
        reason = "候选已达到有界来源批次数量；不能继续无界追加。"
    minimal = {"schema_version": CAPACITY_SCHEMA, "assignment_sha256": manifest["assignment_sha256"],
               "base_state_sha256": state["state_sha256"], "input_ids": controls["input_ids"], "reason": "容量不足。"}
    if len(_json(minimal)) > max_output_chars:
        reason = "本批全部来源单元编号已超过输出容量，必须分批而不能遗漏编号。"
    if reason:
        return {"status": "needs_batching", "reason": reason, "controls": None, "manifest": None,
                "capacity": {"controls_chars": size, "minimum_output_chars": len(_json(minimal))}}
    return {"status": "ready", "controls": controls, "manifest": manifest,
            "capacity": {"controls_chars": size, "minimum_output_chars": len(_json(minimal))}}


def validate_body_patch_manifest(manifest: Any, controls: dict | None = None) -> dict:
    if not isinstance(manifest, dict) or manifest.get("protocol") != PROTOCOL:
        raise BodyPatchError("Frozen body assignment protocol changed")
    try:
        limits = _limits(**manifest["limits"])
        expected_controls, expected = _assemble(
            manifest["candidate"], manifest["base_state"], manifest["evidence"], manifest["operative_ids"],
            manifest["context_ids"], manifest["assignment_id"], limits,
        )
    except (KeyError, TypeError) as exc:
        raise BodyPatchError("Frozen body assignment fields changed") from exc
    if manifest != expected or (controls is not None and controls != expected_controls):
        raise BodyPatchError("Frozen body assignment, original source or controls hash changed")
    return copy.deepcopy(manifest)


def _decoded(value: Any, maximum: int) -> dict:
    if isinstance(value, str):
        if len(value) > maximum:
            raise BodyPatchCapacityError("Complete raw body patch exceeds the output limit")
        try:
            def unique_fields(pairs):
                result = {}
                for key, item in pairs:
                    if key in result:
                        raise BodyPatchError("Body patch JSON cannot repeat a field")
                    result[key] = item
                return result

            value = json.loads(value, object_pairs_hook=unique_fields)
        except (ValueError, TypeError) as exc:
            raise BodyPatchError("Body patch must be complete JSON") from exc
    if not isinstance(value, dict):
        raise BodyPatchError("Body patch must be a JSON object")
    if len(_json(value)) > maximum:
        raise BodyPatchCapacityError("Complete body patch exceeds the output limit")
    return value


def validate_body_patch(output: Any, manifest: dict) -> dict:
    """Validate additions, exact per-unit seen coverage and frozen authority, without mutating."""
    manifest = validate_body_patch_manifest(manifest)
    value = _decoded(output, manifest["limits"]["max_output_chars"])
    common = {"schema_version", "assignment_sha256", "base_state_sha256", "input_ids"}
    capacity = value.get("schema_version") == CAPACITY_SCHEMA
    expected = common | ({"reason"} if capacity else {"title", "add", "attach", "step_order", "no_change_reason"})
    if set(value) != expected or value["schema_version"] not in (OUTPUT_SCHEMA, CAPACITY_SCHEMA):
        raise BodyPatchError("Body patch requires its exact declared schema fields")
    state = manifest["base_state"]
    if (value["assignment_sha256"] != manifest["assignment_sha256"]
            or value["base_state_sha256"] != state["state_sha256"]):
        raise BodyPatchError("Body patch assignment/base state hash does not match")
    inputs = _ids(value["input_ids"], "input_ids")
    if set(inputs) != {unit["id"] for unit in manifest["evidence"]}:
        raise BodyPatchError("Body patch must mark every current source unit seen exactly once")
    if capacity:
        _chinese(value["reason"], 400, "capacity.reason")
        return copy.deepcopy(value)
    _chinese(value["title"], 200, "title", empty=True)
    if value["title"]:
        validate_artifact_headings("", _POLICY, title=value["title"])
    if state["title"] and value["title"] != state["title"]:
        raise BodyPatchError("The first accepted Chinese title is frozen")
    if not isinstance(value["add"], list) or not isinstance(value["attach"], list):
        raise BodyPatchError("add and attach must be lists")
    count = len(value["add"]) + len(value["attach"])
    if len(state["atoms"]) + count > manifest["limits"]["max_atoms"]:
        raise BodyPatchCapacityError("Additive body exceeds max_atoms; no old atom may be dropped")
    _chinese(value["no_change_reason"], 400, "no_change_reason", empty=True)
    if bool(value["no_change_reason"].strip()) == bool(count):
        raise BodyPatchError("no_change_reason is required only when add and attach are empty")
    if count and not value["title"]:
        raise BodyPatchError("New body atoms require a Chinese title")
    steps = {atom["id"] for atom in state["atoms"] if atom["kind"] == "step"}
    offset = len(state["atoms"])
    for group, kinds in (("add", _ADD_KINDS), ("attach", _ATTACH_KINDS)):
        for atom in value[group]:
            fields = {"id", "kind", "text", "support_ids"} | ({"parent_id"} if group == "attach" else set())
            if not isinstance(atom, dict) or set(atom) != fields:
                raise BodyPatchError(f"{group} atom requires exact fields {sorted(fields)}")
            offset += 1
            if (atom["id"] != f"a{offset:04d}" or not isinstance(atom["kind"], str)
                    or atom["kind"] not in kinds):
                raise BodyPatchError("New atoms need contiguous host-assigned IDs and permitted kinds")
            _chinese(atom["text"], MAX_ATOM_TEXT_CHARS, "atom.text")
            support = _ids(atom["support_ids"], "atom.support_ids")
            if not set(support) <= set(inputs):
                raise BodyPatchError("Atom support must select exact current source units")
            if atom["kind"] == "step":
                if not set(support).intersection(manifest["operative_ids"]):
                    raise BodyPatchError("Context alone cannot authorize a new step")
                steps.add(atom["id"])
            if group == "attach" and (not isinstance(atom["parent_id"], str) or atom["parent_id"] not in steps):
                raise BodyPatchError("attach.parent_id must identify an existing or newly added step")
    order = _ids(value["step_order"], "step_order", empty=True)
    if set(order) != steps:
        raise BodyPatchError("step_order must contain every existing and added step exactly once")
    result = _apply(state, value, manifest)
    if len(_json(result)) > manifest["limits"]["max_state_chars"]:
        raise BodyPatchCapacityError("Complete additive body state exceeds capacity; no atom was clipped")
    validate_body_state(result, manifest["candidate"])
    return copy.deepcopy(value)


def _apply(state: dict, value: dict, manifest: dict) -> dict:
    result = copy.deepcopy(state)
    result["title"] = value["title"]
    result["step_order"] = list(value["step_order"])
    for group in ("add", "attach"):
        for atom in value[group]:
            result["atoms"].append({**copy.deepcopy(atom), "parent_id": atom.get("parent_id")})
            for source_id in atom["support_ids"]:
                source = manifest["sources"][source_id]
                if source_id in result["sources"] and result["sources"][source_id] != source:
                    raise BodyPatchError("An original source identity cannot change its content or method role")
                result["sources"][source_id] = copy.deepcopy(source)
    result["receipts"].append({"assignment_id": manifest["assignment_id"],
                               "assignment_sha256": manifest["assignment_sha256"],
                               "output_sha256": _sha(value), "input_ids": list(value["input_ids"])})
    return _seal(result)


def reduce_body_patch(state: dict, output: Any, manifest: dict) -> dict:
    """Return a new additive state. Exact repeated receipt is a no-op, even after later patches."""
    value = validate_body_patch(output, manifest)
    state = validate_body_state(state, manifest["candidate"])
    if value["schema_version"] == CAPACITY_SCHEMA:
        raise BodyPatchCapacityError(value["reason"])
    receipt = next((row for row in state["receipts"] if row["assignment_id"] == manifest["assignment_id"]), None)
    if receipt is not None:
        if (receipt["assignment_sha256"] != manifest["assignment_sha256"]
                or receipt["output_sha256"] != _sha(value)):
            raise BodyPatchError("An accepted assignment cannot be replaced by a different output")
        applied = _apply(manifest["base_state"], value, manifest)
        if (state["atoms"][:len(applied["atoms"])] != applied["atoms"]
                or state["receipts"][:len(applied["receipts"])] != applied["receipts"]
                or state["title"] != applied["title"]
                or any(state["sources"].get(k) != v for k, v in applied["sources"].items())):
            raise BodyPatchError("A previously accepted body prefix was changed")
        return copy.deepcopy(state)
    if state != manifest["base_state"]:
        raise BodyPatchError("Body patch cannot be applied to a stale or different base state")
    return _apply(state, value, manifest)


def render_body_patch(candidate: dict, state: dict) -> str:
    """Render every accepted atom in Chinese; no model selects which conditions survive."""
    candidate = _candidate(candidate)
    state = validate_body_state(state, candidate)
    by_id = {atom["id"]: atom for atom in state["atoms"]}
    steps = [by_id[identity] for identity in state["step_order"]]
    if not steps:
        raise BodyPatchError("A Skill requires at least one supported operative step")
    lines = [f"# {state['title']}", "", "## 任务", "", candidate["task"]]
    for key, label in (("trigger", "适用场景"), ("inputs", "输入"), ("outputs", "输出"), ("boundary", "适用边界")):
        value = candidate.get(key)
        if value:
            lines.extend(["", "## " + label, ""])
            lines.extend(["- " + item for item in value] if isinstance(value, list) else [value])
    source_order = list(state["sources"])

    def emit(atom: dict, prefix: str = "- ") -> None:
        refs = "、".join(f"来源{source_order.index(identity) + 1}" for identity in atom["support_ids"])
        text = atom["text"].replace("\n", "\n" + " " * len(prefix))
        lines.extend([f"{prefix}{text}（{atom['id']}；{refs}）", ""])

    lines.extend(["", "## 操作步骤", ""])
    labels = {"condition": "条件与例外：", "example": "参数化示例：", "conflict": "未决冲突："}
    for index, atom in enumerate(steps, 1):
        prefix = f"{index}. "
        emit(atom, prefix)
        for child in state["atoms"]:
            if child["parent_id"] == atom["id"]:
                emit(child, " " * len(prefix) + "- " + labels[child["kind"]])
    for kind, label in (("limit", "限制与条件"), ("example", "示例"), ("conflict", "未解决问题")):
        atoms = [atom for atom in state["atoms"] if atom["kind"] == kind and atom["parent_id"] is None]
        if atoms:
            lines.extend(["", "## " + label, ""])
            for atom in atoms:
                emit(atom)
    lines.extend(["", "## 来源", ""])
    for index, source in enumerate(state["sources"].values(), 1):
        lines.append(f"- 来源{index}：{source['source_uri']}，字符 [{source['start_char']}, {source['end_char']})；"
                     f"SHA256 `{source['sha256']}`")
    return render_skill_md(name=candidate["name"], description=candidate["task"], content="\n".join(lines).strip())
