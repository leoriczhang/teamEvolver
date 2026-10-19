# API Overview

All HTTP endpoints are served by the `api` role on one port (default 52010). The full route list is in the [Endpoint Index](./00-endpoints.md), which is generated from code.

| Item | Value |
| --- | --- |
| Base URL | `http://<host>:52010` |
| Content-Type | `application/json` (file uploads also use JSON + base64 fields) |
| Encoding | UTF-8 |
| Long tasks | `202 Accepted` with `job_id` and `status_url` |

## Authentication

Authentication is done by `teamEvolver/admin/authn.py:build_console_authorizer`. Each request is identified as one of the following, in order:

| Identity | Header | Tenant source | Scope |
| --- | --- | --- | --- |
| Service Root Key | `Authorization: Bearer <TEAMEVOLVER_ROOT_API_KEY>` | `X-Tenant-Id`, default `default` | All endpoints, treated as the platform administrator service identity |
| Tenant Key | `Authorization: Bearer tevt_...` | Resolved from the key | Agent data-plane paths only |
| Console Session | Cookie `teamEvolver_console_session_<hash>` + `X-Tenant-Id` | Explicit `X-Tenant-Id`; membership is checked on every request | Console endpoints, authorized by capability |
| Anonymous | None | — | `/`, `/console`, `/healthz`, `/readyz`, `/api/auth/*`, static assets |

### Service Root Key

`TEAMEVOLVER_ROOT_API_KEY` is a deployment-level secret for operations scripts and the CLI (`teamEvolver datasource` calls the API with it). It maps to the platform administrator service identity and can select any active tenant via `X-Tenant-Id`; a missing or disabled tenant returns `403 TENANT_ACCESS_DENIED`.

Only the Root Key can read and write tenant Python source: `/api/datasource/code`, `/api/replay-adapter/code` and their `test` endpoints. The Root Key must never be sent to a browser.

### Tenant Key

Tenant keys start with `tevt_` and are the only machine credential of the Agent data plane. The server stores only their SHA-256 digest and resolves the tenant through `teamEvolver/tenants/registry.py:TenantRegistry`. An invalid key or disabled tenant returns `401 invalid tenant token`; an `X-Tenant-Id` that differs from the key's tenant returns `403 tenant mismatch`.

Tenant keys can only access these paths (`_AGENT_MACHINE_PATHS` in `teamEvolver/admin/authn.py`):

- `/ingest_session`
- `/internal/agents/context/*`
- `/sync/skills`
- `/api/datasource/pull`
- `/status`, `/history`

Other paths return `403 console admin required`. The caller declares `user_id` in the request and the principal is `(tenant_id, user_id)`; rules are in [Protocol v2](../agent-integrations/06-protocol-v2.md).

Issuing and rotation:

- `POST /api/tenants` returns the plaintext key once when a platform administrator creates a tenant.
- Tenant administrators rotate it with `POST /api/tenants/{tenant_id}/rotate-token`; the old key stops working immediately.
- The `default` tenant's key is not issued by the console. It is the single key configured in `TEAMEVOLVER_TENANT_TOKEN` (must start with `tevt_`); rotate it by changing the value and restarting.

### Console Session

A successful `POST /api/auth/login` sets an HttpOnly cookie named `teamEvolver_console_session_<12-char hash of host:port:root_path>`, valid for 24 hours and renewed while in use. The browser must send an explicit `X-Tenant-Id` on every request, and the server checks membership with `teamEvolver/admin/authorization.py:resolve_console_membership`:

- Members get `workspace.use`, `sessions.use`, `personal.manage`, `team.read`, `team.submit`.
- Tenant administrators additionally get `tenant.manage`, `members.manage`, `team.publish`.
- Platform administrators can enter any active tenant with tenant administrator capabilities, plus `platform.manage`, `users.manage`, `audit.read`.

Without a valid tenant only control-plane paths such as login, identity and tenant management are available; others return `403 TENANT_MEMBERSHIP_REQUIRED` or `403 TENANT_ACCESS_DENIED`. See [Console Identity and Tenant Permissions](./20-console-rbac.md).

## Asynchronous Tasks

Operations that write to external systems or take time write the domain record and `worker_tasks` in one short transaction, then return:

```json
{"job_id": "...", "status": "queued", "status_url": "/api/jobs/<job_id>"}
```

Each module's accept response also includes its own fields (such as `ingestion_id`, `run_id`). Use the [unified job endpoints](./07-health-status.md#unified-job-endpoints) to query, cancel or retry; each module's `.../jobs/{job_id}/result` returns the business result.

For accept endpoints that take an `Idempotency-Key` header, resubmitting the same key returns the first accepted task; the same key with a different body returns 409. Keys are at most 200 characters.

## Error Format

Errors are an HTTP status code + JSON:

```json
{"detail": "TENANT_ADMIN_REQUIRED"}
```

Common errors:

| Status | detail | Meaning |
| --- | --- | --- |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | Agent request is missing `user_id` or uses an invalid one |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED` | The Session envelope `protocol_version` is not `2.0` |
| 401 | `TENANT_TOKEN_REQUIRED` | The Agent data plane did not use a valid tenant key |
| 401 | `login required` | Console endpoint without login |
| 403 | `TENANT_ADMIN_REQUIRED` / `PLATFORM_ADMIN_REQUIRED` | Insufficient capability |
| 403 | `CONTEXT_SCOPE_FORBIDDEN` | The Context scope does not allow the operation |
| 404 | `CONTEXT_REF_INVALID` | `context_ref` does not exist, was revoked or belongs to another user |
| 409 | — | Version conflict (`expected_revision` / `expected_sha256` mismatch) or the state does not allow the operation |
| 413 | — | Request body exceeds the endpoint limit |
| 503 | — | Tenant module configuration missing, or an external dependency is not configured or unavailable |

## Request Limits

| Endpoint | Limit |
| --- | --- |
| `POST /ingest_session` body | 32 MiB, adjustable via `TEAMEVOLVER_MAX_SESSION_BODY_BYTES`, minimum 1 KiB |
| Context body | 256 KiB |
| Context `resolve.query` | 8000 characters |
| Context `remember.content` | 128 KiB |
| Context `read` content per call | 500 KiB; Skill bundles at most 100 files |
| `context_usage` references per kind | 200 |
| Replay / dataset body | 96 MiB |
| Skill library write body | 96 MiB |
| Memory body | 256 KiB |

## API Groups

| Page | Content |
| --- | --- |
| [Session Ingestion and Data Sources](./03-session-ingest.md) | `POST /ingest_session`, data source Adapters and scheduled pulls |
| [Agent Context](./04-context-workspace.md) | The nine Context endpoints |
| [Replay and Experiment Workbench](./05-replay-branch.md) | Replay Adapter, datasets, Replay runs, workbench |
| [Skill Pull](./06-skill-sync.md) | `GET /sync/skills` |
| [Health, Status and Jobs](./07-health-status.md) | `/healthz`, `/readyz`, `/status`, `/api/jobs/*` |
| [Session Queries](./08-sessions-api.md) | Console Session list, content, export |
| [Team Skill Library](./09-skills-admin.md) | Skill CRUD, versions, rollback, delivery records |
| [Candidates and Evolution](./10-validation.md) | Skill Candidates, evolution triggers, Prompt and evolution settings |
| [Team Memory](./11-team-memory-aggregation.md) | Memory runs, maintenance, changes and Memory Replay |
| [Service Logging Status](./15-logging-status.md) | `/api/logging/status` |
| [Successful Experiences](./16-experience-sync.md) | Experiences, Patterns and experience sync |
| [Skill Import and Export](./18-skill-transfer.md) | ZIP, marketplace, Git |
| [Console Identity and Tenant Permissions](./20-console-rbac.md) | Login, users, tenants, members, platform settings |
| [Knowledge Base Mining](./22-knowledge-mining.md) | `/api/knowledge-mining/*` |
| [Workspace and Platform Assets](./23-workspace-assets.md) | `/api/openviking/workspace/*`, `/api/platform-assets/*` |
| [Docs Maintenance](./99-docs-maintenance.md) | Docs conventions, endpoint index and check scripts |
