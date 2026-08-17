# Custom Agent Integration Guide

This document provides step-by-step guide for integrating custom Agents into teamEvolver from scratch. Includes code examples for both minimum and full integration modes.

## Integration Steps Overview

### Minimum Integration (Session Feedback)

1. Obtain control plane key `EVOLVE_INGEST_API_KEY`
2. Register Agent via `/internal/agents/register`, declaring `session.ingest.v1` capability
3. Securely store returned `agent_access_token`
4. Configure subject mapping (admin UI or `subject_mappings` in registration payload)
5. Implement session data reporting (`POST /ingest_session`)

### Full Integration (Context + Replay + Skill Sync)

Continuing implementation on top of minimum integration:

6. Implement Context Workspace calls (resolve/read/skills)
7. Implement Context Session lifecycle management (start/append/commit)
8. Expose Replay HTTP endpoint for teamEvolver callback
9. Implement Skill Sync (pull or receive push webhook)
10. Optional: Implement personal Memory write (remember/forget)

## Step 1: Obtain Control Plane Key

Contact teamEvolver operations to obtain control plane key `EVOLVE_INGEST_API_KEY`. This key used for registering Agents, permissions equivalent to admin; must be stored securely, must not expose to clients.

Key configured on teamEvolver server side via environment variable:

```bash
export EVOLVE_INGEST_API_KEY="<your-secret-key>"
```

When this environment variable not configured, V1 registration endpoint returns 503 error.

Code entry point: `teamEvolver/proxy/routes.py:768` (`_check_v1_control_plane_key`)

## Step 2: Register Agent

Send registration request to teamEvolver service.

### Minimum Integration Registration Example

```bash
curl -X POST "http://<teamevolver-host>:52010/internal/agents/register" \
  -H "Authorization: Bearer <EVOLVE_INGEST_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "runtime_version": "1.0.0",
    "display_name": "My Custom Agent",
    "capabilities": {
      "session.ingest.v1": {}
    }
  }'
```

### Full Integration Registration Example

```bash
curl -X POST "http://<teamevolver-host>:52010/internal/agents/register" \
  -H "Authorization: Bearer <EVOLVE_INGEST_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "runtime_version": "1.0.0",
    "display_name": "My Custom Agent",
    "capabilities": {
      "session.ingest.v1": {},
      "context.workspace.v1": {
        "scopes": ["personal_memory", "team_memory", "team_skills"]
      },
      "replay.branch.v1": {
        "transport": "http",
        "endpoint": "https://my-agent.example.com/api/teamevolver/replay",
        "max_interactions": 10,
        "supports_materials": true,
        "supports_artifacts": false,
        "supports_full_trace": true,
        "idempotent": true,
        "auth_profile": "my_agent"
      },
      "skill.sync.v1": {
        "transport": "http",
        "endpoint": "https://my-agent.example.com/api/teamevolver/skill-sync"
      }
    },
    "endpoints": {
      "health_url": "https://my-agent.example.com/health",
      "replay_url": "https://my-agent.example.com/api/teamevolver/replay",
      "skill_sync_url": "https://my-agent.example.com/api/teamevolver/skill-sync"
    },
    "subject_mappings_authoritative": true,
    "subject_mappings": [
      {
        "external_subject": "user-001",
        "team_evolver_user_id": "alice"
      },
      {
        "external_subject": "user-002",
        "team_evolver_user_id": "bob"
      }
    ]
  }'
```

Code implementation: `teamEvolver/integrations/agent_registry.py:107` (`register_agent`)

## Step 3: Store Access Token

Successful registration response example:

```json
{
  "agent_id": "my-agent:prod",
  "runtime_type": "my-agent",
  "status": "active",
  "credentials": {
    "agent_access_token": "tev1_<random-token>"
  },
  "capabilities": ["session.ingest.v1"],
  "created_at": "2024-01-01T00:00:00Z"
}
```

`agent_access_token` returned only once on first registration or explicit rotation. Must be stored securely (e.g., key management service, environment variables, encrypted config files). teamEvolver server side only stores its SHA-256 hash; lost tokens cannot be recovered. Lost tokens require re-registration or rotation via admin API.

All subsequent Agent API calls authenticate with this token:

```
Authorization: Bearer tev1_<random-token>
```

## Step 4: Map Subjects

Subject is Agent-side user identifier, needs mapping to teamEvolver user. Two configuration methods:

### Method A: Batch Sync During Registration

Set `subject_mappings_authoritative: true` in registration payload and provide `subject_mappings` array. This replaces all existing mappings for that integration.

### Method B: Admin UI Configuration

In teamEvolver console Agent integration management page, manually add external_subject to teamEvolver user mappings.

Mapping format:

```
integration_id (agent_id) + external_subject -> team_evolver_user_id
```

Unmapped subjects calling Context API or Session ingestion receive `403 SUBJECT_NOT_MAPPED` error.

Code implementation: `teamEvolver/proxy/users_admin.py` (`resolve_agent_subject_user_id`, `sync_agent_subject_mappings`)

## Step 5: Implement Session Ingestion

Core functionality for minimum integration. After each Agent session ends, report complete trajectory to teamEvolver.

### Python Code Example (Minimum Integration)

```python
import requests
import json

TEAMEVOLVER_URL = "http://<teamevolver-host>:52010"
AGENT_TOKEN = "tev1_<token>"

def ingest_session(session_id: str, external_subject: str, turns: list, metrics: dict):
    payload = {
        "schema_version": "teamevolver.agent-session.v1",
        "protocol_version": "1.0",
        "session_id": session_id,
        "runtime": {
            "type": "my-agent",
            "integration_id": "my-agent:prod",
            "version": "1.0.0",
            "protocol_version": "1.0"
        },
        "runtime_context": {
            "external_subject": external_subject
        },
        "turns": turns,
        "metrics": metrics,
        "source_materials": []
    }

    resp = requests.post(
        f"{TEAMEVOLVER_URL}/ingest_session",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()

turns = [
    {
        "turn_num": 1,
        "prompt_text": "Help me write a sort function",
        "response_text": "Okay, here's a quicksort implementation...",
        "messages": [],
        "tool_calls": [],
        "tool_results": [],
        "injected_skills": [],
        "used_skills": [],
        "modified_skills": [],
        "metrics": {
            "input_tokens": 150,
            "output_tokens": 300
        }
    }
]

result = ingest_session(
    session_id="sess-abc123",
    external_subject="user-001",
    turns=turns,
    metrics={"interaction_turns": 1}
)
print(result)
```

Code entry point: `teamEvolver/proxy/routes.py:2971` (`ingest_session`)

## Step 6 (Optional): Implement Context Workspace

Full integration requires calling Context Workspace API to get relevant Memory and Skill context when users initiate requests.

### Python Code Example: Resolve Context

```python
def resolve_context(external_subject: str, query: str, context_session_id: str = ""):
    payload = {
        "external_subject": external_subject,
        "query": query,
        "scopes": ["personal_memory", "team_memory", "team_skills"],
        "max_items": 12,
        "max_chars": 16000
    }
    if context_session_id:
        payload["context_session_id"] = context_session_id

    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/resolve",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()

result = resolve_context("user-001", "How to handle database connection errors?")
for item in result["items"]:
    print(f"[{item['scope']}] {item['title']}: {item['l0'][:100]}...")
```

### Read Full Content

```python
def read_context(context_ref: str, level: str = "full"):
    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/read",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"context_ref": context_ref, "level": level},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()
```

Context Workspace API implementation: `teamEvolver/proxy/agent_context.py`

## Step 7 (Optional): Implement Context Session

Context Session associates context library usage with OpenViking Sessions, supporting precise usage attribution.

```python
def start_context_session(external_subject: str, external_session_id: str):
    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/sessions/start",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "external_subject": external_subject,
            "external_session_id": external_session_id
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()["context_session_id"]

def commit_context_session(context_session_id: str, used_refs: list[str]):
    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/sessions/commit",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "context_session_id": context_session_id,
            "used_context_refs": used_refs
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()
```

## Step 8 (Optional): Expose Replay Endpoint

When teamEvolver validates candidate Skills, sends HTTP requests to Agent's registered `replay_url`, requiring execution of baseline and candidate branches in isolated environments.

Replay endpoint needs to:

1. Receive POST request containing `request_id`, `branch` (baseline/candidate), `case.query` (task instruction), `frozen_context` (frozen context), `limits` (timeout and turn limits).
2. Execute task in isolated sandbox; must not access production data or produce external side effects.
3. Return results containing `interaction_turns`, `tool_call_count`, `total_tokens` metrics, plus `context_input_hash`.
4. For external tool calls that cannot be deterministically replayed, return `REPLAY_EXTERNAL_TOOL_UNSUPPORTED`.

### Replay Request Handling Framework Example

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/api/teamevolver/replay")
def handle_replay():
    body = request.json
    request_id = body["request_id"]
    branch = body["branch"]
    case = body["case"]
    limits = body["limits"]
    frozen_context = body.get("context_snapshot", {})

    if branch not in ("baseline", "candidate"):
        return jsonify({"error": "invalid branch"}), 400

    try:
        result = run_replay_branch(
            branch=branch,
            instruction=case["query"],
            frozen_context=frozen_context,
            timeout_seconds=limits["timeout_seconds"],
            max_interactions=limits["max_interactions"]
        )
        return jsonify({
            "schema_version": "teamevolver.replay-branch-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "branch": branch,
            "status": "succeeded",
            "metrics": {
                "interaction_turns": result["turns"],
                "tool_call_count": result["tool_calls"],
                "total_tokens": result["tokens"]
            },
            "output": {"final_response": result["response"]},
            "trace": {"messages": result["messages"], "interactions": []},
            "context_input_hash": result["context_hash"],
            "elapsed_seconds": result["elapsed"]
        })
    except ReplayExternalToolError:
        return jsonify({
            "schema_version": "teamevolver.replay-branch-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "branch": branch,
            "status": "unsupported",
            "error": {
                "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
                "message": "external tool call cannot be deterministically replayed",
                "retryable": False
            },
            "metrics": {},
            "elapsed_seconds": 0
        })
```

Replay adapter implementation: `teamEvolver/integrations/replay_adapters.py`

## Step 9 (Optional): Implement Skill Sync

Skill Sync has two modes:

### Pull Mode (Simple)

Periodically call `GET /internal/agents/context/skills` to get latest team skills manifest; compare with local cache then download full Skill Bundles on demand.

```python
def sync_skills(external_subject: str):
    resp = requests.get(
        f"{TEAMEVOLVER_URL}/internal/agents/context/skills",
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        params={"external_subject": external_subject, "scope": "team"},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()
```

### Push Mode (Real-time)

Expose webhook endpoint receiving teamEvolver Skill change notifications, return acknowledgment:

```python
@app.post("/api/teamevolver/skill-sync")
def handle_skill_sync():
    body = request.json
    event_id = body["event_id"]
    action = body["action"]
    skills = body["skills"]
    tenant_ids = body.get("tenant_ids", [])

    for skill in skills:
        if action == "publish":
            download_and_apply_skill(skill)
        elif action == "delete":
            remove_skill(skill["name"])

    return jsonify({
        "ok": True,
        "results": {
            tid: {
                "verification": {
                    "skills": [
                        {
                            "name": s["name"],
                            "matched": True,
                            "actual_version": s["version"],
                            "actual_sha256": s["sha256"]
                        }
                        for s in skills
                    ]
                }
            }
            for tid in tenant_ids
        }
    })
```

Skill Sync implementation: `teamEvolver/integrations/skill_sync_adapters.py`

## Minimum vs Full Integration Code Summary

| Feature | Minimum Integration LOC | Full Integration LOC |
|---------|------------------------|---------------------|
| Registration | ~20 lines | ~50 lines (including all capability declarations) |
| Token management | Store one string | Store one string |
| Session ingestion | ~40 lines/session | ~40 lines/session (plus context_usage) |
| Context Workspace | Not needed | ~100 lines (resolve + read + session lifecycle) |
| Replay endpoint | Not needed | ~200+ lines (sandbox isolation + deterministic replay) |
| Skill Sync | Not needed | ~50 lines (pull mode) or ~80 lines (push webhook) |
| Memory write | Not needed | ~20 lines (remember/forget) |

Recommend completing minimum integration and verifying data correctly fed back, then incrementally implementing Context Workspace and Replay functionality.

## Testing and Verification

After integration complete, verify each link using following commands:

```bash
# Health check
curl -fsS "http://<teamevolver-host>:52010/health"

# Verify Agent registration status
curl "http://<teamevolver-host>:52010/api/agent-integrations"

# Trigger one evolution cycle
curl -X POST "http://<teamevolver-host>:52010/trigger"

# View Sessions in queue
curl "http://<teamevolver-host>:52010/sessions?limit=5"
```

Related test cases reference: `tests/test_agent_registry.py`, `tests/test_agent_protocol.py`, `tests/test_agent_context_workspace.py`, `tests/test_replay_adapters.py`.
