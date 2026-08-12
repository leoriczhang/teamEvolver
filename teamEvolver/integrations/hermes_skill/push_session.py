#!/usr/bin/env python3
"""on_session_end hook: push the just-ended Hermes session into teamEvolver.

Portable & config-driven — nothing about a specific machine is baked in. All
of server URL, username, DB path and API key resolve from (in priority order):

    1. explicit env vars (TEAMEVOLVER_URL / TEAMEVOLVER_USER / HERMES_STATE_DB /
       EVOLVE_INGEST_API_KEY)
    2. a ``feed.json`` next to this script (written by install.py)
    3. built-in fallbacks — only for DB path (Hermes default state.db).

The teamEvolver service URL and username have NO built-in default: they must be
supplied explicitly. If either is missing the hook skips silently instead of
guessing a host.

Wire protocol (Hermes shell hooks):
- stdin  : JSON payload; we read ``session_id``.
- stdout : JSON status (ignored by Hermes for on_session_end).

Zero third-party dependencies on purpose — stdlib only (``sqlite3`` +
``urllib``) so it runs under whatever interpreter Hermes spawns it with.

Idempotent: teamEvolver keys the queue by ``session_id`` (put_object overwrites),
so firing on every turn simply refreshes the latest, fullest snapshot.
"""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

# --- constants -------------------------------------------------------------
# No default base_url on purpose: the teamEvolver service address must be
# supplied explicitly (env / feed.json). If it is missing we skip silently
# rather than guess a host.
MAX_CHARS = 64_000  # modern model context windows can absorb much richer turns
MAX_SYSTEM_CHARS = 200_000
_AVAILABLE_SKILLS_RE = re.compile(
    r"<available_skills>\s*(.*?)\s*</available_skills>",
    re.IGNORECASE | re.DOTALL,
)
_SKILL_LIST_ITEM_RE = re.compile(r"^\s*-\s+([^:\n]+?)(?:\s*:|$)")


def _log(msg: str) -> None:
    # Hooks capture stderr into ~/.hermes/errors.log; keep it terse.
    print(f"[teamEvolver-feed] {msg}", file=sys.stderr)


def _config_path() -> Path:
    override = os.environ.get("TEAMEVOLVER_FEED_CONFIG", "").strip()
    if override:
        return Path(override).expanduser()
    return Path(__file__).resolve().with_name("feed.json")


def _load_config() -> dict:
    path = _config_path()
    if path.exists():
        try:
            return json.loads(path.read_text("utf-8")) or {}
        except (ValueError, OSError):
            return {}
    return {}


def _hermes_home() -> Path:
    home = os.environ.get("HERMES_HOME", "").strip()
    if home:
        return Path(home).expanduser()
    # Mirror hermes_constants.get_hermes_home() default on Linux/macOS.
    return Path.home() / ".hermes"


def _state_db_path(cfg: dict) -> Path:
    override = os.environ.get("HERMES_STATE_DB", "").strip() or cfg.get("state_db")
    if override:
        return Path(override).expanduser()
    return _hermes_home() / "state.db"


def _json_value(value, default):
    if value in (None, ""):
        return default
    if isinstance(value, (dict, list)):
        return value
    try:
        parsed = json.loads(str(value))
        return parsed
    except (TypeError, ValueError):
        return default


def _normalize_tool_call(raw: dict) -> dict:
    if not isinstance(raw, dict):
        return {}
    function = raw.get("function") if isinstance(raw.get("function"), dict) else {}
    if not function and raw.get("name"):
        function = {
            "name": str(raw.get("name") or ""),
            "arguments": raw.get("arguments") or "{}",
        }
    call_id = str(raw.get("id") or raw.get("call_id") or "")
    return {
        "id": call_id,
        "type": str(raw.get("type") or "function"),
        "function": {
            "name": str(function.get("name") or ""),
            "arguments": function.get("arguments") or "{}",
        },
    }


def _tool_call_skill_name(tool_call: dict) -> tuple[str, str]:
    function = tool_call.get("function") if isinstance(tool_call, dict) else {}
    tool_name = str((function or {}).get("name") or "").strip().lower()
    arguments = _json_value((function or {}).get("arguments"), {})
    if not isinstance(arguments, dict):
        return tool_name, ""
    for key in ("name", "skill_name", "skill"):
        value = str(arguments.get(key) or "").strip()
        if value:
            return tool_name, value
    return tool_name, ""


def _extract_injected_skills(system_prompt: str) -> list[str]:
    """Return the exact skill names present in Hermes' injected catalog."""
    match = _AVAILABLE_SKILLS_RE.search(str(system_prompt or ""))
    if not match:
        return []
    names: list[str] = []
    seen: set[str] = set()
    for line in match.group(1).splitlines():
        item = _SKILL_LIST_ITEM_RE.match(line)
        if not item:
            continue
        name = item.group(1).strip()
        if name and name not in seen:
            seen.add(name)
            names.append(name)
    return names


def _message_record(row: sqlite3.Row) -> dict:
    tool_calls = [
        normalized
        for raw in (_json_value(row["tool_calls"], []) or [])
        if (normalized := _normalize_tool_call(raw))
    ]
    record = {
        "id": int(row["id"]),
        "role": str(row["role"] or ""),
        "content": str(row["content"] or "")[:MAX_CHARS],
        "timestamp": row["timestamp"],
        "tool_call_id": str(row["tool_call_id"] or ""),
        "tool_name": str(row["tool_name"] or ""),
        "tool_calls": tool_calls,
        "token_count": int(row["token_count"] or 0),
        "finish_reason": str(row["finish_reason"] or ""),
    }
    reasoning = str(row["reasoning"] or row["reasoning_content"] or "")
    if reasoning:
        record["reasoning"] = reasoning[:MAX_CHARS]
    return record


def _fold_turns(rows: list[sqlite3.Row], injected_skills: list[str]) -> list[dict]:
    """Fold the complete Hermes trajectory into user interaction turns.

    Unlike the old feed, assistant tool calls, tool results, reasoning and
    system exposure metadata are retained. A new user message starts a new
    interaction; every following assistant/tool message belongs to that turn.
    """
    turns: list[dict] = []
    current: list[dict] = []

    def _flush() -> None:
        if not current:
            return
        prompts = [m["content"] for m in current if m["role"] == "user" and m["content"]]
        responses = [m["content"] for m in current if m["role"] == "assistant" and m["content"]]
        tool_calls = [
            tool_call
            for message in current
            for tool_call in (message.get("tool_calls") or [])
        ]
        tool_results = [
            {
                "tool_call_id": message.get("tool_call_id") or "",
                "tool_name": message.get("tool_name") or "",
                "content": message.get("content") or "",
                "has_error": any(
                    token in str(message.get("content") or "").lower()
                    for token in ("error", "exception", "traceback", "failed")
                ),
            }
            for message in current
            if message.get("role") == "tool"
        ]
        used: list[str] = []
        modified: list[str] = []
        for tool_call in tool_calls:
            tool_name, skill_name = _tool_call_skill_name(tool_call)
            if not skill_name:
                continue
            if tool_name == "skill_view" and skill_name not in used:
                used.append(skill_name)
            elif tool_name == "skill_manage" and skill_name not in modified:
                modified.append(skill_name)
        turns.append(
            {
                "turn_num": len(turns) + 1,
                "prompt_text": "\n".join(prompts).strip()[:MAX_CHARS],
                "response_text": "\n".join(responses).strip()[:MAX_CHARS],
                "messages": list(current),
                "tool_calls": tool_calls,
                "tool_results": tool_results,
                "injected_skills": list(injected_skills),
                "used_skills": used,
                "read_skills": [{"skill_name": name} for name in used],
                "modified_skills": [{"skill_name": name} for name in modified],
                "metrics": {
                    "tool_call_count": len(tool_calls),
                    "message_tokens": sum(int(m.get("token_count") or 0) for m in current),
                },
            }
        )
        current.clear()

    for row in rows:
        message = _message_record(row)
        if message["role"] == "user" and current:
            _flush()
        current.append(message)
    _flush()
    return turns


def _read_session(db_path: Path, session_id: str) -> dict | None:
    if not db_path.exists():
        _log(f"state db not found: {db_path}")
        return None
    try:
        conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error as exc:
        _log(f"cannot open state db: {exc}")
        return None
    try:
        conn.row_factory = sqlite3.Row
        rows = conn.execute(
            "SELECT id, role, content, timestamp, tool_call_id, tool_calls, "
            "tool_name, token_count, finish_reason, reasoning, reasoning_content "
            "FROM messages "
            "WHERE session_id = ? AND active = 1 ORDER BY id ASC",
            (session_id,),
        ).fetchall()
        meta = conn.execute(
            "SELECT started_at, source, model, system_prompt, message_count, "
            "tool_call_count, input_tokens, output_tokens, cache_read_tokens, "
            "cache_write_tokens, reasoning_tokens, api_call_count "
            "FROM sessions WHERE id = ?",
            (session_id,),
        ).fetchone()
    except sqlite3.Error as exc:
        _log(f"query failed: {exc}")
        return None
    finally:
        conn.close()

    system_prompt = str(meta["system_prompt"] or "") if meta else ""
    injected_skills = _extract_injected_skills(system_prompt)
    turns = _fold_turns(rows, injected_skills)
    if not turns:
        return None

    title = turns[0]["prompt_text"].splitlines()[0][:120] if turns[0]["prompt_text"] else ""
    messages = [{"role": "system", "content": system_prompt[:MAX_SYSTEM_CHARS]}] if system_prompt else []
    messages.extend(_message_record(row) for row in rows)
    used_skills: list[str] = []
    for turn in turns:
        for name in turn.get("used_skills") or []:
            if name not in used_skills:
                used_skills.append(name)
    metrics = {
        "interaction_turns": len(turns),
        "message_count": int(meta["message_count"] or len(rows)) if meta else len(rows),
        "tool_call_count": int(meta["tool_call_count"] or 0) if meta else 0,
        "api_call_count": int(meta["api_call_count"] or 0) if meta else 0,
        "input_tokens": int(meta["input_tokens"] or 0) if meta else 0,
        "output_tokens": int(meta["output_tokens"] or 0) if meta else 0,
        "cache_read_tokens": int(meta["cache_read_tokens"] or 0) if meta else 0,
        "cache_write_tokens": int(meta["cache_write_tokens"] or 0) if meta else 0,
        "reasoning_tokens": int(meta["reasoning_tokens"] or 0) if meta else 0,
    }
    metrics["total_tokens"] = metrics["input_tokens"] + metrics["output_tokens"]
    session: dict = {
        "session_id": session_id,
        "turns": turns,
        "messages": messages,
        "system_prompt": system_prompt[:MAX_SYSTEM_CHARS],
        "injected_skills": injected_skills,
        "used_skills": used_skills,
        "metrics": metrics,
        "source": str(meta["source"] or "") if meta else "",
        "model": str(meta["model"] or "") if meta else "",
    }
    if title:
        session["title"] = title
    if meta and meta["started_at"]:
        session["timestamp"] = str(meta["started_at"])
    return session


def _post(base_url: str, session: dict, user: str, api_key: str) -> None:
    session = dict(session)
    session["user_alias"] = user
    url = base_url.rstrip("/") + "/ingest_session"
    data = json.dumps(session, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    if api_key:
        req.add_header("Authorization", f"Bearer {api_key}")
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            body = resp.read().decode("utf-8", "replace")
            _log(f"ingested {session['session_id']} ({len(session['turns'])} turns): {body[:200]}")
    except urllib.error.HTTPError as exc:
        _log(f"ingest failed HTTP {exc.code}: {exc.read().decode('utf-8', 'replace')[:200]}")
    except urllib.error.URLError as exc:
        _log(f"ingest unreachable at {url}: {exc.reason}")


def main() -> int:
    try:
        payload = json.loads(sys.stdin.read() or "{}")
    except ValueError:
        payload = {}
    session_id = str(payload.get("session_id") or "").strip()
    if not session_id:
        _log("no session_id on stdin; nothing to push")
        return 0

    cfg = _load_config()
    user = str(os.environ.get("TEAMEVOLVER_USER") or cfg.get("user_alias") or "").strip()
    if not user:
        _log("no user_alias configured (run install.py or set TEAMEVOLVER_USER)")
        return 0
    base_url = str(os.environ.get("TEAMEVOLVER_URL") or cfg.get("base_url") or "").strip()
    if not base_url:
        _log("no base_url configured (run install.py --url ... or set TEAMEVOLVER_URL)")
        return 0
    api_key = str(os.environ.get("EVOLVE_INGEST_API_KEY") or cfg.get("api_key") or "")

    session = _read_session(_state_db_path(cfg), session_id)
    if not session:
        _log(f"session {session_id} had no foldable turns; skipped")
        return 0

    _post(base_url, session, user, api_key)
    print(json.dumps({"action": "allow"}))  # for `hermes hooks test`
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
