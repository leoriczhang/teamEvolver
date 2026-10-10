# Agent registration is retired

teamEvolver no longer exposes `/internal/agents/register`,
`/api/agent-integrations`, or a separate Agent registry. New clients connect
with a tenant Key and `user_id`; they do not pre-register an Agent, declare
capabilities, or receive a separate Agent token.

## Identity model

- Multi-tenant deployments issue one `tevt_` tenant machine credential when a tenant is created; only its hash is stored.
- Single-tenant deployments configure the same credential through `TEAMEVOLVER_TENANT_TOKEN`.
- `user_id` identifies a business user inside the tenant Account.
- `integration_id` is an ownership label for Context, Sessions, and audit records, not a registry key.
- OpenViking access uses only the server-side `sharing.viking_api_key` Root Key.

## Current entry points

| Scenario | Endpoint |
|----------|----------|
| Session ingest | `POST /ingest_session` |
| Context description | `GET /internal/agents/context/describe` |
| Context resolution | `POST /internal/agents/context/resolve` |
| Context read/write | `/internal/agents/context/*` |
| Skill pull | `GET /skills` |
| Replay | Configured through `validation.runtimes` and Replay Adapter |

See:

- [Protocol V2: tenant Key and user identity](../agent-integrations/06-protocol-v2.md)
- [Context Workspace API](./04-context-workspace.md)
- [Session ingest](./03-session-ingest.md)

Legacy registration endpoints return `404`. Do not send registration schemas,
capability declarations, callback endpoints, or Agent-scoped storage
credentials.
