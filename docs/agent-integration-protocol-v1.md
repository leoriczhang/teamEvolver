# teamEvolver Agent Integration Protocol V1

## Overview

Protocol V1 lets an Agent use teamEvolver as its context and evolution control
plane. OpenViking credentials stay on the teamEvolver server. Agents receive a
scoped access token and can:

- ingest versioned Sessions;
- resolve and read personal/team Memory and Skill context;
- write or forget only the mapped user's personal Memory;
- execute one baseline or candidate replay branch in the Agent's real runtime;
- receive published team Skill updates.

The protocol version is `1.0`. Unknown major versions fail with
`PROTOCOL_VERSION_UNSUPPORTED`. Payloads without a version use the one-cycle
legacy adapter.

## Registration

Register through:

```text
POST /internal/agents/register
Authorization: Bearer <control-plane-key>
```

The control-plane key is `EVOLVE_INGEST_API_KEY`. V1 registration fails closed
when it is not configured.

Minimal payload:

```json
{
  "schema_version": "teamevolver.agent-registration.v1",
  "protocol_version": "1.0",
  "agent_id": "example:tenant-a",
  "runtime_type": "example",
  "runtime_version": "3.2",
  "capabilities": {
    "session.ingest.v1": {},
    "context.workspace.v1": {
      "scopes": [
        "personal_memory",
        "team_memory",
        "personal_skills",
        "team_skills"
      ]
    },
    "replay.branch.v1": {
      "transport": "http",
      "endpoint": "https://agent.example/replay/v1",
      "max_interactions": 20,
      "supports_materials": true,
      "supports_artifacts": true,
      "supports_full_trace": true,
      "idempotent": false,
      "auth_profile": "example"
    }
  },
  "endpoints": {
    "health_url": "https://agent.example/health",
    "replay_url": "https://agent.example/replay/v1"
  }
}
```

V1 registration must not include OpenViking endpoints or keys. A successful
first registration, or an explicit token rotation, returns
`credentials.agent_access_token` once. The registry stores only its SHA-256.

The control plane may synchronize mappings for users that already exist in
teamEvolver:

```json
{
  "subject_mappings_authoritative": true,
  "subject_mappings": [
    {
      "external_subject": "user-123",
      "team_evolver_user_id": "alice"
    }
  ]
}
```

This extension is accepted only on the control-plane-authenticated registration
route. It never provisions users or credentials. Unknown target users are
reported in `subject_sync.missing_user_ids`; authoritative synchronization
removes stale mappings for the same integration.

## Identity

The access token identifies an integration, not a user. Every Context request
also supplies `external_subject`. Administrators map:

```text
integration_id + external_subject -> teamEvolver user
```

An unmapped subject returns `403 SUBJECT_NOT_MAPPED`. Runtime-only username
mapping is legacy-only.

## Context Workspace

All calls use:

```text
Authorization: Bearer <agent-access-token>
```

Endpoints:

- `GET /internal/agents/context/describe`
- `POST /internal/agents/context/resolve`
- `POST /internal/agents/context/read`
- `GET /internal/agents/context/skills`
- `POST /internal/agents/context/remember`
- `POST /internal/agents/context/forget`
- `POST /internal/agents/context/sessions/start`
- `POST /internal/agents/context/sessions/append`
- `POST /internal/agents/context/sessions/commit`

`resolve` returns opaque, short-lived `context_ref` values. It never returns a
personal OpenViking URI or key. `read` accepts only a ref issued to the same
integration and user. Team Memory and team Skill are read-only. `remember` and
`forget` are limited to personal Memory.

Session commit may include explicit refs that the Agent actually read:

```json
{
  "context_session_id": "ctxs_...",
  "used_context_refs": ["ctx_...", "ctx_..."]
}
```

teamEvolver resolves these refs server-side and submits OpenViking
`session.used` before `session.commit`. Refs must belong to the same Context
Session, Integration, and user. Usage submission is persisted per payload so a
failed commit can be retried without incrementing OpenViking usage twice.
The OpenViking deployment must durably preserve `/used` records across HTTP
requests until Commit consumes them; teamEvolver's local OpenViking service
stores this pending state in the Session's `.usage.jsonl`.

Default injection should use L0/L1. Full content or a Skill bundle requires an
explicit `read`. When a global item budget spans multiple requested scopes,
results must be interleaved by scope before the budget is applied so a large
personal space cannot starve team Memory or Skill context.

## Session Ingest

```text
POST /ingest_session
Authorization: Bearer <agent-access-token>
```

Required identity:

```json
{
  "schema_version": "teamevolver.agent-session.v1",
  "protocol_version": "1.0",
  "session_id": "session-1",
  "runtime": {
    "type": "example",
    "integration_id": "example:tenant-a",
    "version": "3.2",
    "protocol_version": "1.0"
  },
  "runtime_context": {
    "external_subject": "user-123"
  },
  "turns": [
    {
      "turn_num": 1,
      "prompt_text": "Perform the task",
      "response_text": "Done",
      "messages": [],
      "tool_calls": [],
      "tool_results": [],
      "injected_skills": [],
      "used_skills": [],
      "modified_skills": [],
      "metrics": {},
      "context_usage": {
        "context_snapshot_id": "ctxsnap_...",
        "memory_refs": [],
        "skill_refs": [],
        "feedback": {}
      }
    }
  ],
  "metrics": {},
  "source_materials": []
}
```

The `runtime.integration_id` must match the token. Context references are
verified against server-side receipts; caller-supplied scope or URI values are
discarded.

## Replay Branch

HTTP Agents expose the exact endpoint registered in `replay.branch.v1`.
teamEvolver sends one synchronous request for each branch. The baseline and
candidate calls run concurrently and share the same Context and execution
manifests.

The caller owns the deadline. The Agent must stop before
`limits.timeout_seconds`; it must not continue consuming model or tool resources
after the HTTP caller times out.

A successful result must contain non-negative integer metrics:

- `interaction_turns`
- `tool_call_count`
- `total_tokens`

Missing metrics, a mismatched `request_id`/`branch`, or an invalid schema fails
closed as `INVALID_RESPONSE`.

The runtime must isolate replay state and credentials:

- materialize only the source tenant/user/runtime configuration required by the
  branch, never a full production database;
- inject the frozen Context projection actually used by the runtime and return
  its hash as `context_input_hash`;
- keep upstream model credentials outside candidate-controlled processes,
  behind a short-lived parent broker;
- place the worker in a private network namespace and connect its local model
  sidecar to the parent broker through a protected Unix socket;
- keep the branch workspace as the only writable host path;
- fail with `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` when a recorded external side
  effect cannot be deterministically injected into the current runtime.

Agents that support recorded external-tool injection identify every result by
the normalized tool name, canonical argument signature, same-signature call
sequence, and result SHA-256. Matching by tool name alone is not Protocol V1
compliant. AgentsHub's Pi runtime currently advertises
`external_tool_replay=fail-closed`: workspace-local tools execute inside the
branch sandbox, while network-capable or external tools make the case
non-runnable rather than falling back to live side effects.

Checklist completion is a gate, not a weighted score. Efficiency comparison is
ordered by interaction turns, tool calls, then total tokens.

## Rollout

Recommended sequence:

1. register as V1 and map subjects;
2. run Context in `shadow` mode;
3. switch one integration to `enabled`;
4. enable V1 Session ingest;
5. enable context-aware Replay;
6. disable legacy storage and shared-key paths after one compatibility cycle.

Never downgrade replay safety, baseline CAS, or central Checklist judgment
during rollback.
