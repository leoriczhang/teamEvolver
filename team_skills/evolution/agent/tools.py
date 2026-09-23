"""Tool registry, implementations, and the code-owned protocol appendix.

The appendix is appended to the (Prompt-Studio-overridable) task card by the
runner and is NOT overridable: the loop protocol, the tool contracts and
the final-decision schemas are mechanical contracts owned by this code.
Studio overrides should describe the mission and the editing rules, not
the wire format.
"""

from __future__ import annotations

import inspect
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from team_skills.evolution.kernel.enums import SLUG_RE
from team_skills.evolution.kernel.skill_edits import apply_skill_edits, format_edit_failures

# Per-part read caps (chars). Effect first, but bounded so one runaway
# observation cannot blow the context window.
_READ_CAPS = {
    "summary": 12000,
    "trajectory": 30000,
    "tail": 4000,
    "full": 36000,
}
_SEARCH_MAX_MATCHES = 20
_LIBRARY_SKILL_CAP = 20000
_GENERALIZATION_MAX_SUSPECTS = 40


@dataclass
class ToolContext:
    """Everything the tools need, including the staged-edit state."""

    stage: str
    skill_name: str = ""
    sessions: list[dict] = field(default_factory=list)
    current_skill: Optional[dict] = None
    existing_skill_names: list[str] = field(default_factory=list)
    library_reader: Optional[Callable[[str], Any]] = None
    bundle_contract: dict[str, Any] = field(default_factory=dict)

    original_content: str = ""
    staged_content: str = ""
    editable_files: dict[str, str] = field(default_factory=dict)
    applied_edits: int = 0
    propose_attempts: int = 0
    read_session_ids: list[str] = field(default_factory=list)

    def __post_init__(self) -> None:
        self.sessions = list(self.sessions or [])
        skill = self.current_skill or {}
        content = str(skill.get("content") or "")
        self.original_content = content
        self.staged_content = content
        editable = skill.get("_editable_bundle_files")
        self.editable_files = dict(editable) if isinstance(editable, dict) else {}
        self.sessions_by_id = {
            str(session.get("session_id") or "?"): session for session in self.sessions
        }


def _heading_list(content: str, limit: int = 40) -> list[str]:
    return [
        line.strip()
        for line in content.splitlines()
        if line.lstrip().startswith("#")
    ][:limit]


def _nearest_heading(content: str, position: int) -> str:
    for line in reversed(content[:position].splitlines()):
        if line.lstrip().startswith("#"):
            return line.strip()
    return "(frontmatter/intro)"


async def _tool_read_session(ctx: ToolContext, args: dict) -> dict:
    session_id = str(args.get("session_id") or "").strip()
    part = str(args.get("part") or "full").strip().lower()
    if part not in _READ_CAPS:
        return {"ok": False, "error": "part 必须是 summary|trajectory|tail|full 之一"}
    session = ctx.sessions_by_id.get(session_id)
    if session is None:
        return {
            "ok": False,
            "error": f"unknown session_id: {session_id}",
            "known": list(ctx.sessions_by_id)[:50],
        }
    if session_id not in ctx.read_session_ids:
        ctx.read_session_ids.append(session_id)
    summary = str(session.get("_summary") or "")
    trajectory = str(session.get("_trajectory") or "")
    if part == "summary":
        text = summary
    elif part == "trajectory":
        text = trajectory
    elif part == "tail":
        text = trajectory[-_READ_CAPS["tail"] :]
    else:
        text = f"{summary}\n\n---\n\n{trajectory}" if (summary or trajectory) else ""
    cap = _READ_CAPS[part]
    truncated = len(text) > cap
    if truncated:
        text = text[:cap] + "\n…(截断；可用 part=tail 或 part=summary 读取其余部分)"
    if not text.strip():
        return {
            "ok": True,
            "session_id": session_id,
            "part": part,
            "content": "",
            "note": "该会话没有可用的 summary/trajectory 数据",
        }
    return {
        "ok": True,
        "session_id": session_id,
        "part": part,
        "content": text,
        "truncated": truncated,
    }


async def _tool_search_sessions(ctx: ToolContext, args: dict) -> dict:
    query = str(args.get("query") or "").strip()
    if not query:
        return {"ok": False, "error": "query 必填"}
    try:
        pattern = re.compile(query, re.IGNORECASE)
    except re.error:
        pattern = re.compile(re.escape(query), re.IGNORECASE)
    matches: list[dict] = []
    for session in ctx.sessions:
        session_id = str(session.get("session_id") or "?")
        for field_name, label in (("_summary", "summary"), ("_trajectory", "trajectory")):
            text = str(session.get(field_name) or "")
            found = pattern.search(text)
            if not found:
                continue
            start = max(0, found.start() - 120)
            snippet = " ".join(text[start : found.end() + 120].split())
            matches.append(
                {"session_id": session_id, "field": label, "snippet": snippet[:300]}
            )
            break
        if len(matches) >= _SEARCH_MAX_MATCHES:
            break
    return {
        "ok": True,
        "query": query,
        "matches": matches,
        "matched_sessions": [m["session_id"] for m in matches],
    }


async def _tool_read_skill(ctx: ToolContext, args: dict) -> dict:
    if ctx.stage != "evolve_skill" or not ctx.current_skill:
        return {
            "ok": False,
            "error": "read_skill 仅在 evolve_skill 阶段可用（当前没有待改进的 skill）",
        }
    skill = ctx.current_skill
    out: dict[str, Any] = {
        "ok": True,
        "name": skill.get("name", ""),
        "description": skill.get("description", ""),
        "category": skill.get("category", "general"),
        "content": ctx.staged_content,
        "note": (
            "这是当前正文（含已暂存的编辑）。propose_edits 的 old_string/anchor "
            "必须从这份 content 逐字节复制——包括反斜杠转义、缩进、空行与标点风格。"
        ),
    }
    if ctx.editable_files:
        out["editable_files"] = {
            path: len(text) for path, text in sorted(ctx.editable_files.items())
        }
    return out


async def _tool_read_bundle_file(ctx: ToolContext, args: dict) -> dict:
    path = str(args.get("path") or "").strip()
    if path not in ctx.editable_files:
        return {
            "ok": False,
            "error": f"该路径不可编辑或不存在：{path}",
            "available": sorted(ctx.editable_files),
        }
    return {"ok": True, "path": path, "content": ctx.editable_files[path]}


async def _tool_read_library_skill(ctx: ToolContext, args: dict) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name 必填"}
    if ctx.library_reader is None:
        return {"ok": False, "error": "library reader 不可用（本阶段无法读取技能库）"}
    result = ctx.library_reader(name)
    if inspect.isawaitable(result):
        result = await result
    content = str(result or "")
    if not content:
        return {"ok": False, "error": f"技能库中没有该技能：{name}"}
    truncated = len(content) > _LIBRARY_SKILL_CAP
    return {
        "ok": True,
        "name": name,
        "content": content[:_LIBRARY_SKILL_CAP],
        "truncated": truncated,
    }


async def _tool_propose_edits(ctx: ToolContext, args: dict) -> dict:
    if not ctx.staged_content:
        return {"ok": False, "error": "没有可编辑的当前正文（read_skill 先确认）"}
    edits = args.get("edits")
    ctx.propose_attempts += 1
    application = apply_skill_edits(ctx.staged_content, edits)
    if not application.ok:
        return {
            "ok": False,
            "error": "编辑应用失败（本轮整批未生效，正文未变）",
            "failures": format_edit_failures(application.failures),
            "hint": (
                "old_string/anchor 必须从当前正文逐字节复制——包括反斜杠转义"
                '（如 JSON 示例中的 \\"）、缩进、空行与标点风格；匹配必须恰好一次。'
                "修正失败项后重新调用 propose_edits（可保留未失败的编辑）。"
            ),
        }
    before_chars = len(ctx.staged_content)
    ctx.staged_content = application.content
    ctx.applied_edits += application.applied
    return {
        "ok": True,
        "applied": application.applied,
        "total_applied_edits": ctx.applied_edits,
        "chars_before": before_chars,
        "chars_after": len(application.content),
        "headings": _heading_list(application.content),
        "note": "编辑已暂存生效；final 提交 improve_skill 时省略 content 即使用该结果",
    }


_GENERALIZATION_PATTERNS: list[tuple[str, str]] = [
    ("absolute_path", r"(?:/Users/|/home/|/root/|/tmp/|/var/folders/|[A-Za-z]:\\\\)[^\s\"'`\)\]]+"),
    ("date", r"\b20\d{2}[-/.]\d{1,2}(?:[-/.]\d{1,2})?\b"),
    ("long_id", r"\b\d{7,}\b"),
    ("uuid", r"\b[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\b"),
]


async def _tool_check_generalization(ctx: ToolContext, args: dict) -> dict:
    content = ctx.staged_content or ctx.original_content
    if not content:
        return {"ok": False, "error": "没有可检查的正文"}
    suspects: list[dict] = []
    for kind, pattern in _GENERALIZATION_PATTERNS:
        for match in re.finditer(pattern, content):
            suspects.append(
                {
                    "kind": kind,
                    "snippet": " ".join(content[match.start() : match.start() + 80].split())[:80],
                    "section": _nearest_heading(content, match.start()),
                }
            )
            if len(suspects) >= _GENERALIZATION_MAX_SUSPECTS:
                break
        if len(suspects) >= _GENERALIZATION_MAX_SUSPECTS:
            break
    return {
        "ok": True,
        "checked_chars": len(content),
        "suspects": suspects,
        "note": (
            "这些只是嫌疑项，由你判定：环境稳定事实（端口/端点/固定 schema/领域规则）"
            "应保留具体写法；任务实例取值（本次路径/日期/数据集 ID/主体）应参数化为占位符"
            "并说明推导方式"
        ),
    }


async def _tool_check_name(ctx: ToolContext, args: dict) -> dict:
    name = str(args.get("name") or "").strip()
    if not name:
        return {"ok": False, "error": "name 必填"}
    if not SLUG_RE.match(name):
        return {"ok": False, "error": f"name 不是合法 slug（小写字母数字与连字符）：{name}"}
    existing = set(ctx.existing_skill_names)
    if name in existing:
        return {
            "ok": False,
            "error": f"技能库中已存在同名技能：{name}",
            "hint": "换一个面向能力、简短、动作导向的名称后重试",
        }
    if ctx.skill_name and name == ctx.skill_name:
        return {"ok": False, "error": f"name 与当前技能同名：{name}"}
    return {"ok": True, "name": name}


_STAGE_TOOLS: dict[str, list[str]] = {
    "evolve_skill": [
        "read_session",
        "search_sessions",
        "read_skill",
        "read_bundle_file",
        "propose_edits",
        "check_generalization",
    ],
    "create_skill": [
        "read_session",
        "search_sessions",
        "read_library_skill",
        "check_name",
    ],
    "merge": [],
}


class ToolRegistry:
    """Dispatches tool calls and renders per-call observations."""

    def __init__(self, ctx: ToolContext, *, max_calls_per_round: int = 8) -> None:
        self.ctx = ctx
        self.max_calls_per_round = max(1, int(max_calls_per_round))
        self._tools: dict[str, Any] = {
            "read_session": _tool_read_session,
            "search_sessions": _tool_search_sessions,
            "read_skill": _tool_read_skill,
            "read_bundle_file": _tool_read_bundle_file,
            "read_library_skill": _tool_read_library_skill,
            "propose_edits": _tool_propose_edits,
            "check_generalization": _tool_check_generalization,
            "check_name": _tool_check_name,
        }

    def names_for_stage(self) -> list[str]:
        return list(_STAGE_TOOLS.get(self.ctx.stage, []))

    async def execute(self, calls: Any) -> list[dict]:
        if not isinstance(calls, list):
            calls = []
        observations: list[dict] = []
        if len(calls) > self.max_calls_per_round:
            observations.append(
                {
                    "tool": "(limiter)",
                    "ok": False,
                    "error": f"tool_calls 超过每轮上限 {self.max_calls_per_round}，多余调用被丢弃",
                }
            )
            calls = calls[: self.max_calls_per_round]
        allowed = set(self.names_for_stage())
        for call in calls:
            if not isinstance(call, dict):
                observations.append({"ok": False, "error": "每个 tool call 必须是对象"})
                continue
            name = str(call.get("tool") or "").strip()
            args = call.get("args") if isinstance(call.get("args"), dict) else {}
            handler = self._tools.get(name)
            if handler is None or name not in allowed:
                observations.append(
                    {
                        "tool": name,
                        "ok": False,
                        "error": f"unknown or unavailable tool: {name}",
                        "available": sorted(allowed),
                    }
                )
                continue
            try:
                observation = await handler(self.ctx, args)
                if not isinstance(observation, dict):
                    observation = {"ok": False, "error": "tool returned non-dict"}
            except Exception as exc:  # noqa: BLE001 - one bad tool must not kill the loop
                observation = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}
            observation.setdefault("tool", name)
            observations.append(observation)
        return observations


# ------------------------------------------------------------------ #
# Code-owned protocol appendix                                        #
# ------------------------------------------------------------------ #

_PLAN_SCHEMA = """\
{"type": "plan", "action_candidate": "improve_skill|optimize_description|create_skill|skip", \
"evidence_classification": {"team_skill": [{"claim": "...", "supporting_session_ids": ["..."], \
"causal_link": "..."}], "user_memory": [], "task_requirement": [], "agent_runtime": [], \
"insufficient_evidence": []}, "plan": ["步骤 1", "步骤 2"], "sessions_to_read": ["会话 id"], \
"rationale": "..."}"""

_EVIDENCE_SCHEMA = """\
"evidence_classification": {"team_skill": [{"claim": "...", "supporting_session_ids": ["..."], \
"causal_link": "..."}], "user_memory": [], "task_requirement": [], "agent_runtime": [], \
"insufficient_evidence": []}"""

_TOOL_DOCS: dict[str, str] = {
    "read_session": (
        '- {"tool": "read_session", "args": {"session_id": "s1", "part": "full"}}\n'
        "  按需读取单个会话。part：summary（LLM 分析）/ trajectory（程序化轨迹）/"
        " tail（轨迹末尾 4000 字符）/ full（默认，summary+trajectory）。各部分有截断上限。"
    ),
    "search_sessions": (
        '- {"tool": "search_sessions", "args": {"query": "关键词或正则"}}\n'
        "  跨所有会话的 summary+trajectory 检索，返回命中会话 id 与片段（不返回全文）。"
    ),
    "read_skill": (
        '- {"tool": "read_skill", "args": {}}\n'
        "  读取当前（含已暂存编辑的）skill 正文——byte-exact。propose_edits 的"
        " old_string/anchor 必须从这份正文逐字节复制。同时列出可编辑 bundle 文件。"
    ),
    "read_bundle_file": (
        '- {"tool": "read_bundle_file", "args": {"path": "scripts/run.py"}}\n'
        "  读取一个可编辑 bundle 文件的全文（file_changes 的 upsert 需要完整替换内容）。"
    ),
    "read_library_skill": (
        '- {"tool": "read_library_skill", "args": {"name": "existing-skill"}}\n'
        "  读取技能库中一个既有 skill 的内容，用于差异化定位新 skill。"
    ),
    "propose_edits": (
        '- {"tool": "propose_edits", "args": {"edits": [...]}}\n'
        "  把一组有序编辑机械应用到当前 staged 正文。每条编辑：\n"
        '  {"operation": "replace", "old_string": "<从 read_skill 的 content 逐字节复制的唯一片段>", '
        '"new_string": "<替换后的内容；空字符串表示删除>"}\n'
        '  {"operation": "insert_after", "anchor": "<当前正文中恰好出现一次的锚点>", '
        '"new_string": "<紧随锚点之后插入的内容>"}\n'
        "  整批全部成功才生效并暂存；任何一条失败则整批不生效，观测会给出失败明细，"
        "修正后重新提交整批。锚点凭记忆重写是最常见的失败原因——先 read_skill。"
    ),
    "check_generalization": (
        '- {"tool": "check_generalization", "args": {}}\n'
        "  对当前（staged）正文做确定性嫌疑扫描（绝对路径/日期/长数字 ID/UUID），"
        "返回嫌疑清单与所在章节，由你判定保留还是参数化。"
    ),
    "check_name": (
        '- {"tool": "check_name", "args": {"name": "new-skill-slug"}}\n'
        "  校验新 skill 名称：slug 合法性 + 与技能库既有名称不冲突。"
    ),
}


def build_protocol_appendix(stage: str, *, max_tool_calls_per_round: int = 8) -> str:
    """The loop protocol + tool contracts + final schema (code-owned)."""
    tool_names = _STAGE_TOOLS.get(stage, [])
    sections: list[str] = [
        "## 工作协议（代码所有，覆盖任何与之冲突的说明）",
        "",
        "你在一个受限的多轮循环中工作。每一轮输出一个 JSON 对象（不要 markdown 围栏，"
        "不要多余文本），只能是以下三种之一：",
        "",
        "1. **plan**（第 1 轮必须先输出）：",
        "```json",
        _PLAN_SCHEMA,
        "```",
        "2. **tool_calls**（第 2 轮起，可批量）：",
        '```json\n{"type": "tool_calls", "calls": [{"tool": "...", "args": {...}}]}\n```',
        f"每轮最多 {max_tool_calls_per_round} 次调用；执行后你会收到观测（Observations）。",
        "3. **final**（终稿，随时可提交；轮数耗尽前必须提交）：",
        '```json\n{"type": "final", "decision": {<决策对象，见下>}}\n```',
    ]

    if tool_names:
        sections.extend(["", "### 可用工具", ""])
        for name in tool_names:
            sections.append(_TOOL_DOCS[name])
            sections.append("")

    if stage == "merge":
        sections.extend(
            [
                "### final 决策对象（merge）",
                "",
                "```json",
                '{"type": "final", "skill": {"name": "<名称不变>", '
                '"description": "<合并后的触发描述>", "content": "<仅合并后的 Markdown 正文，'
                '不是带 frontmatter 的完整 SKILL.md>"}}',
                "```",
                "可选字段：skill 内的 metadata / extra_frontmatter。",
                "也接受旧式顶层对象 {\"name\", \"description\", \"content\"}。",
                "",
                "提交前对照任务卡中的合并原则逐项自检，然后一次性输出 final。",
            ]
        )
        return "\n".join(sections)

    if stage == "evolve_skill":
        final_schema = """\
{"type": "final", "decision": {
  "action": "improve_skill",
  "skill": {
    "name": "<保持原名>",
    "description": "<仅当需要修改时给出；省略则沿用当前值>",
    "content": "<完整更新后的正文——仅当不走 propose_edits 暂存路线时才提供；已暂存时必须省略>",
    "category": "<仅当需要修改时给出；省略则沿用当前值>",
    "file_changes": [
      {"path": "scripts/run.py", "operation": "upsert", "content": "<完整 UTF-8 文件>", "reason": "..."},
      {"path": "scripts/old.sh", "operation": "delete", "reason": "..."}
    ]
  },
  "rationale": "<综合证据说明理由>",
  EVIDENCE
}}

或 optimize_description：
{"type": "final", "decision": {"action": "optimize_description", "skill": {"name": "<保持原名>", \
"description": "<重写后的 description，包含使用场景与 NOT-for 条件>"}, "rationale": "...", EVIDENCE}}

或 create_skill：
{"type": "final", "decision": {"action": "create_skill", "skill": {"name": "<新的小写连字符 slug>", \
"description": "<2-4 句，包含触发场景与 NOT-for 条件>", "content": "<Markdown 格式的 Skill 正文>", \
"file_changes": [{"path": "scripts/run.py", "operation": "upsert", "content": "<完整 UTF-8 文件>", "reason": "..."}]}, \
"rationale": "<为什么需要新建，以及为什么当前 Skill 不应吸纳这些内容>", EVIDENCE}}

或 skip：
{"type": "final", "decision": {"action": "skip", "rationale": "<为什么跳过>", EVIDENCE}}""".replace(
            "EVIDENCE", _EVIDENCE_SCHEMA
        )
    else:  # create_skill
        final_schema = """\
{"type": "final", "decision": {
  "action": "create_skill",
  "skill": {
    "name": "<小写连字符 slug，用 check_name 校验>",
    "description": "<2-4 句，包含触发场景与 NOT-for 条件>",
    "content": "<Markdown 格式的 Skill 正文>",
    "file_changes": [
      {"path": "scripts/run.py", "operation": "upsert", "content": "<完整 UTF-8 文件>", "reason": "..."}
    ]
  },
  "rationale": "<为什么要创建这个 Skill>",
  EVIDENCE
}}

或 skip：
{"type": "final", "decision": {"action": "skip", "rationale": "<为什么跳过>", EVIDENCE}}""".replace(
            "EVIDENCE", _EVIDENCE_SCHEMA
        )

    sections.extend(
        [
            "### final 决策对象",
            "",
            "```json",
            final_schema,
            "```",
            "",
            "### 循环规则",
            "",
            "- 除 final 外不要输出决策对象；所有 JSON 都不要 markdown 围栏。",
            "- plan 的 action_candidate=skip 会立即终局（不再调用工具）。",
            "- plan 声称非 skip 但 team_skill 证据为空时会被要求修正一次；仍为空则终局 skip。",
            "- 工具观测中的失败会告诉你原因，修正后重试；编辑锚点失败时先 read_skill 再重提。",
            "- improve_skill 走 propose_edits 暂存路线时，final 的 skill 中省略 content。",
            "- 轮数耗尽前一轮会收到必须提交的提醒；到顶未提交时系统用当前 staged 状态"
            "做 best-effort 收尾（可能降级为 skip）。",
        ]
    )
    return "\n".join(sections)
