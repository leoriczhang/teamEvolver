#!/usr/bin/env python3
"""Audit AgentsHub history against teamEvolver and OpenViking personal state."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any

DEFAULT_USERS = (
    "zhanghaitao",
    "yulin",
    "chenghan",
    "zhenglinsheng",
    "zhangpengkun",
    "admin",
)


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return value if isinstance(value, dict) else {}


def _business_file_count(root: Path) -> int:
    if not root.is_dir():
        return 0
    return sum(
        1
        for path in root.rglob("*")
        if path.is_file()
        and not any(
            part.startswith(".") for part in path.relative_to(root).parts
        )
    )


def _agentshub_rows(
    database: Path,
    usernames: tuple[str, ...],
) -> dict[str, dict[str, Any]]:
    if not database.is_file():
        return {}
    placeholders = ",".join("?" for _ in usernames)
    query = f"""
        SELECT
            u.username,
            u.id AS agentshub_user_id,
            u.openviking_user AS configured_openviking_user,
            COUNT(DISTINCT s.id) AS agentshub_sessions,
            COUNT(DISTINCT m.id) AS agentshub_messages,
            COUNT(DISTINCT mem.id) AS agentshub_memories
        FROM users u
        LEFT JOIN sessions s ON s.user_id = u.id
        LEFT JOIN messages m ON m.session_id = s.id
        LEFT JOIN memories mem ON mem.user_id = u.id
        WHERE u.username IN ({placeholders})
        GROUP BY u.id
    """
    connection = sqlite3.connect(database)
    connection.row_factory = sqlite3.Row
    try:
        return {
            str(row["username"]): dict(row)
            for row in connection.execute(query, usernames)
        }
    finally:
        connection.close()


def _context_counts(state_path: Path) -> dict[str, dict[str, int]]:
    state = _read_json(state_path)
    counts: dict[str, dict[str, int]] = {}
    sessions = state.get("sessions")
    if not isinstance(sessions, dict):
        return counts
    for session in sessions.values():
        if not isinstance(session, dict):
            continue
        username = str(session.get("user_id") or "")
        item = counts.setdefault(
            username,
            {"context_sessions": 0, "context_committed": 0, "context_events": 0},
        )
        item["context_sessions"] += 1
        item["context_committed"] += int(bool(session.get("committed")))
        events = session.get("events")
        item["context_events"] += len(events) if isinstance(events, dict) else 0
    return counts


def _remember_counts(audit_path: Path) -> dict[str, int]:
    counts: dict[str, int] = {}
    try:
        lines = audit_path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return counts
    for line in lines:
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if not isinstance(event, dict) or event.get("action") != "remember":
            continue
        username = str(event.get("user_id") or "")
        counts[username] = counts.get(username, 0) + 1
    return counts


def _audit(args: argparse.Namespace) -> list[dict[str, Any]]:
    usernames = tuple(dict.fromkeys(args.users))
    agentshub = _agentshub_rows(args.agentshub_db, usernames)
    context = _context_counts(args.context_state)
    remember = _remember_counts(args.context_audit)
    rows: list[dict[str, Any]] = []
    for username in usernames:
        source = agentshub.get(username, {})
        memory_root = (
            args.openviking_data
            / "viking"
            / args.account
            / "user"
            / username
            / "memories"
        )
        session_root = memory_root.parent / "sessions"
        history_sessions = int(source.get("agentshub_sessions") or 0)
        memory_files = _business_file_count(memory_root)
        row = {
            "username": username,
            "agentshub_user": bool(source),
            "agentshub_sessions": history_sessions,
            "agentshub_messages": int(source.get("agentshub_messages") or 0),
            "agentshub_memories": int(source.get("agentshub_memories") or 0),
            "configured_openviking_user": str(
                source.get("configured_openviking_user") or ""
            ),
            **context.get(
                username,
                {
                    "context_sessions": 0,
                    "context_committed": 0,
                    "context_events": 0,
                },
            ),
            "remember_events": remember.get(username, 0),
            "openviking_sessions": _business_file_count(session_root),
            "openviking_memory_files": memory_files,
            "recovery_needed": history_sessions > 0 and memory_files == 0,
        }
        rows.append(row)
    return rows


def _print_table(rows: list[dict[str, Any]]) -> None:
    headers = (
        "username",
        "ah_sessions",
        "ah_messages",
        "ctx_sessions",
        "ctx_committed",
        "remember",
        "ov_memories",
        "status",
    )
    values = [headers]
    for row in rows:
        if row["recovery_needed"]:
            status = "RECOVERY_NEEDED"
        elif not row["agentshub_user"]:
            status = "NO_AGENTSHUB_USER"
        elif not row["agentshub_sessions"]:
            status = "NO_HISTORY"
        else:
            status = "OK"
        values.append(
            (
                row["username"],
                str(row["agentshub_sessions"]),
                str(row["agentshub_messages"]),
                str(row["context_sessions"]),
                str(row["context_committed"]),
                str(row["remember_events"]),
                str(row["openviking_memory_files"]),
                status,
            )
        )
    widths = [max(len(item[index]) for item in values) for index in range(len(headers))]
    for item in values:
        print("  ".join(value.ljust(widths[index]) for index, value in enumerate(item)))


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("users", nargs="*", default=DEFAULT_USERS)
    parser.add_argument(
        "--agentshub-db",
        type=Path,
        default=Path.home() / "AgentsHub/backend/skill_agent_loop.db",
    )
    parser.add_argument(
        "--context-state",
        type=Path,
        default=Path.home() / ".teamEvolver/agent_context_state.json",
    )
    parser.add_argument(
        "--context-audit",
        type=Path,
        default=Path.home() / ".teamEvolver/agent_context_audit.jsonl",
    )
    parser.add_argument(
        "--openviking-data",
        type=Path,
        default=Path.home() / ".openviking/data",
    )
    parser.add_argument("--account", default="default")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    rows = _audit(args)
    if args.json:
        print(json.dumps(rows, ensure_ascii=False, indent=2))
    else:
        _print_table(rows)
    return int(any(row["recovery_needed"] for row in rows))


if __name__ == "__main__":
    raise SystemExit(main())
