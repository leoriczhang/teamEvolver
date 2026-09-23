# API overview

The Agent data plane uses `tevt_ + user_id`; see [v2 identity](../agent-integrations/06-protocol-v2.md).
Administrators use console sessions or the service Root Key. Tenant Keys cannot call admin APIs.

| Capability | API |
| --- | --- |
| Session ingestion | [POST /ingest_session](./03-session-ingest.md) |
| Nine Context routes | [Context Workspace](./04-context-workspace.md) |
| Skill bundles | [GET /sync/skills](./06-skill-sync.md) |
| Replay binding and source | [Replay adapters](./05-replay-branch.md) |
| Session management | [Sessions](./08-sessions-api.md) |
| Skill management | [Skills](./09-skills-admin.md) |
| Validation | [Validation](./10-validation.md) |
| Memory | [Team Memory](./12-team-memory.md) |

Missing/invalid user IDs return `400 USER_ID_REQUIRED` / `400 USER_ID_INVALID`.
Invalid tenant credentials return 401. Context resources additionally validate tenant/user and expiry.
See [the upgrade guide](../guides/11-agent-deregistration.md) for legacy retirement.

| Item | Value |
|------|-------|
| Default Base URL | `http://<host>:52010` |
| Content-Type | `application/json` (except for file uploads) |
| Character Encoding | UTF-8 |

teamEvolver uses a single unified port `52010` for all HTTP interfaces, including health checks, Agent Protocol APIs, console APIs, and static web console assets.

## Authentication

teamEvolver API uses three authentication mechanisms, plus a small set of unauthenticated health and compatibility endpoints:

| Authentication Method | Applicable Scenarios | Header Format |
|----------------------|---------------------|---------------|
| Control Plane Key | Agent registration (`/internal/agents/register`) | `Authorization: Bearer <EVOLVE_INGEST_API_KEY>` |
| Tenant Machine Credential | Agent data plane and protocol APIs (Session ingest, Context Workspace, datasource pull, status queries) | `Authorization: Bearer <tenant_agent_token>` |
| Console Session Cookie | Console management APIs (`/api/*`) | Cookie: `teamEvolver_console_session=<token>` |
| No Authentication | Health checks, status queries | No authentication required |

### Control Plane Key

The key configured via the `EVOLVE_INGEST_API_KEY` environment variable, used for registering new Agents (registration control plane) and the legacy (non-V1) Session ingest channel. This has the highest privilege level and must be kept secure. When this variable is unset, the V1 registration endpoint performs no authentication and registration requests proceed unauthenticated (always configure it in production); when the provided key does not match the configured one, 401 is returned.

Code entry point: `teamEvolver/proxy/routes.py:_check_ingest_api_key`

### Tenant Machine Credential

The **only machine credential** on the Agent data plane (format `tevt_<random>`): registration no longer issues any per-Agent access token, and any bearer starting with `tev1_` is rejected with `401 {"detail": "AGENT_ACCESS_TOKEN_RETIRED"}` (a temporary migration aid so credentials cached in Agent configs are diagnosable; planned for removal in a later minor).

It is the **tenant-level full-privilege machine credential**: the tenant identity is derived server-side from the credential (a client-supplied `X-Tenant-Id` that disagrees with the derivation is rejected), and it can call these machine paths:

- `/ingest_session` (including Agent Protocol V1 ingest)
- `/internal/agents/context/*` (all 9 Context Workspace routes)
- `/langfuse/pull`, `/api/datasource/pull`, `/trigger`, `/status`, `/history`, `/sessions`, `/conversations`, `/storage/status`

The credential is not narrowed by capability → scope mapping (full privilege), but **Context Workspace calls must declare `integration_id`** (query parameter on GET routes, body field on POST routes). The server validates that the Agent belongs to the current tenant and is `active`. The declared identity is the ownership key for `context_ref` issuance/validation, Context Sessions and audit records.

**Issuance and rotation:**

- Multi-tenant deployments: issued by the console when a tenant is created, displayed in full exactly once; the server only stores its SHA-256 hash. Rotate it via `POST /api/tenants/{tenant_id}/rotate-token`; the previous credential stops working immediately.
- Single-tenant deployments (`storage_pg` disabled): the console does not issue credentials; the operator configures **one** credential via the `TEAMEVOLVER_TENANT_TOKEN` environment variable (or `tenant.machine_token` in config.yaml). It must start with `tevt_`, and the server resolves it to the `default` tenant. Generate it with: `TEAMEVOLVER_TENANT_TOKEN="tevt_$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"`. Rotation means changing the value and restarting the process. The console's tenant page shows whether it is configured (and the generation command when it is not). On single-tenant deployments this credential is administrator-equivalent on the machine paths.

Management APIs (`/api/*`) and the Agent registration endpoint (`/internal/agents/register`) **do not accept** this credential. The `default` tenant never carries a console-issued credential: `POST /api/tenants/default/rotate-token` is rejected (400 `default is reserved`), and the console hides the rotate action for it.

Code entry point: `teamEvolver/tenants/registry.py:resolve_by_agent_token`

### Console Session

An HttpOnly Cookie obtained after logging in via `/api/auth/login`, valid for 24 hours. Management interfaces under the `/api/*` path require this authentication, with additional permission checks for admin users.

Code entry point: `teamEvolver/proxy/routes.py:require_console_auth` middleware

## Versioning

Agent Protocol APIs use the `protocol_version` field for version control. The current version is `1.0`. Unknown major versions return a `PROTOCOL_VERSION_UNSUPPORTED` error.

- Specify `protocol_version` at the top level of the payload during registration
- Specify in `runtime.protocol_version` during Session ingest
- Both Replay requests and responses include `schema_version` and `protocol_version` fields

## Error Format

All API errors use HTTP status codes uniformly with a JSON response body:

```json
{
  "detail": "Error description message"
}
```

Some Agent Protocol interfaces return structured error codes (string constants), for example:

- `SUBJECT_NOT_MAPPED` -- Subject not mapped (403)
- `PROTOCOL_VERSION_UNSUPPORTED` -- Protocol version not supported (400)
- `INVALID_PAYLOAD` -- Invalid request body format (400)
- `WORKSPACE_TOKEN_INVALID` -- Invalid access token (401)
- `TENANT_TOKEN_REQUIRED` -- V1 Session ingest is missing the tenant machine credential (401)
- `AGENT_ACCESS_TOKEN_RETIRED` -- The request carried a retired `tev1_` Agent access token (401)
- `INTEGRATION_ID_REQUIRED` -- Tenant machine credential used without declaring integration_id (400)
- `UNKNOWN_INTEGRATION_ID` -- Declared integration_id is not registered in this tenant (403)
- `INTEGRATION_DISABLED` -- Declared Agent is disabled (403)
- `CONTEXT_REF_INVALID` -- Invalid or expired context reference (404)
- `CONTEXT_SCOPE_FORBIDDEN` -- Context scope permission denied (403)

## Rate Limits

- Session ingest request body maximum 32MB (configurable via `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` environment variable, minimum 1KB)
- Context resolve query string maximum 8000 characters
- Context remember content maximum 128KB
- Context read single content maximum 500,000 characters
- Skill bundle reading maximum 100 files, total content not exceeding 500,000 characters
- Context Session used_context_refs maximum 200 entries

## API Groups

### Agent Protocol Interfaces

| Document | Description |
|---------|-------------|
| [Agent Registration](./02-agent-register.md) | Register Agent runtime identity (no token is issued) |
| [Session Ingest](./03-session-ingest.md) | Submit Agent session trajectory data |
| [Context Workspace](./04-context-workspace.md) | Context resolution, reading, Memory read/write |
| [Replay Branch Execution](./05-replay-branch.md) | teamEvolver callback to Agent for True Replay execution |
| [Skill Sync](./06-skill-sync.md) | Skill pull and push synchronization |

### Control Plane Interfaces

| Document | Description |
|---------|-------------|
| [Health and Status](./07-health-status.md) | Health checks, service status, manual evolution trigger |
| [Session Queries](./08-sessions-api.md) | Query queued Sessions and processed conversations |
| [Skill Management](./09-skills-admin.md) | Team/personal Skill CRUD, publish requests, rollback, and versions |
| [Validation and Candidates](./10-validation.md) | Candidate queries, Replay evaluation, release decisions, and deletion |
| [Team Memory Aggregation](./11-team-memory-aggregation.md) | Cross-user aggregation, run recovery, aggregation Skill, and output settings |

### Console-internal interfaces

The following `/api/*` endpoints serve the built-in console and require a console Session Cookie. They evolve with the console and are not part of the stable Agent Protocol V1 compatibility surface:

| Prefix | Purpose | Main documentation |
|--------|---------|--------------------|
| `/api/auth/*`, `/api/users/*`, `/api/team-settings` | Login, administrator bootstrap, users, and identity mapping | [Web Console](../guides/03-console.md) |
| `/api/openviking/workspace/*` | Workspace browsing, L0/L1, conditional batch writes, and CLI | [Storage Layout](../concepts/09-storage-layout.md) |
| `/api/replay-lab/*`, `/api/openviking/memory/*` | Skill/Memory experiments and True Replay | [Web Console](../guides/03-console.md) |
| `/api/mining/*` | Knowledge sources, mining jobs, artifacts, and LIFT | [Skill Miner Guide](../guides/07-skill-miner.md) |
| `/api/langfuse-config`, `/api/langfuse-tracing-config`, `/langfuse/*` | Tenant sources, global tracing, pull, mapping, and status | [Observability Guide](../guides/04-observability.md) |
| `/api/docs/*` | Built-in document tree, page reads, and search | [Documentation Maintenance](./99-docs-maintenance.md) |
| `/api/sharing-config`, `/api/model-settings`, `/api/model-settings/test`, `/api/skill-evolution/settings` | Sharing/local-fallback storage config, evolution model config (with connectivity test), and evolution settings | [Web Console](../guides/03-console.md) |
| `/api/skill-evolution/session-analysis/audit`, `/api/mined-skills`, `/api/mined-skills/{name}/submit` | Session filter audit queries, mining artifact queries and submission | [Skill Miner Guide](../guides/07-skill-miner.md) |
| `/api/openviking-accounts`, `/api/openviking-accounts/{account}/users`, `/api/openviking-accounts/{account}/import-users` | OpenViking account list, account users, and user import | [Web Console](../guides/03-console.md) |
| `/sessions`, `/conversations`, `/conversations/export`, `/conversations/status`, `/conversations/{session_id}`, `/conversations/{session_id}/process`, `/history` | Console Session/conversation browsing, export, processing status, and history queries | [Session Queries](./08-sessions-api.md) |
| `/v1/models`, `/v1/chat/completions` | OpenAI-compatible model proxy (model listing and chat completions) | — |
| `/internal/agentshub/openviking-config`, `/internal/reload-skills` | AgentsHub internal config delivery, Skill runtime reload | — |

### Documentation Maintenance

| Document | Description |
|---------|-------------|
| [Documentation Maintenance Guide](./99-docs-maintenance.md) | Documentation writing standards and maintenance processes |
