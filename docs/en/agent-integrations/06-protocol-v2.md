# KEE R6 integration scope (2026-10-08)

Agents continue reading approved Wiki through existing OV authentication. UpClaw is unchanged. Optional structural HTTP reads are disabled by default, have no MCP registration and do not authorize tool or Skill execution. No additional Session or Judge pipeline is introduced. See [R6 governance API](../api/20-kee-r6.md). The original protocol and IDs below remain compatible.

# Agent integration protocol v2

The Agent data plane uses `Authorization: Bearer tevt_...` and a declared `user_id`.
The server resolves the tenant from the credential and the OpenViking Account from that tenant's
effective configuration. A tenant Key may act as any user in its Account; no Agent registration
or global console-user mapping is required. Console users are a separate concern.

## Identity

The principal is `(tenant_id, user_id)`. The user ID is NFKC normalized and trimmed, must be nonempty
and at most 160 characters, and allows letters, digits, `_-.@`. Path separators and `..` are rejected.
Equal user IDs in different tenants have isolated Accounts, Sessions and Context.
Client tenant/Account values cannot override the server; conflicting `X-Tenant-Id` is rejected.

## Data plane

| Endpoint | Identity field | Purpose |
| --- | --- | --- |
| `POST /ingest_session` | `runtime_context.user_id` | v2 Session ingestion |
| `GET /internal/agents/context/describe` | Query `user_id` | Context scopes |
| `GET /internal/agents/context/skills` | Query `user_id` | Skill refs |
| Other seven Context POST routes | Body `user_id` | Search, reads, Memory and Sessions |
| `GET /sync/skills` | Query `user_id` | Tenant-shared Skill bundle |

`runtime.type` is for analytics, display and runtime selection, never authentication.
Tenant credentials cannot access admin routes. OpenViking Root and model keys stay on the server.

## Minimal Session

```json
{
  "schema_version": "teamevolver.agent-session.v2",
  "protocol_version": "2.0",
  "session_id": "task-001",
  "runtime": {"type": "my-agent"},
  "runtime_context": {"user_id": "alice"},
  "turns": [{"turn_num": 1, "prompt_text": "Analyze costs", "response_text": "The cost report is ready."}]
}
```

See [Session API](../api/03-session-ingest.md), [Context API](../api/04-context-workspace.md),
[Skill pull](../api/06-skill-sync.md), and [Replay adapters](../api/05-replay-branch.md).
Schemas: [Session v2](../../schemas/agent-session-v2.schema.json),
[Context request v2](../../schemas/agent-context-request-v2.schema.json),
[Context result v2](../../schemas/agent-context-result-v2.schema.json),
[Context snapshot v2](../../schemas/agent-context-snapshot-v2.schema.json).

## Boundaries

`identity_mode` is fixed to `tenant_user`; `delivery_mode` defaults to `pull`.
Session, Context and Skill all use the tenant machine credential plus `user_id`.
