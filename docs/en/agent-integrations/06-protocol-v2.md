# Agent Protocol v2

The Agent data plane uses `Authorization: Bearer tevt_...` and a `user_id` declared in the request. The server determines the tenant from the tenant key and the OpenViking Account from the tenant's effective configuration. A caller holding the tenant key can act for any user in that tenant, with no Agent registration or user mapping. Console accounts and Agent integration are independent.

## Identity

The only principal is `(tenant_id, user_id)`, resolved by `teamEvolver/security/agent_principal.py:resolve_agent_principal`. `user_id` rules (`normalize_user_id`):

- A non-empty string of 1–160 characters after NFKC normalization and trimming.
- Only letters, digits and `_`, `-`, `.`, `@`.
- Not `.` or `..`, and must not contain `..`.

Violations return `400 USER_ID_INVALID`; a missing value returns `400 USER_ID_REQUIRED`. Same-named users in different tenants are isolated. Clients cannot override the tenant or Account; an `X-Tenant-Id` different from the key's tenant returns 403.

## Data Plane

| Endpoint | Identity field | Purpose |
| --- | --- | --- |
| `POST /ingest_session` | Body `runtime_context.user_id` | Report Sessions |
| `GET /internal/agents/context/describe` | Query `user_id` | Context capabilities and scopes |
| `GET /internal/agents/context/skills` | Query `user_id` | Skill list and references |
| `POST /internal/agents/context/resolve`, `read`, `remember`, `forget` | Body `user_id` | Retrieval, reads, personal Memory writes and deletes |
| `POST /internal/agents/context/sessions/start`, `append`, `commit` | Body `user_id` | Context Session |
| `GET /sync/skills` | Query `user_id` | Team Skill bundles |

`runtime.type` is used only for analysis, display and Replay selection, never for authentication. Tenant keys can also access `/status`, `/history` and `/api/datasource/pull`; other console endpoints return 403.

## Minimal Session

```json
{
  "schema_version": "teamevolver.agent-session.v2",
  "protocol_version": "2.0",
  "session_id": "task-001",
  "runtime": {"type": "my-agent"},
  "runtime_context": {"user_id": "alice"},
  "turns": [{"turn_num": 1, "prompt_text": "Analyze cost", "response_text": "Cost report done."}]
}
```

## Schemas

- [Session v2](../../schemas/agent-session-v2.schema.json)
- [Context request v2](../../schemas/agent-context-request-v2.schema.json)
- [Context result v2](../../schemas/agent-context-result-v2.schema.json)
- [Context snapshot v2](../../schemas/agent-context-snapshot-v2.schema.json)

## Boundaries and Constraints

- The Session envelope accepts only `schema_version=teamevolver.agent-session.v2` and `protocol_version=2.0`; `runtime.integration_id` and `runtime_context.external_subject` belong to the retired Agent registration protocol and return 400 when present.
- Team Skills are delivered only by pull through `GET /sync/skills`.
- Only personal Memory can be written; team Memory, team Skills and team Resources are read-only for Agents.
- Context references expire after 15 minutes and can only be used by the `(tenant_id, user_id)` they were issued to.

See [Session Ingestion](../api/03-session-ingest.md), [Agent Context](../api/04-context-workspace.md), [Skill Pull](../api/06-skill-sync.md) and [Replay Adapter](../api/05-replay-branch.md).
