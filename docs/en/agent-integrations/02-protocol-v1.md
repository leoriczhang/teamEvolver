> **Legacy migration reference**: the following describes old-client compatibility only. New integrations use [v2](./06-protocol-v2.md). Strict mode rejects v1; the final release removes registration and mappings.

# Protocol V1 Specification

## Overview

TE projects successful experiences to OV in the background. Its [status, manual-trigger and historical-import endpoints](../api/16-experience-sync.md) are tenant-admin only and do not change this protocol. Status includes source-gap summaries, an unconfirmed-index count and retry/worker diagnostics. Resource synchronization is not Ontology publication.

Protocol V1 allows Agents to use teamEvolver as their context and evolution control plane. OpenViking credentials remain on teamEvolver server side; using the **tenant machine credential** (`tevt_`, the only machine credential on the Agent data plane), Agents can:

- Report versioned Session data;
- Resolve and read personal/team Memory and Skill context;
- Only write or forget mapped users' personal Memory;
- Execute baseline or candidate replay branches in the Agent's real runtime (server-driven per-turn Turn protocol, or single synchronous request fallback);
- Receive published team Skill updates.

Protocol version is `1.0`. Unknown major versions return `PROTOCOL_VERSION_UNSUPPORTED` error. Payloads without version numbers processed via single-cycle legacy adapter.

Related code: `teamEvolver/integrations/agent_protocol.py`

## Registration

### Registration Endpoint

```
POST /internal/agents/register
Authorization: Bearer <control-plane-key>
```

Control plane key is environment variable `EVOLVE_INGEST_API_KEY`. When not configured V1 registration fails closed by default.

Code entry point: `teamEvolver/integrations/agent_registry.py:register_agent()`

### Registration Payload Format

V1 registration must specify `schema_version` as `teamevolver.agent-registration.v1`, must not contain OpenViking endpoints or keys.

Minimum registration payload example:

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
      "orchestration": "server_driven",
      "endpoint": "https://agent.example/replay/v1",
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

### Capability Field Reference

| Field | Type | Description |
|-------|------|-------------|
| `session.ingest.v1` | object | Supports Session ingestion |
| `context.workspace.v1` | object | Supports Context Workspace; `scopes` specifies accessible ranges |
| `replay.branch.v1` | object | Supports True Replay. `endpoint` specifies the callback endpoint; `orchestration: "server_driven"` enables the server-driven Turn protocol. `max_interactions` (default 20) and `idempotent` (default false) have server-filled defaults; `supports_*` fields are not consumed by the Replay code |
| `skill.sync.v1` | object | Supports Skill push sync; must provide `skill_sync_url` |

### Subject Mappings

Control plane can synchronize mappings for users already existing in teamEvolver during registration:

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

This extension only accepted on registration routes authenticated with control plane key. It does not automatically create users or credentials. Unknown target users reported in `subject_sync.missing_user_ids`; authoritative sync removes stale mappings for same integration.

### Registration Response

Registration **issues no token at all** (its job is to declare identity, capabilities, and endpoints): the response contains no `credentials`, and the Agent data plane uniformly uses the **tenant machine credential** (prefix `tevt_`).

On multi-tenant deployments the credential is issued by the console when a tenant is created; the tenant identity is always derived server-side from the credential (a client-supplied `X-Tenant-Id` that disagrees is rejected), the server stores only its SHA-256 hash, and rotation is `POST /api/tenants/{tenant_id}/rotate-token` (the old credential dies immediately). On single-tenant deployments (`storage_pg` disabled) the operator configures one credential via the `TEAMEVOLVER_TENANT_TOKEN` environment variable (or `tenant.machine_token` in config.yaml); it must start with `tevt_`, the server resolves it to the `default` tenant, and rotation means changing the value and restarting the process. The credential is not narrowed by capability → scope, but Context Workspace calls must declare `integration_id`.

## Authentication

The credential together with the declared `integration_id` identifies an integration, not individual users. Each Context request must also provide `external_subject` parameter. Admins map via:

```
integration_id + external_subject -> teamEvolver user
```

Unmapped subjects return `403 SUBJECT_NOT_MAPPED`. Runtime username-only mappings are legacy mode, no longer supported in V1.

The Agent data plane has only one machine credential:

- **Tenant machine credential** (`tevt_`): tenant-wide full-privilege credential; the tenant identity is derived server-side from the credential. Any bearer starting with `tev1_` is rejected (`401 AGENT_ACCESS_TOKEN_RETIRED`). Context Workspace calls **must declare `integration_id`** (Query parameter on the `describe`/`skills` GET routes, Body field on the other seven POST routes); the server validates that the Agent belongs to the current tenant and is `active`. The declared identity owns the resource: `context_ref`, Context Sessions, and audit records are all keyed by the declared `integration_id`. This credential cannot call admin routes (`/api/*`) or the Agent registration route (`/internal/agents/register`).

On single-tenant deployments (`storage_pg` disabled) the console does not issue credentials: the operator configures one credential via the `TEAMEVOLVER_TENANT_TOKEN` environment variable (or `tenant.machine_token` in config.yaml); it must start with `tevt_` and the server resolves it to the `default` tenant. On single-tenant deployments this credential is administrator-equivalent on the machine paths.

Code implementation: `teamEvolver/proxy/agent_context.py:119` (`_agent_context_auth` and `_agent_context_user`)

## Context Workspace

All Context Workspace calls use:

```
Authorization: Bearer <tenant machine credential tevt_<random>>
```

Every route must also declare `integration_id`: a Query parameter on `describe`/`skills`, and a Body field on the other seven POST routes.

### Endpoint List

| Method | Path | Description |
|--------|------|-------------|
| GET | `/internal/agents/context/describe` | Get scope description and budget limits |
| POST | `/internal/agents/context/resolve` | Resolve context entries per query, returns opaque `context_ref` |
| POST | `/internal/agents/context/read` | Read content of specified `context_ref` |
| GET | `/internal/agents/context/skills` | Get skills manifest |
| POST | `/internal/agents/context/remember` | Write personal Memory |
| POST | `/internal/agents/context/forget` | Delete personal Memory |
| POST | `/internal/agents/context/sessions/start` | Start a Context Session |
| POST | `/internal/agents/context/sessions/append` | Append events to Context Session |
| POST | `/internal/agents/context/sessions/commit` | Commit Context Session and report usage |

Code implementation: `teamEvolver/proxy/agent_context.py`

### Opaque Refs

`resolve` returns short-lived opaque `context_ref` values (format: `ctx_<random>`), never returns personal OpenViking URIs or keys. `read` only accepts refs issued by same integration and user. Team Memory and team Skills are read-only. `remember` and `forget` restricted to personal Memory scope.

Ref default TTL 900 seconds (15 minutes), minimum 60 seconds, maximum 3600 seconds.

Code implementation: `teamEvolver/integrations/context_workspace.py:93` (`issue_ref`)

### Session Commit and Usage Reporting

Session commit can contain explicit ref list Agent actually read:

```json
{
  "context_session_id": "ctxs_...",
  "used_context_refs": ["ctx_...", "ctx_..."]
}
```

teamEvolver resolves these refs server-side and submits `session.used` records to OpenViking before `session.commit`. Refs must belong to same Context Session, integration, and user. Usage reporting persisted per payload, so failed commits can be retried without double-counting OpenViking usage.

Locally deployed OpenViking services must persist `/used` records across HTTP requests until Commit consumes them; teamEvolver's local OpenViking service stores this pending state in Session's `.usage.jsonl`.

Code implementation: `teamEvolver/proxy/agent_context.py:322` (`_agent_context_submit_usage`)

### Content Hierarchy and Default Injection

- Default injection should use L0 (abstract) / L1 (overview) levels.
- Full content or Skill Bundles require explicit `read` call (`level=full`).
- When global entry budget spans multiple request scopes, results must be interleaved by scope before applying budget to prevent oversized personal space crowding out team Memory or Skill context.

Four content levels:

| Level | OpenViking Endpoint | Description |
|-------|---------------------|-------------|
| `l0` | `/api/v1/content/abstract` | Abstract (~1000 chars) |
| `l1` | `/api/v1/content/overview` | Overview (~4000 chars) |
| `l2` | `/api/v1/content/read` | Read with offset and limit |
| `full` | `/api/v1/content/read` | Full content (returns bundle for Skills) |

## Session Ingest

```
POST /ingest_session
Authorization: Bearer <tenant machine credential tevt_<random>>
```

Code entry point: `teamEvolver/proxy/routes.py:2971` (`ingest_session`)

### Required Identity Fields

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

`runtime.integration_id` must belong to the current tenant (derived from the credential) and be `active`; a missing credential returns `401 TENANT_TOKEN_REQUIRED`, an unregistered id returns `403 UNKNOWN_INTEGRATION_ID`, and an inactive Agent returns `403 INTEGRATION_DISABLED`. Context references verified against server credentials; caller-provided scope or URI values discarded.

`runtime_context.external_subject` is required field for subject mapping resolution. Returns `403 SUBJECT_NOT_MAPPED` when unmapped.

## Replay Branch

HTTP Agent exposes exact endpoint registered in `replay.branch.v1`. The primary mode is the **server-driven Turn protocol**: the Agent declares `orchestration: "server_driven"` at registration, and teamEvolver calls the Agent's turn endpoint once per interaction turn (`teamevolver.replay-turn-request.v1` / `teamevolver.replay-turn-result.v1`), with the multi-turn loop, checklist judging, and metric aggregation all orchestrated server-side. Registrations without `orchestration` fall back to a single synchronous request: teamEvolver sends one synchronous request per branch (`teamevolver.replay-branch-request.v1`), and baseline/candidate calls execute concurrently, sharing the same Context and execution manifest.

Code implementation: `team_replay/adapters.py` (`TurnBasedReplayAdapter`, `MappedHttpAdapter`, `HttpReplayAdapter`)

### Timeouts and Deadlines

Caller controls deadline. Agent must stop before `limits.timeout_seconds`; must not continue consuming model or tool resources after HTTP caller timeout. timeout_seconds range: 30-3600 seconds; max_interactions range: 1-20. In server-driven mode the per-turn limit is `limits.turn_timeout_seconds` (30-3600 seconds, default 600).

### Request Format (single-call fallback mode)

Replay request format teamEvolver sends to Agent:

```json
{
  "schema_version": "teamevolver.replay-branch-request.v1",
  "protocol_version": "1.0",
  "request_id": "replay_<hash>",
  "job_id": "<job-id>",
  "branch": "baseline",
  "case": {
    "query": "<task instruction>",
    "materials": []
  },
  "limits": {
    "timeout_seconds": 600,
    "max_interactions": 4
  },
  "context_snapshot": {},
  "skill": {},
  "current_skill": {},
  "source_session": {}
}
```

### Success Response Metrics

Success results must contain non-negative integer metrics:

| Metric | Description |
|--------|-------------|
| `interaction_turns` | Interaction turns |
| `tool_call_count` | Tool calls |
| `total_tokens` | Total token consumption |

Missing metrics, `request_id`/`branch` mismatch, or invalid schema fail-closed as `INVALID_RESPONSE`. In server-driven mode the Agent returns `teamevolver.replay-turn-result.v1` per turn, and each turn must report `metrics.tool_call_count` and `metrics.total_tokens` (fail-closed); the server aggregates them.

### Response Format (single-call fallback mode)

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_<hash>",
  "branch": "baseline",
  "status": "succeeded",
  "metrics": {
    "interaction_turns": 3,
    "tool_call_count": 5,
    "total_tokens": 4500
  },
  "output": {
    "final_response": "..."
  },
  "trace": {
    "messages": [],
    "events": [],
    "interactions": []
  },
  "context_input_hash": "<sha256>",
  "runtime_checklist_report": {},
  "elapsed_seconds": 45.2
}
```

### Runtime Isolation Requirements

Runtime must isolate replay state and credentials:

- Only instantiate source tenant/user/runtime configuration needed for branch; must not load full production database;
- Inject frozen Context projection actually used at runtime, return its hash as `context_input_hash`;
- Keep upstream model credentials outside candidate control process, behind short-lived parent proxy;
- Place Worker in private network namespace; local model sidecar connects to parent proxy via protected Unix socket;
- Keep branch workspace as only writable host path;
- When recorded external side effects cannot be deterministically injected into current runtime, return `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` error.

Agents supporting recorded external tool injection must identify each result via normalized tool name, normalized parameter signature, same-signature call sequence, and result SHA-256. Matching by tool name alone does not meet Protocol V1 specification.

Pi Agent currently declares `external_tool_replay=fail-closed`: workspace-local tools execute in branch sandbox, while network-capable or external tools make case unrunnable rather than falling back to live side effects.

### Checklist and Efficiency Comparison

Checklist completion is gate condition, not weighted score. Efficiency comparison ordered by: interaction_turns (fewer better), tool_call_count (fewer better), total_tokens (fewer better).

## Skill Sync

Agents supporting `skill.sync.v1` can receive Skill updates two ways:

1. **Pull mode**: Proactively pull skills manifest via `GET /internal/agents/context/skills`
2. **Push mode**: Provide `skill_sync_url` at registration; teamEvolver sends webhook callback to that URL on Skill publish/rollback

Code implementation: `teamEvolver/integrations/skill_sync_adapters.py`

### Push Callback Format

```json
{
  "schema_version": "teamevolver.skill-changed.v1",
  "protocol_version": "1.0",
  "event_id": "skill_evt_<hash>",
  "action": "publish",
  "job_id": "<mutation-id>",
  "skills": [
    {
      "name": "<skill-name>",
      "version": 3,
      "sha256": "<hash>",
      "tree_sha256": "<tree-hash>"
    }
  ],
  "tenant_ids": ["<tenant>"]
}
```

### Acknowledgment and Verification

Agent must return after receiving push:

```json
{
  "ok": true,
  "results": {
    "<tenant-id>": {
      "verification": {
        "skills": [
          {
            "name": "<skill-name>",
            "matched": true,
            "actual_version": 3,
            "actual_sha256": "<hash>",
            "actual_tree_sha256": "<tree-hash>"
          }
        ]
      }
    }
  }
}
```

teamEvolver verifies version numbers and hashes match; mismatches marked sync failure and retried.

## Rollout Sequence

Recommended rollout order:

1. Register as V1 and map subjects;
2. Run Context in `shadow` mode;
3. Switch one integration to `enabled`;
4. Enable V1 Session ingestion;
5. Enable Context-aware Replay;
6. After one compatibility cycle disable legacy storage and shared key paths.

Never lower replay security, baseline CAS, or central Checklist adjudication during rollback.

## JSON Schema Reference

| Schema | Path |
|--------|------|
| Agent Registration | `docs/schemas/agent-registration-v1.schema.json` |
| Session Ingest | `docs/schemas/agent-session-v1.schema.json` |
| Context Request | `docs/schemas/agent-context-request-v1.schema.json` |
| Context Result | `docs/schemas/agent-context-result-v1.schema.json` |
| Context Snapshot | `docs/schemas/agent-context-snapshot-v1.schema.json` |
| Replay Request | `docs/schemas/replay-branch-request-v1.schema.json` |
| Replay Result | `docs/schemas/replay-branch-result-v1.schema.json` |
| Replay Turn Request | No standalone schema file; schema constant `teamevolver.replay-turn-request.v1` defined in `team_replay/protocol.py:REPLAY_TURN_REQUEST_SCHEMA_V1` |
| Replay Turn Result | No standalone schema file; schema constant `teamevolver.replay-turn-result.v1` defined in `team_replay/protocol.py:REPLAY_TURN_RESULT_SCHEMA_V1` |


## Ontology Compile

Ontology compilation belongs to the TE administrator control plane, not ordinary Agent publication tools. See [Ontology API](../api/18-ontology-operations.md)。

Administrator ontology builds accept an optional `skill_uri`, falling back to the service default. It does not grant publication permissions or allow the browser to choose an OV account or compiler identity.
