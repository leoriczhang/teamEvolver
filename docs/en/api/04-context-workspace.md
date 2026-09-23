# Context Workspace API v2

All routes are under `/internal/agents/context` and require `tevt_` plus `user_id`.
GET routes take identity in Query; POST routes take it in JSON, including operations otherwise
identified by a ref/session ID. See [v2](../agent-integrations/06-protocol-v2.md) for identity and Account rules.
Implementation: [routes](../../../teamEvolver/proxy/agent_context.py),
[principal](../../../teamEvolver/integrations/agent_principal.py),
[state store](../../../teamEvolver/integrations/context_workspace.py).

## Routes

| Method | Path suffix | Fields beyond user_id |
| --- | --- | --- |
| GET | `/describe` | — |
| POST | `/resolve` | `query`, optional `context_session_id`, scopes/budget |
| POST | `/read` | `context_ref`, `level` (`l0/l1/l2`) |
| GET | `/skills` | optional `scope`, `context_session_id` |
| POST | `/remember` | `content`, optional `title`, `category` |
| POST | `/forget` | `context_ref` |
| POST | `/sessions/start` | `external_session_id` |
| POST | `/sessions/append` | `context_session_id`, `event_id`, `sequence`, `role`, `content` |
| POST | `/sessions/commit` | `context_session_id`, optional `used_context_refs` |


`resolve` returns short-lived refs, layered content and a snapshot identifier; `read` expands a level.
v2 subjects contain only `tenant_id` and `user_id`. Clients cannot supply OpenViking URIs or credentials.
`remember/forget` write personal Memory only; team Memory and Skills are read-only.

New Session IDs include tenant, user and external Session ID. Existing Session IDs stay unchanged
during migration. Starting the same external Session for the same principal is idempotent.
Append sequences start at 1; identical event retries do not duplicate writes. Reusing an event ID
with different content returns 409. Commit submits actual used refs; repeated commits return duplicate.

## Example

```bash
curl --fail "$TEAMEVOLVER_URL/internal/agents/context/describe?user_id=alice"   -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"

curl --fail "$TEAMEVOLVER_URL/internal/agents/context/resolve"   -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"   -H "Content-Type: application/json"   -d '{"user_id":"alice","query":"Cost calculation methods"}'
```

## Errors and isolation

| HTTP | Cause |
| --- | --- |
| 401 | Missing or invalid tenant credential |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` or invalid request fields |
| 403 | Non-personal Memory write or forbidden scope |
| 404 | Missing, expired, revoked or differently owned ref/session |
| 409 | Event ordering/content conflict; some Session ownership failures |

A tenant Key can declare any user in that tenant, but a request must match the resource's owner.
Changing the user cannot cross the tenant boundary. State, snapshots, audit and commits preserve both
tenant and user. v1 is retained only for the compatibility window; its schemas are unchanged.

---

The sections below keep the full V1 Context Workspace API reference for the compatibility window.

# Context Workspace API (V1)

## 1. API Implementation Overview

The Context Workspace API provides Agents with a unified context access interface, including search, read, and write of personal/team Memory and Skills, as well as lifecycle management of Context Sessions. All interfaces accept only the **tenant machine credential** (`tevt_`, the only machine credential on the Agent data plane), and must provide the `external_subject` parameter for user identity resolution plus `integration_id` to declare the caller's identity.

The core design principle is **opaque references**: the `resolve` interface returns short-lived `context_ref`s (format `ctx_<random>`), and Agents read content through `context_ref` without ever touching underlying OpenViking URIs or storage credentials. Team Memory and Team Skills are read-only; only personal Memory supports remember/forget writes.

Code implementation: `teamEvolver/proxy/agent_context.py`
State management: `teamEvolver/integrations/context_workspace.py` (`ContextStateStore`)

## 2. Interface and Parameter Specification

All Context Workspace interfaces:

```
Authorization: Bearer <tenant machine credential tevt_<random>>
Content-Type: application/json
```

### General Authentication Notes

Each request identifies the user via `external_subject` (Query parameter or JSON Body field). The system maps `integration_id + external_subject` to a teamEvolver user; unmapped users receive `403 SUBJECT_NOT_MAPPED`.

| Credential | Format | Description |
|-----------|--------|-------------|
| Tenant machine credential | `tevt_<random>` | The only machine credential on the Agent data plane, tenant-wide full privilege with no capability → scope narrowing. Context Workspace calls **must declare `integration_id`**: as a Query parameter on the two GET routes (`describe`, `skills`), and as a JSON Body field on the seven POST routes (`resolve`, `read`, `remember`, `forget`, `sessions/start`, `sessions/append`, `sessions/commit`). The server validates that the declared `integration_id` is a registered, `active` Agent of the current tenant, and uses it as the ownership key for `context_ref` issuance/validation, Context Sessions and audit records (**declared identity owns the resource**). Missing it returns `400 INTEGRATION_ID_REQUIRED`; not belonging to this tenant returns `403 UNKNOWN_INTEGRATION_ID`; registered but not `active` returns `403 INTEGRATION_DISABLED`. |

On multi-tenant deployments the credential is issued by the console when a tenant is created and can be rotated on the tenant page; on single-tenant deployments (`storage_pg` disabled) the operator configures one credential via the `TEAMEVOLVER_TENANT_TOKEN` environment variable (or `tenant.machine_token` in config.yaml); it must start with `tevt_` and the server resolves it to the `default` tenant. On single-tenant deployments this credential is administrator-equivalent on the machine paths.

**Security trade-off:** there is no longer per-Agent isolation inside a tenant—the tenant machine credential can act as any registered Agent of that tenant, so it can read/write refs, Context Sessions and snapshots owned by other Agents of the same tenant; and because the users registry is service-global, `remember`/`forget` can target any mapped user's personal Memory. Cross-tenant access is still impossible: declaring another tenant's `integration_id` always returns 403.

---

### GET /internal/agents/context/describe

Get the current user's Context scope description, available operations, and budget limits.

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `external_subject` | string | Yes | Agent-side user identifier |
| `integration_id` | string | Yes | Declares the caller's identity: must be a registered, `active` Agent of the current tenant; the ownership key for refs, Context Sessions and audit records |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `protocol_version` | string | Protocol version `1.0` |
| `integration_id` | string | Integration ID |
| `subject.user_id` | string | Resolved teamEvolver user ID |
| `scopes` | object | Scope configurations |
| `scopes.<scope>.kind` | string | Type: `memory` or `skill` |
| `scopes.<scope>.space` | string | Space: `personal` or `team` |
| `scopes.<scope>.operations` | array[string] | List of allowed operations |
| `budgets.max_items` | integer | Maximum items per resolve (50) |
| `budgets.max_chars` | integer | Maximum characters per resolve (100,000) |
| `budgets.max_skill_bytes` | integer | Maximum Skill bundle bytes (500,000) |

**Scope List:** `personal_memory`, `team_memory`, `personal_skills`, `team_skills`

---

### POST /internal/agents/context/resolve

Search for relevant context entries based on a query string, returning a list of opaque `context_ref`s.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `external_subject` | string | Yes | User identifier |
| `query` | string | Yes | Query text, maximum 8,000 characters |
| `scopes` | array[string] | No | Search scopes, defaults to all four scopes |
| `max_items` | integer | No | Maximum return items, 1-50, default 12 |
| `max_chars` | integer | No | Maximum return characters, 500-100,000, default 16,000 |
| `context_session_id` | string | No | Associated Context Session ID |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `schema_version` | string | `teamevolver.context-result.v1` |
| `subject` | object | Subject information |
| `snapshot_id` | string | Context snapshot ID (`ctxsnap_<hash>`) |
| `items` | array | Context entry list |
| `items[].context_ref` | string | Opaque reference for subsequent read calls |
| `items[].scope` | string | Owning scope |
| `items[].kind` | string | `memory` or `skill` |
| `items[].title` | string | Entry title |
| `items[].l0` | string | Summary content |
| `items[].l1` | string | Overview content |
| `items[].version` | string | Version identifier |
| `items[].content_hash` | string | Content SHA-256 |
| `items[].selected` | boolean | Whether selected (may be shadowed due to skill deduplication) |
| `items[].qualified_skill_id` | string | Qualified skill ID (only when kind=skill), format `team:<name>` or `personal:<name>` |
| `receipts` | array | Receipt list (containing context_ref and metadata) |
| `warnings` | array | Warning messages (e.g., DUPLICATE_SKILL) |
| `budget` | object | Budget usage |
| `skills_etag` | string | Skills list ETag |

**Note:** Cross-scope results are returned interleaved by scope to ensure a single scope does not exhaust the budget. Personal/team Skills with the same name+description are deduplicated; the newer version is kept and the other marked `selected: false` with `shadowed_by`.

---

### POST /internal/agents/context/read

Read the content of a specified `context_ref`.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_ref` | string | Yes | Opaque reference returned by resolve |
| `level` | string | No | Content level: `l0` (summary), `l1` (overview, default), `l2`, `full` (complete content) |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields (memory or skill with level!=full):**

| Field | Type | Description |
|-------|------|-------------|
| `context_ref` | string | Reference ID |
| `scope` | string | Owning scope |
| `kind` | string | `memory` or `skill` |
| `level` | string | Returned content level |
| `content` | string | Text content |

**Response Fields (kind=skill and level=full):**

| Field | Type | Description |
|-------|------|-------------|
| `bundle` | object | Skill file bundle, key is relative path, value is file content |

**content_ref validity:** Default 900 seconds (15 minutes); after expiration returns `404 CONTEXT_REF_INVALID`. Refs are usable only by the same integration and user.

---

### GET /internal/agents/context/skills

Get the skill inventory (direct directory listing without semantic search).

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `external_subject` | string | Yes | User identifier |
| `scope` | string | No | `personal`, `team`, `all` (default) |
| `context_session_id` | string | No | Associated Context Session ID |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `skills` | array | Skill list |
| `skills[].qualified_skill_id` | string | Qualified ID (`team:<name>`/`personal:<name>`) |
| `skills[].name` | string | Skill name |
| `skills[].scope` | string | Owning scope |
| `skills[].context_ref` | string | Opaque reference usable for read |
| `snapshot_id` | string | Snapshot ID |
| `etag` | string | List ETag |

---

### POST /internal/agents/context/remember

Write personal Memory. Limited to `personal_memory` scope.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `external_subject` | string | Yes | User identifier |
| `content` | string | Yes | Memory content, maximum 128KB |
| `category` | string | No | Category, default `agent`, only alphanumeric, underscore, dot, hyphen allowed |
| `idempotency_key` | string | No | Idempotency key, defaults to content hash |
| `context_session_id` | string | No | Associated Context Session ID |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `remembered` | boolean | Whether write succeeded |
| `context_ref` | string | Reference to newly created Memory |
| `receipt` | object | Receipt information |

---

### POST /internal/agents/context/forget

Delete personal Memory. Limited to refs in the `personal_memory` scope.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_ref` | string | Yes | Memory reference to delete |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `forgotten` | boolean | Whether deletion succeeded |

---

### POST /internal/agents/context/sessions/start

Start a new Context Session for subsequent event appends and usage reporting on commit.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `external_subject` | string | Yes | User identifier |
| `external_session_id` | string | Yes | Agent-side Session ID (idempotency key) |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `context_session_id` | string | Context Session ID (`ctxs_<hash>`) |
| `created` | boolean | Whether newly created (false means already exists) |

Context Sessions are idempotent based on `agent_id + external_session_id`; duplicate starts return the same ID.

---

### POST /internal/agents/context/sessions/append

Append a message event to a Context Session. Events are appended in order by sequence number.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_session_id` | string | Yes | Context Session ID |
| `event_id` | string | Yes | Event unique ID (idempotency key) |
| `sequence` | integer | Yes | Event sequence number, must strictly increment (starting from 1) |
| `role` | string | Yes | Message role: `user`, `assistant`, `system`, `tool` |
| `content` | string | Yes | Message content, maximum 128KB |
| `created_at` | string | No | ISO8601 timestamp |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `appended` | boolean | Whether append succeeded |
| `duplicate` | boolean | Whether duplicate event (same event_id + same content) |
| `sequence` | integer | Event sequence number |

**Note:** sequence must be strictly and continuously incrementing; out-of-order returns a 409 error. Duplicate event_id with different content also returns 409. Already-committed sessions cannot be appended to.

---

### POST /internal/agents/context/sessions/commit

Commit a Context Session, report actually used context_refs, trigger OpenViking session commit.

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `context_session_id` | string | Yes | Context Session ID |
| `used_context_refs` | array[string] | No | Actually read/injected context_ref list, maximum 200 entries |
| `integration_id` | string | Yes | Declared identity owns the resource: must be a registered, `active` Agent of the current tenant |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `committed` | boolean | Whether commit succeeded |
| `duplicate` | boolean | Whether duplicate commit (already committed returns true) |
| `result_hash` | string | Commit result hash |
| `usage` | object | Usage reporting statistics |
| `usage.contexts` | integer | Number of Memory contexts reported |
| `usage.skills` | integer | Number of Skills reported |
| `usage.submitted` | integer | Number of usage records submitted this time |
| `usage.skipped` | integer | Number of records skipped during retry |

**Idempotency:** Repeated commit of an already committed session returns `duplicate: true` and does not re-report OpenViking usage.

## 3. Usage Examples

### Search Context and Read

```bash
# 1. Resolve context
curl -X POST "http://localhost:52010/internal/agents/context/resolve" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "external_subject": "user-001",
    "integration_id": "my-agent:prod",
    "query": "database connection pool configuration",
    "scopes": ["team_memory", "team_skills"],
    "max_items": 5
  }'

# 2. Read full content of one entry
curl -X POST "http://localhost:52010/internal/agents/context/read" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "integration_id": "my-agent:prod",
    "context_ref": "ctx_abc123...",
    "level": "full"
  }'
```


### Context Session Lifecycle Example

```bash
# 1. Start Session
CTX_SESS=$(curl -s -X POST "http://localhost:52010/internal/agents/context/sessions/start" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{"external_subject": "user-001", "integration_id": "my-agent:prod", "external_session_id": "sess-001"}' | jq -r '.context_session_id')

# 2. Append message
curl -X POST "http://localhost:52010/internal/agents/context/sessions/append" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d "{
    \"integration_id\": \"my-agent:prod\",
    \"context_session_id\": \"$CTX_SESS\",
    \"event_id\": \"evt-1\",
    \"sequence\": 1,
    \"role\": \"user\",
    \"content\": \"Help me look at this error\"
  }"

# 3. Commit Session (with actually used refs)
curl -X POST "http://localhost:52010/internal/agents/context/sessions/commit" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d "{
    \"integration_id\": \"my-agent:prod\",
    \"context_session_id\": \"$CTX_SESS\",
    \"used_context_refs\": [\"ctx_abc123...\", \"ctx_def456...\"]
  }"
```

### Calling with the Tenant Machine Credential (`tevt_`) and a Declared integration_id

With the tenant machine credential, every request must declare `integration_id`, as a Query parameter on GET routes and a Body field on POST routes:

```bash
# GET route: integration_id as a Query parameter
curl "http://localhost:52010/internal/agents/context/describe?external_subject=user-001&integration_id=my-agent:prod" \
  -H "Authorization: Bearer tevt_abcdef1234567890"

# POST route: integration_id as a Body field
curl -X POST "http://localhost:52010/internal/agents/context/resolve" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "external_subject": "user-001",
    "integration_id": "my-agent:prod",
    "query": "database connection pool configuration",
    "scopes": ["team_memory", "team_skills"],
    "max_items": 5
  }'
```

`my-agent:prod` must be a registered, `active` Agent of that tenant, otherwise `403 UNKNOWN_INTEGRATION_ID` or `403 INTEGRATION_DISABLED` is returned; omitting `integration_id` returns `400 INTEGRATION_ID_REQUIRED`.

## 4. Response Contract and Error Handling

### Error Codes

| HTTP Status | Error Message | Cause |
|------------|--------------|-------|
| 401 | `WORKSPACE_TOKEN_INVALID` | No valid tenant machine credential (`tevt_`) |
| 403 | `SUBJECT_NOT_MAPPED` | external_subject not mapped to teamEvolver user |
| 403 | `CONTEXT_SCOPE_FORBIDDEN` | Requested scope not within Agent authorization |
| 400 | `INTEGRATION_ID_REQUIRED` | integration_id not declared |
| 403 | `UNKNOWN_INTEGRATION_ID` | Declared integration_id is not a registered Agent of the current tenant |
| 403 | `INTEGRATION_DISABLED` | Declared integration_id is registered but its status is not active |
| 400 | `body must be an object` | Request body is not a JSON object |
| 400 | `invalid context query` | query empty or exceeds 8000 characters |
| 400 | `unsupported content level` | level is not l0/l1/l2/full |
| 400 | `invalid memory content` | remember content empty or exceeds 128KB |
| 400 | `external_session_id is required` | sessions/start missing external_session_id |
| 400 | `invalid context event` | sessions/append invalid parameters (role/event_id/content) |
| 400 | `used_context_refs must be a list` | commit used_context_refs not an array |
| 400 | `unsupported skill scope` | skills interface invalid scope parameter |
| 404 | `CONTEXT_REF_INVALID` | context_ref invalid, expired, or not belonging to this integration |
| 404 | `context session not found` | context_session_id does not exist |
| 409 | `context event sequence must be N, got M` | append sequence not continuous |
| 409 | `event id was reused with a different payload` | Same event_id but different content |
| 409 | `context session is already committed` | Appending events to already committed session |
| 409 | `used context reference is invalid for this session` | used_context_refs contains refs not belonging to this session |
| 413 | `context event is too large` | append content exceeds 128KB |
