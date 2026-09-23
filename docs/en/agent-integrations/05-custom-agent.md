# Custom Agent integration

Use [v2](./06-protocol-v2.md), declaring the user on every Session/Context/Skill request.
The server determines the tenant and Account.

The sections below keep the detailed V1-era step-by-step integration reference for the compatibility window.

This document provides step-by-step guide for integrating custom Agents into teamEvolver from scratch. Includes code examples for both minimum and full integration modes.

## Integration Steps Overview

### Minimum Integration (Session Feedback)

1. Obtain control plane key `EVOLVE_INGEST_API_KEY`
2. Register Agent via `/internal/agents/register`, declaring `session.ingest.v1` capability
3. Obtain and securely store the tenant machine credential `tevt_` (supplied by the operator)
4. Configure subject mapping (admin UI or `subject_mappings` in registration payload)
5. Implement session data reporting (`POST /ingest_session`)

### Full Integration (Context + Replay + Skill Sync)

Continuing implementation on top of minimum integration:

6. Implement Context Workspace calls (resolve/read/skills)
7. Implement Context Session lifecycle management (start/append/commit)
8. Expose Replay Turn endpoint for teamEvolver callback (or reuse `scripts/replay_turn_server.py`)
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
        "orchestration": "server_driven",
        "endpoint": "https://my-agent.example.com/api/teamevolver/replay",
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

## Step 3: Obtain and Securely Store the Tenant Machine Credential

Successful registration response example (registration **issues no token at all**; the response contains no credential fields):

```json
{
  "agent_id": "my-agent:prod",
  "runtime_type": "my-agent",
  "status": "active",
  "capabilities": ["session.ingest.v1"],
  "created_at": "2024-01-01T00:00:00Z"
}
```

The **tenant machine credential** (format `tevt_<random>`) required by the Agent data plane is supplied by the operator: on multi-tenant deployments obtain it from the console when the tenant is created (shown only once; rotation is `POST /api/tenants/{tenant_id}/rotate-token`, and the old credential dies immediately); on single-tenant deployments (`storage_pg` disabled) the operator configures it via the `TEAMEVOLVER_TENANT_TOKEN` environment variable (it must start with `tevt_`, and the server resolves it to the `default` tenant).

It must be stored securely (e.g., key management service, environment variables, encrypted config files). The teamEvolver server stores only its SHA-256 hash and cannot recover a lost credential. To rotate after loss or exposure: multi-tenant deployments rotate on the tenant page; single-tenant deployments change `TEAMEVOLVER_TENANT_TOKEN` and restart the process.

All subsequent Agent API calls authenticate with this credential:

```
Authorization: Bearer tevt_<random>
```

Every Context Workspace request must also declare `integration_id` (Query parameter on `describe`/`skills`, Body field on the other seven POST routes); the server validates that the Agent belongs to the current tenant and is `active`, and uses it as the ownership key for `context_ref`, Context Sessions and audit records (the declared identity owns the resource). This credential cannot call admin routes (`/api/*`) or the Agent registration route (`/internal/agents/register`).

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

Current protocol (v2 payload):

```python
import os
import requests

base = os.environ["TEAMEVOLVER_URL"]
headers = {"Authorization": "Bearer " + os.environ["TEAMEVOLVER_TENANT_TOKEN"]}
user_id = "alice"
response = requests.post(base + "/ingest_session", headers=headers, timeout=60, json={
    "schema_version": "teamevolver.agent-session.v2",
    "protocol_version": "2.0",
    "session_id": "task-001",
    "runtime": {"type": "custom"},
    "runtime_context": {"user_id": user_id},
    "turns": [{"turn_num": 1, "prompt_text": "Analyze costs", "response_text": "Report ready."}],
})
response.raise_for_status()
print(response.json())
```

V1 payload reference for the compatibility window:

```python
import os
import requests

TEAMEVOLVER_URL = "http://<teamevolver-host>:52010"
AGENT_TOKEN = "tevt_<token>"   # tenant machine credential (the only machine credential on the Agent data plane)

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


Search Context with POST `/internal/agents/context/resolve` and JSON `{"user_id":"alice","query":"..."}`.
Read, forget, append and commit must include user_id even when a ref/session ID is already present.
See [Context API](../api/04-context-workspace.md).

Use [authenticated pull](../api/06-skill-sync.md) for team Skills. For real evaluation, deploy a Python
[ReplayAdapterFactory](../api/05-replay-branch.md) wrapping the existing Agent API with isolated,
continuous branch sessions. Customer Agents do not implement judging or disclosure.

The sections below keep the detailed V1-era full-integration reference for the compatibility window.

Full integration requires calling Context Workspace API to get relevant Memory and Skill context when users initiate requests. Every request must declare `integration_id` (Query parameter on `describe`/`skills`, Body field on the other POST routes).

### Python Code Example: Resolve Context

```python
def resolve_context(external_subject: str, query: str, context_session_id: str = ""):
    payload = {
        "external_subject": external_subject,
        "integration_id": "my-agent:prod",
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
        json={"integration_id": "my-agent:prod", "context_ref": context_ref, "level": level},
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
            "integration_id": "my-agent:prod",
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
            "integration_id": "my-agent:prod",
            "context_session_id": context_session_id,
            "used_context_refs": used_refs
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()
```

## Step 8 (Optional): Implement Replay Turn Endpoint

When teamEvolver validates candidate Skills, it calls the Agent's registered `replay_url` to execute the baseline and candidate branches. The recommended mode is the **server-driven Turn protocol**: the Agent registers `orchestration: "server_driven"`, and teamEvolver calls the turn endpoint once per interaction turn, while the multi-turn loop, checklist judging, and metric aggregation stay on the server.

The turn endpoint needs to:

1. Receive a POST request (`teamevolver.replay-turn-request.v1`) containing `request_id` (session handle; consecutive turns with the same `request_id` must resume the same replay session instead of resetting it), `turn_num`, `branch` (baseline/candidate), `prompt` (turn instruction), `history` (prior turn records `[{turn_num, prompt, response}]`), and `limits.turn_timeout_seconds` (per-turn timeout); turn 1 additionally carries `context_snapshot`, `skill`, `materials`, and `tool_policy`.
2. Execute this turn's task in an isolated environment; must not access production data or produce external side effects.
3. Return this turn's result (`teamevolver.replay-turn-result.v1`): `final_response`, `messages` (full message trace of the turn; the only evidence the server-side Checklist Judge can see), plus fail-closed `metrics`—`tool_call_count` and `total_tokens` must be non-negative integers; missing or invalid counts invalidate the turn.
4. For external tool calls that cannot be deterministically replayed, return `status: "unsupported"` (fail-closed; do not fall back to live calls).

### Quick Path: Reuse the Ready-Made Turn Server

The repository ships `scripts/replay_turn_server.py`, so you do not need to implement the HTTP and protocol layers yourself:

```bash
python scripts/replay_turn_server.py --port 8010 --api-key <secret>
```

- Routes: `POST /turn/<runtime_type>` (called by teamEvolver once per turn) and `GET /health` (liveness)
- Each `runtime_type` maps to one handler function in the `AGENT_HANDLERS` dict; the script keeps per-session history keyed by `request_id` and passes it to the handler as `req["history"]`, so even stateless Agents can continue a task
- When the handler raises `ReplayUnsupportedError`, the script returns `status: "unsupported"`
- On the teamEvolver side export `TEAMEVOLVER_AGENT_<AUTH_PROFILE>_REPLAY_API_KEY=<secret>`, the same secret passed via `--api-key`

### Implementing the Turn Endpoint Yourself

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/api/teamevolver/replay/turn")
def handle_replay_turn():
    body = request.json
    request_id = body["request_id"]
    turn_num = body["turn_num"]
    branch = body["branch"]
    prompt = body["prompt"]
    history = body.get("history", [])      # prior turns: [{turn_num, prompt, response}]
    turn_timeout = body["limits"]["turn_timeout_seconds"]
    # Turn 1 additionally carries: context_snapshot, skill, materials, tool_policy

    if branch not in ("baseline", "candidate"):
        return jsonify({"error": "invalid branch"}), 400

    try:
        result = run_replay_turn(
            request_id=request_id,         # session handle for continuation
            turn_num=turn_num,
            prompt=prompt,
            history=history,
            turn_timeout_seconds=turn_timeout
        )
        return jsonify({
            "schema_version": "teamevolver.replay-turn-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "turn_num": turn_num,
            "branch": branch,
            "status": "succeeded",
            "final_response": result["response"],
            "messages": result["messages"],
            "metrics": {
                "tool_call_count": result["tool_calls"],
                "total_tokens": result["tokens"]
            }
        })
    except ReplayExternalToolError:
        return jsonify({
            "schema_version": "teamevolver.replay-turn-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "turn_num": turn_num,
            "branch": branch,
            "status": "unsupported",
            "error": {
                "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
                "message": "external tool call cannot be deterministically replayed",
                "retryable": False
            },
            "metrics": {}
        })
```

Replay adapter implementation: `team_replay/adapters.py:TurnBasedReplayAdapter`

## Step 9 (Optional): Implement Skill Sync

Skill Sync has two modes:

### Pull Mode (Simple)

Periodically call `GET /internal/agents/context/skills` to get latest team skills manifest; compare with local cache then download full Skill Bundles on demand.

```python
def sync_skills(external_subject: str):
    resp = requests.get(
        f"{TEAMEVOLVER_URL}/internal/agents/context/skills",
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        params={"external_subject": external_subject, "scope": "team", "integration_id": "my-agent:prod"},
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
| Credential management | Obtain one `tevt_` string from the operator and store it | Obtain one `tevt_` string from the operator and store it |
| Session ingestion | ~40 lines/session | ~40 lines/session (plus context_usage) |
| Context Workspace | Not needed | ~100 lines (resolve + read + session lifecycle) |
| Replay Turn handler | Not needed | One function (placed in `AGENT_HANDLERS` of `scripts/replay_turn_server.py`; HTTP and protocol layers are provided by the script) |
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
