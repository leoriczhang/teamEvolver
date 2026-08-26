# API Overview

This document describes the HTTP API interfaces for the teamEvolver service.

## Basic Information

| Item | Value |
|------|-------|
| Default Base URL | `http://<host>:52010` |
| Content-Type | `application/json` (except for file uploads) |
| Character Encoding | UTF-8 |

teamEvolver uses a single unified port `52010` for all HTTP interfaces, including health checks, Agent Protocol APIs, console APIs, and static web console assets.

## Authentication

teamEvolver API uses three authentication mechanisms:

| Authentication Method | Applicable Scenarios | Header Format |
|----------------------|---------------------|---------------|
| Control Plane Key | Agent registration (`/internal/agents/register`) | `Authorization: Bearer <EVOLVE_INGEST_API_KEY>` |
| Agent Access Token | Agent Protocol APIs (Session ingest, Context Workspace) | `Authorization: Bearer <agent_access_token>` |
| Console Session Cookie | Console management APIs (`/api/*`) | Cookie: `teamEvolver_console_session=<token>` |
| No Authentication | Health checks, status queries | No authentication required |

### Control Plane Key

The key configured via the `EVOLVE_INGEST_API_KEY` environment variable, used for registering new Agents. This has the highest privilege level and must be kept secure. If this variable is not configured, the V1 registration endpoint returns a 503 error.

Code entry point: `teamEvolver/proxy/routes.py:768` (`_check_v1_control_plane_key`)

### Agent Access Token

A token issued by teamEvolver after successful registration (format `tev1_<random>`), with privileges limited to the scopes corresponding to the capabilities declared during registration. The token is bound to an integration_id, and the server only stores its SHA-256 hash.

Token scope mapping (`teamEvolver/integrations/agent_registry.py:201` `_access_scopes`):

| Capability | Granted Scopes |
|-----------|---------------|
| `session.ingest.v1` | `session.ingest` |
| `context.workspace.v1` | `context.describe`, `context.resolve`, `context.read`, `context.skills`, `context.session` |
| `memory.personal.write.v1` | `context.remember`, `context.forget` |

Code entry point: `teamEvolver/integrations/agent_registry.py:259` (`verify_agent_access_token`)

### Console Session

An HttpOnly Cookie obtained after logging in via `/api/auth/login`, valid for 24 hours. Management interfaces under the `/api/*` path require this authentication, with additional permission checks for admin users.

Code entry point: `teamEvolver/proxy/routes.py:1699` (`require_console_auth` middleware)

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
| [Agent Registration](./02-agent-register.md) | Register Agent runtime, obtain access token |
| [Session Ingest](./03-session-ingest.md) | Submit Agent session trajectory data |
| [Context Workspace](./04-context-workspace.md) | Context resolution, reading, Memory read/write |
| [Replay Branch Execution](./05-replay-branch.md) | teamEvolver callback to Agent for True Replay execution |
| [Skill Sync](./06-skill-sync.md) | Skill pull and push synchronization |

### Control Plane Interfaces

| Document | Description |
|---------|-------------|
| [Health and Status](./07-health-status.md) | Health checks, service status, manual evolution trigger |
| [Session Queries](./08-sessions-api.md) | Query queued Sessions and processed conversations |
| [Skill Management](./09-skills-admin.md) | Skill CRUD, publish, rollback, version management |
| [Validation and Candidates](./10-validation.md) | Candidate Skill review, approval, rejection, Replay results |
| [Team Memory Aggregation](./11-team-memory-aggregation.md) | Aggregate cross-user memories into shared team knowledge, task progress, OKF Skill editing |

### Documentation Maintenance

| Document | Description |
|---------|-------------|
| [Documentation Maintenance Guide](./99-docs-maintenance.md) | Documentation writing standards and maintenance processes |
