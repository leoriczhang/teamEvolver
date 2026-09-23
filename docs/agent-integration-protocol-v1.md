# Agent Protocol：迁移入口

新接入使用 [中文 v2 协议](./zh/agent-integrations/06-protocol-v2.md) /
[English v2 protocol](./en/agent-integrations/06-protocol-v2.md)。

旧客户端行为保存在 [v1 历史参考](./zh/agent-integrations/02-protocol-v1.md)，仅适用于兼容发布窗口。
阶段 5、6 的时序与回滚见 [升级指南](./zh/guides/11-agent-deregistration.md)。

以下为 v1 协议完整历史参考（英文）：

Protocol V1 lets an Agent use teamEvolver as its context and evolution control
plane. OpenViking credentials stay on the teamEvolver server. Agents use the
tenant machine credential (`tevt_`), the only machine credential on the Agent
data plane, and can:

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

V1 registration must not include OpenViking endpoints or keys. Registration
issues no token at all: the response contains no `credentials`, and
registration's job is to declare identity, capabilities, and endpoints.

The credential is the tenant machine credential (prefix `tevt_`), issued by the
console when a tenant is created on multi-tenant deployments: the tenant
identity is always derived server-side from the credential (a client-supplied
`X-Tenant-Id` that disagrees is rejected), the server stores only its SHA-256
hash, and rotation is `POST /api/tenants/{tenant_id}/rotate-token`, after which
the old credential dies immediately. On single-tenant deployments
(`storage_pg` disabled) the operator configures one credential via the
`TEAMEVOLVER_TENANT_TOKEN` environment variable (or `tenant.machine_token` in
config.yaml); it must start with `tevt_` and the server resolves it to the
`default` tenant. It is the only credential that can call the Agent Protocol
APIs.

## Identity

The credential and the declared `integration_id` identify an integration, not a
user. Every Context request also supplies `external_subject`. Administrators
map:

```text
integration_id + external_subject -> teamEvolver user
```

An unmapped subject returns `403 SUBJECT_NOT_MAPPED`. Runtime-only username
mapping is legacy-only.

The Agent data plane has exactly one machine credential:

- the tenant machine credential (`tevt_`) is tenant-wide and not narrowed by
  capability → scope, but Context Workspace calls must declare
  `integration_id`: a query parameter on `describe`/`skills`, a body field on
  the other seven POST routes. The server validates that the declared Agent is
  registered and `active` in the tenant resolved from the credential, then uses
  it as the ownership key for `context_ref` issuance/validation, Context
  Sessions and audit records (the declared identity owns the resource). It
  cannot reach admin routes (`/api/*`) or `/internal/agents/register`. Any
  bearer starting with `tev1_` is rejected with
  `401 AGENT_ACCESS_TOKEN_RETIRED`.

On single-tenant deployments (`storage_pg` disabled) the console does not issue
credentials: the operator configures one via `TEAMEVOLVER_TENANT_TOKEN` (or
`tenant.machine_token` in config.yaml); it must start with `tevt_` and the server
resolves it to the `default` tenant. There it is administrator-equivalent on the
machine paths.

## Context Workspace

All calls use:

```text
Authorization: Bearer <tenant machine credential tevt_<random>>
```

Every route must also declare `integration_id` (query parameter on
`describe`/`skills`, body field on the other seven POST routes); omitting it
returns `400 INTEGRATION_ID_REQUIRED`.

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
explicit `read`.

## Session Ingest

```text
POST /ingest_session
Authorization: Bearer <tenant machine credential tevt_<random>>
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

The `runtime.integration_id` must be a registered, `active` Agent of the tenant
resolved from the credential; a missing credential returns
`401 TENANT_TOKEN_REQUIRED`. Context references are verified against server-side
receipts; caller-supplied scope or URI values are discarded.

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
