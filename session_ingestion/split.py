"""Split mixed-topic Sessions into topic-coherent sub-sessions (semantic only).

Sessions pulled from Langfuse (or pushed by agents) can mix several unrelated
tasks with chitchat in one conversation. Feeding such a session wholesale
into value classification produces one blurred verdict and one unusable
trajectory. Before ingest classification, every session is evaluated for a
semantic topic split:

1. One cheap LLM call per session returns topic boundaries over the turn
   list; each boundary segment becomes a sub-session with its own
   title/metadata. Boundaries are decided purely by semantics — no turn-count
   thresholds, time-gap heuristics, or size caps are involved.
2. When no model is configured, or the boundary reply fails validation,
   the session passes through unsplit.

Every split is recorded in the segment metadata (``split_from_session``) so
segments stay traceable to the original upstream session, and segments are
never re-split when they re-enter the pipeline.
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime
from typing import Any, Optional

from team_skills.evolution.stages.judge import _extract_json_object

logger = logging.getLogger(__name__)

_SPLIT_SYSTEM_PROMPT = """\
你是 agent 会话的话题切分器。输入是一个会话的轮次列表，每轮包含序号 i、时间戳 ts 和用户输入摘要 text。
同一个会话可能混杂了多个不相关的任务话题或闲聊。请按话题把轮次切分成若干连续段：
- 每段内的轮次必须属于同一个话题（同一次任务或同一段闲聊）。
- 段与段必须连续、不重叠，并按顺序覆盖全部轮次（1..N）。
- 只在话题切换明显（用户换了新任务、开始闲聊、或内容明显无关）时切分；不要过度切分。
- 整个会话自始至终只有一个话题时，返回覆盖全部轮次的单段。
只输出一个 JSON 对象，不要输出任何其它文本或 markdown 围栏：
{{"segments": [{{"start": 1, "end": 5, "topic": "简短话题描述"}}, ...]}}
"""


def _split_enabled(config) -> bool:
    if os.environ.get("TEAMEVOLVER_SESSION_SPLIT", "1").strip() == "0":
        return False
    return bool(getattr(config, "session_split_enabled", True))


def _turn_timestamp(turn: dict[str, Any]) -> Optional[datetime]:
    """Best-effort ISO timestamp of a turn (langfuse or push payloads)."""
    langfuse = turn.get("_langfuse") if isinstance(turn.get("_langfuse"), dict) else {}
    for source in (turn.get("timestamp"), langfuse.get("timestamp")):
        text = str(source or "").strip()
        if not text:
            continue
        try:
            return datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            continue
    return None


def _turn_int(turn: dict[str, Any], key: str) -> int:
    metrics = turn.get("metrics") if isinstance(turn.get("metrics"), dict) else {}
    try:
        return int(metrics.get(key) or 0)
    except (TypeError, ValueError):
        return 0


def _first_prompt_line(turn: dict[str, Any]) -> str:
    text = str(turn.get("prompt_text") or "").strip()
    for line in text.splitlines():
        line = line.strip()
        if line:
            return line[:80]
    return ""


def _coerce_boundaries(raw: str, n: int) -> list[tuple[int, int, str]]:
    """Validate the LLM boundary reply into contiguous (start, end, topic) tuples.

    Returns [] when the reply carries no usable boundaries, so the caller
    keeps the session unsplit. Overlaps are clipped and coverage holes are
    absorbed into the preceding segment — boundaries stay exactly as the
    model decided them semantically.
    """
    payload = _extract_json_object(raw)
    entries = payload.get("segments") if isinstance(payload, dict) else None
    if not isinstance(entries, list) or not entries:
        return []

    parsed: list[tuple[int, int, str]] = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        try:
            start = int(entry.get("start"))
            end = int(entry.get("end"))
        except (TypeError, ValueError):
            continue
        topic = str(entry.get("topic") or "").strip()[:80]
        if start < 1 or end < start:
            continue
        parsed.append((start, min(end, n), topic))
    if not parsed:
        return []

    parsed.sort(key=lambda item: item[0])
    ordered: list[list[Any]] = []
    for start, end, topic in parsed:
        if ordered and start <= ordered[-1][1]:
            start = ordered[-1][1] + 1
            if start > n:
                break
            if end < start:
                continue
        ordered.append([start, min(end, n), topic])

    # Fill coverage holes by absorbing the uncovered turns into the previous
    # segment (or opening one when the reply starts late).
    covered = 0
    filled: list[list[Any]] = []
    for start, end, topic in ordered:
        if start > covered + 1:
            if filled:
                filled[-1][1] = start - 1
            else:
                filled.append([1, start - 1, ""])
        filled.append([start, end, topic])
        covered = end
    if covered < n:
        if filled:
            filled[-1][1] = n
        else:
            filled.append([1, n, ""])

    return [(start, end, topic) for start, end, topic in filled]


async def _llm_boundaries(
    client: Any, turns: list[dict[str, Any]]
) -> list[tuple[int, int, str]]:
    """One cheap LLM call over the turn index; [] on any failure (fail-open)."""
    payload = {
        "total_turns": len(turns),
        "turns": [
            {
                "i": i,
                "ts": str(
                    (turn.get("_langfuse") or {}).get("timestamp")
                    if isinstance(turn.get("_langfuse"), dict)
                    else ""
                )
                or str(turn.get("timestamp") or ""),
                "text": str(turn.get("prompt_text") or "").strip()[:160],
            }
            for i, turn in enumerate(turns, 1)
        ],
    }
    messages = [
        {"role": "system", "content": _SPLIT_SYSTEM_PROMPT},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]
    try:
        raw = await client.chat(
            messages,
            max_tokens=4096,
            temperature=0.1,
            trace_name="session_ingestion.split_session",
            trace_tags=["ingest", "split"],
            trace_metadata={
                "component": "session_ingestion.split",
                "operation": "topic_boundaries",
            },
        )
    except Exception as exc:  # noqa: BLE001 - splitting is best-effort
        logger.warning("[SessionSplit] boundary LLM call failed: %s", exc)
        return []
    return _coerce_boundaries(str(raw or ""), len(turns))


def _split_client(config) -> Any:
    """Reuse the shared classifier LLM client for boundary detection."""
    try:
        from team_skills.evolution.session_filter import SessionValueClassifier

        return SessionValueClassifier.from_config(config).client
    except Exception as exc:  # noqa: BLE001 - fail-open to no split
        logger.warning("[SessionSplit] split LLM client unavailable: %s", exc)
        return None


def _build_segment(
    base: dict[str, Any],
    turns: list[dict[str, Any]],
    start: int,
    end: int,
    topic: str,
    index: int,
    total: int,
) -> dict[str, Any]:
    """Build one sub-session from a contiguous turn range of ``base``."""
    base_id = str(base.get("session_id") or "session")
    messages = [
        message
        for turn in turns
        for message in (turn.get("messages") or [])
        if isinstance(message, dict)
    ]

    tool_call_count = sum(
        _turn_int(turn, "tool_call_count") or len(turn.get("tool_calls") or [])
        for turn in turns
    )
    api_call_count = sum(_turn_int(turn, "api_call_count") for turn in turns)
    input_tokens = sum(_turn_int(turn, "input_tokens") for turn in turns)
    output_tokens = sum(_turn_int(turn, "output_tokens") for turn in turns)
    total_tokens = sum(_turn_int(turn, "total_tokens") for turn in turns) or (
        input_tokens + output_tokens
    )

    injected: list[str] = []
    used: list[str] = []
    for turn in turns:
        for key, bucket in (("injected_skills", injected), ("used_skills", used)):
            for skill in turn.get(key) or []:
                skill = str(skill).strip()
                if skill and skill not in bucket:
                    bucket.append(skill)

    title = topic or str(base.get("title") or "").strip()
    if not title:
        title = _first_prompt_line(turns[0]) if turns else ""
    if title and f"第{index}/{total}段" not in title:
        title = f"{title}（第{index}/{total}段）"

    metadata = dict(base.get("metadata") or {})
    metadata["split_from_session"] = base_id
    metadata["split_segment"] = {
        "index": index,
        "total": total,
        "turn_range": [start, end],
        "topic": topic,
        "strategy": "semantic",
    }

    segment: dict[str, Any] = {
        "session_id": f"{base_id}_s{start:03d}-{end:03d}"[:200],
        "turns": turns,
        "messages": messages,
        "system_prompt": base.get("system_prompt") or "",
        "injected_skills": injected,
        "used_skills": used,
        "source": base.get("source") or "",
        "title": title[:120],
        "metrics": {
            "interaction_turns": len(turns),
            "message_count": len(messages),
            "tool_call_count": tool_call_count,
            "api_call_count": api_call_count,
            "input_tokens": input_tokens,
            "output_tokens": output_tokens,
            "total_tokens": total_tokens,
        },
        "metadata": metadata,
    }

    # ``meta`` carries the adapter/push-extracted display identity (user_id /
    # session_id / trace_id); without it every split segment loses the console
    # identity columns the original session had.
    for key in ("user_alias", "user_id", "runtime", "runtime_context", "agent_id", "meta"):
        if base.get(key) is not None:
            segment[key] = base[key]

    timestamp = _turn_timestamp(turns[0]) if turns else None
    if timestamp is not None:
        segment["timestamp"] = timestamp.isoformat()
    elif base.get("timestamp"):
        segment["timestamp"] = base["timestamp"]

    langfuse = base.get("langfuse")
    if isinstance(langfuse, dict):
        trace_ids = [str(turn.get("trace_id")) for turn in turns if turn.get("trace_id")]
        segment["langfuse"] = {
            **langfuse,
            "trace_count": len(trace_ids),
            "trace_ids": trace_ids,
        }

    if base.get("force_reprocess"):
        segment["force_reprocess"] = True
        segment["reprocess_reason"] = str(
            base.get("reprocess_reason") or "split from original session"
        )
    if base.get("defer_evolution_trigger"):
        segment["defer_evolution_trigger"] = True
    return segment


async def split_session(config, session: dict[str, Any]) -> list[dict[str, Any]]:
    """Return the sub-sessions to ingest for ``session`` (usually ``[session]``).

    Every session is evaluated for a semantic topic split (no turn-count
    gating). Sessions already produced by an earlier split, controlled
    candidate audits, and sessions without turns pass through unchanged.
    Splitting is strictly best-effort: without a configured model, or when
    the boundary call fails / validates to fewer than two segments, the
    session is ingested unsplit.
    """
    if not isinstance(session, dict):
        return [session]
    if not _split_enabled(config):
        return [session]
    metadata = session.get("metadata") if isinstance(session.get("metadata"), dict) else {}
    if metadata.get("split_from_session"):
        return [session]
    runtime_context = (
        session.get("runtime_context")
        if isinstance(session.get("runtime_context"), dict)
        else {}
    )
    if str(runtime_context.get("candidate_job_id") or "").strip():
        return [session]  # controlled candidate audit: keep the session intact

    turns = [turn for turn in session.get("turns") or [] if isinstance(turn, dict)]
    if not turns:
        return [session]

    client = _split_client(config)
    if client is None:
        return [session]
    boundaries = await _llm_boundaries(client, turns)
    if len(boundaries) <= 1:
        return [session]

    total = len(boundaries)
    segments = [
        _build_segment(session, turns[start - 1 : end], start, end, topic, index, total)
        for index, (start, end, topic) in enumerate(boundaries, 1)
    ]
    logger.info(
        "[SessionSplit] session=%s turns=%d split into %d segments (strategy=semantic)",
        session.get("session_id"),
        len(turns),
        len(segments),
    )
    return segments
