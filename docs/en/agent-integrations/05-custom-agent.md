# Custom Agent Integration

This page uses Python examples to show how to connect any Agent runtime to TeamEvolver. Protocol rules are in [Protocol v2](./06-protocol-v2.md).

## Integration Steps

### Minimal Integration

1. Get the service address and tenant key from the administrator.
2. After each task, call `POST /ingest_session` to report the Session.

### Full Integration

3. Before model calls, retrieve context with Agent Context and record `context_usage` in the Session.
4. Before model calls, pull team Skills with `GET /sync/skills`.
5. For True Replay, the deployer writes a Replay Adapter or provides a per-turn HTTP endpoint.

## Step 1: Configure Credentials

```bash
export AGENT_TEAMEVOLVER_URL="https://teamevolver.example.com"
export TEAMEVOLVER_TENANT_TOKEN="tevt_..."
```

The tenant key represents the whole tenant; its holder can call data-plane endpoints on behalf of any `user_id` in the tenant. Keep it only on the Agent server, never in browsers or end-user devices.

## Step 2: Report Sessions

```python
import os
import httpx

BASE = os.environ["AGENT_TEAMEVOLVER_URL"]
HEADERS = {"Authorization": f"Bearer {os.environ['TEAMEVOLVER_TENANT_TOKEN']}"}


def report_session(session_id: str, user_id: str, turns: list[dict]) -> dict:
    envelope = {
        "schema_version": "teamevolver.agent-session.v2",
        "protocol_version": "2.0",
        "session_id": session_id,
        "runtime": {"type": "my-agent", "version": "1.0.0"},
        "runtime_context": {"user_id": user_id},
        "turns": turns,
    }
    response = httpx.post(f"{BASE}/ingest_session", json=envelope, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()  # 202: {"accepted": true, "job_id": ..., "status_url": ..., "dedupe": false}


report_session("task-001", "alice", [
    {"turn_num": 1, "prompt_text": "Analyze this month's cost", "response_text": "Cost report done.",
     "tool_calls": [], "metrics": {"input_tokens": 520, "output_tokens": 890}},
])
```

Reporting is asynchronous: 202 means the Session was persisted and accepted. Reporting the same `session_id` again returns the first acceptance. Retrying after network failures is safe.

## Step 3: Retrieve Context

```python
def resolve_context(user_id: str, query: str) -> dict:
    response = httpx.post(f"{BASE}/internal/agents/context/resolve", headers=HEADERS, timeout=30, json={
        "user_id": user_id,
        "query": query,
        "scopes": ["team_skills", "personal_skills", "team_memory", "personal_memory"],
        "max_items": 8,
        "max_chars": 12000,
    })
    response.raise_for_status()
    return response.json()


result = resolve_context("alice", "monthly cost report")
prompt_context = "\n\n".join(f"[{i['scope']}] {i['title']}\n{i['l1'] or i['l0']}" for i in result["items"])
```

Read full content by reference when needed:

```python
def read_context(user_id: str, context_ref: str, level: str = "l2") -> dict:
    response = httpx.post(f"{BASE}/internal/agents/context/read", headers=HEADERS, timeout=30,
                          json={"user_id": user_id, "context_ref": context_ref, "level": level})
    response.raise_for_status()
    return response.json()
```

Write the references actually injected or read into that turn's `context_usage`:

```python
turn["context_usage"] = {
    "context_snapshot_id": result["snapshot_id"],
    "skill_refs": [{"context_ref": i["context_ref"], "operation": "injected"}
                   for i in result["items"] if i["kind"] == "skill"],
    "memory_refs": [{"context_ref": i["context_ref"], "operation": "injected"}
                    for i in result["items"] if i["kind"] == "memory"],
}
```

References expire after 15 minutes, so report the Session promptly after the task.

## Step 4: Context Session (Optional)

To write conversation events into an OpenViking Session:

```python
def context_session(user_id: str, external_id: str, events: list[tuple[str, str]], used_refs: list[str]):
    start = httpx.post(f"{BASE}/internal/agents/context/sessions/start", headers=HEADERS, timeout=30,
                       json={"user_id": user_id, "external_session_id": external_id}).json()
    sid = start["context_session_id"]
    for sequence, (role, content) in enumerate(events, start=1):
        httpx.post(f"{BASE}/internal/agents/context/sessions/append", headers=HEADERS, timeout=30, json={
            "user_id": user_id, "context_session_id": sid, "event_id": f"{external_id}:{sequence}",
            "sequence": sequence, "role": role, "content": content,
        }).raise_for_status()
    httpx.post(f"{BASE}/internal/agents/context/sessions/commit", headers=HEADERS, timeout=30, json={
        "user_id": user_id, "context_session_id": sid, "used_context_refs": used_refs,
    }).raise_for_status()
```

`sequence` must increase consecutively from 1; replaying the same `event_id` with the same content is idempotent.

## Step 5: Pull Team Skills

```python
import base64
import pathlib

_etag = ""


def pull_skills(user_id: str, target: pathlib.Path) -> None:
    global _etag
    headers = {**HEADERS, **({"If-None-Match": _etag} if _etag else {})}
    response = httpx.get(f"{BASE}/sync/skills", params={"user_id": user_id}, headers=headers, timeout=30)
    if response.status_code == 304:
        return
    response.raise_for_status()
    _etag = response.headers["ETag"]
    for skill in response.json()["skills"]:
        root = target / skill["name"]
        for item in skill["files"]:
            path = root / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(item["content_b64"]))
```

Team Skills are delivered only by pull; the server does not push.

## Step 6: True Replay (Optional)

TeamEvolver does not call the Agent's private interfaces directly. The deployer writes a Replay Adapter (a Python file) that is written to PostgreSQL with the service Root Key and bound by a tenant administrator; its interface is `open(context) -> session`, `session.send(message)`, `session.close()`. See [Replay Adapter](../api/05-replay-branch.md#replay-adapter).

When the Agent can already run turn by turn over HTTP, the Adapter can delegate to the repository's `TurnBasedReplayAdapter`. The Agent side can follow the example server:

```bash
python scripts/replay_turn_server.py --port 8010 --api-key "$REPLAY_KEY"
```

The example server's endpoint is `POST /turn/<runtime_type>`; each call runs one turn, and `request_id` stays the same within a branch. On success it must return the turn's complete `messages` and `metrics.tool_call_count`, `metrics.total_tokens`; for external side effects that cannot be replayed deterministically it returns `unsupported` instead of falling back to a real call.

## Verification

```bash
curl "$AGENT_TEAMEVOLVER_URL/healthz"
curl -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  "$AGENT_TEAMEVOLVER_URL/internal/agents/context/describe?user_id=alice"
curl -i -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  "$AGENT_TEAMEVOLVER_URL/sync/skills?user_id=alice"
```

After reporting a Session, confirm the record appears on the console Session page, or have an administrator check the processing stages with `/api/sessions/{session_id}/stages`.
