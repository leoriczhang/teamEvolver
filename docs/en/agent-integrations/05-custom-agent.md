# Custom Agent integration

Custom Agents connect through [Protocol V2](./06-protocol-v2.md). The system
does not require Agent registration and does not issue Agent-scoped tokens.
Authentication uses only a tenant machine credential; `user_id` identifies the
business user.

## Prerequisites

1. Obtain the teamEvolver service URL.
2. Obtain the `tevt_<random>` tenant machine credential.
3. Ensure the selected `user_id` belongs to the tenant Account.
4. Configure Session ingest, Context Workspace, Skill pull, and Replay Adapter as needed.

Multi-tenant credentials are issued once when the tenant is created. A
single-tenant deployment configures `TEAMEVOLVER_TENANT_TOKEN`.

## Session ingest

```http
POST /ingest_session
Authorization: Bearer tevt_<random>
Content-Type: application/json
```

The request includes a stable `session_id`, `user_id`, `runtime.type`, and
conversation content. `runtime.type` describes the runtime and is not an
authentication identity.

See [Session ingest](../api/03-session-ingest.md).

## Context Workspace

Context endpoints live under `/internal/agents/context/*`. Requests carry the
tenant credential and an explicit `user_id`. `integration_id` may be used as a
source and audit label, but it requires no registry record.

| Operation | Endpoint |
|-----------|----------|
| Describe capabilities | `GET /internal/agents/context/describe` |
| Resolve context | `POST /internal/agents/context/resolve` |
| Read content | `POST /internal/agents/context/read` |
| List Skills | `GET /internal/agents/context/skills` |
| Write Memory | `POST /internal/agents/context/remember` |
| Context Session | `/internal/agents/context/sessions/*` |

See [Context Workspace](../api/04-context-workspace.md).

## Skills and Replay

- Agents pull published Skill versions; publishing does not depend on a registered callback.
- True Replay runtimes come from `validation.runtimes` and the tenant Replay Adapter.
- Deployment owners configure custom HTTP or Python Runtime Adapters; Agents cannot submit adapter code or credentials.
- Baseline and Candidate executions must use isolated sessions and workspaces.

See [Replay API](../api/05-replay-branch.md) and
[Skill synchronization](../api/06-skill-sync.md).

## Verification

```bash
curl -fsS "http://<teamevolver-host>:52010/health"
curl -H "Authorization: Bearer ${TEAMEVOLVER_TENANT_TOKEN}" \
  "http://<teamevolver-host>:52010/status"
```

The server does not expose `/internal/agents/register` or
`/api/agent-integrations`; legacy calls return `404`.
