"""Import AgentsHub conversations into team-evolver evolution analysis.

Read-only against the AgentsHub SQLite DB; each AgentsHub session becomes one
``teamevolver.agent-session.v2`` envelope POSTed to ``/ingest_session``. The
worker then runs the mandatory normalize -> analyze -> trace_analyze -> archive
chain. Idempotent: re-running maps the same ``session_id`` back to its first
acceptance (push dedupe), so it is safe to resume.
Set AGENTSHUB_DB to the SQLite file, TEAMEVOLVER_TENANT_TOKEN to the tenant Key,
and optionally TEAMEVOLVER_INGEST_URL to the target ingest endpoint.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import time
import urllib.error
import urllib.request

AGENTS_DB = os.environ.get("AGENTSHUB_DB", "")
INGEST_URL = os.environ.get("TEAMEVOLVER_INGEST_URL", "http://127.0.0.1:52010/ingest_session")


def load_sessions() -> list[dict]:
    if not AGENTS_DB:
        raise ValueError("AGENTSHUB_DB must name the source SQLite database")
    c = sqlite3.connect(f"file:{AGENTS_DB}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    sessions = []
    for s in c.execute("select id, user_id, title, created_at from sessions order by created_at"):
        msgs = c.execute(
            "select role, content, created_at from messages "
            "where session_id=? order by created_at, id",
            (s["id"],),
        ).fetchall()
        # Pair consecutive user->assistant messages into turns. A user message
        # opens a turn; the next assistant message closes it. Unpaired user
        # messages still become a turn (prompt only); leading assistant messages
        # are attached as a response to the current open turn.
        turns: list[dict] = []
        pending_prompt: str | None = None
        for m in msgs:
            role, content = m["role"], (m["content"] or "")
            if role == "user":
                if pending_prompt is not None:
                    turns.append({"prompt_text": pending_prompt, "response_text": ""})
                pending_prompt = content
            elif role == "assistant":
                if pending_prompt is not None:
                    turns.append({"prompt_text": pending_prompt, "response_text": content})
                    pending_prompt = None
                else:
                    turns.append({"prompt_text": "", "response_text": content})
        if pending_prompt is not None:
            turns.append({"prompt_text": pending_prompt, "response_text": ""})
        if not turns:
            continue
        sessions.append(
            {
                "session_id": s["id"],
                "user_id": s["user_id"] or "admin",
                "title": s["title"] or "",
                "business_time": _iso(s["created_at"]),
                "turns": turns,
            }
        )
    c.close()
    return sessions


def _iso(ts: str) -> str:
    # AgentsHub stores naive UTC timestamps like "2026-08-15 07:22:46.575469".
    return (str(ts or "").strip().replace(" ", "T") or "1970-01-01T00:00:00") + "Z"


def post(session: dict) -> tuple[int, str]:
    token = os.environ["TEAMEVOLVER_TENANT_TOKEN"]
    body = {
        "schema_version": "teamevolver.agent-session.v2",
        "protocol_version": "2.0",
        "session_id": session["session_id"],
        "runtime": {"type": "agentshub"},
        "runtime_context": {"user_id": session["user_id"]},
        "business_time": session["business_time"],
        "meta": {
            "title": session["title"], "origin": "agentshub-import",
            "trace_id": session["session_id"],
        },
        "turns": session["turns"],
    }
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(
        INGEST_URL,
        data=data,
        method="POST",
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")


def main() -> int:
    sessions = load_sessions()
    print(f"loaded {len(sessions)} sessions with turns")
    accepted = dedupe = failed = 0
    for i, s in enumerate(sessions, 1):
        code, text = post(s)
        if code == 202:
            payload = json.loads(text)
            if payload.get("dedupe"):
                dedupe += 1
            else:
                accepted += 1
        else:
            failed += 1
            print(f"  [{i}] FAIL {s['session_id']} http={code} {text[:200]}")
        if i % 20 == 0:
            print(f"  progress {i}/{len(sessions)} accepted={accepted} dedupe={dedupe} failed={failed}")
        time.sleep(0.05)
    print(f"DONE accepted={accepted} dedupe={dedupe} failed={failed} total={len(sessions)}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
