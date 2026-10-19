# Agent Context API

Agent Context gives runtimes retrieval and reading of personal and team Memory, Skills and Resources, writes personal Memory, and writes Agent conversation events into OpenViking Sessions. All endpoints use a tenant key + `user_id` under the prefix `/internal/agents/context`.

Code entry point: `teamEvolver/context/api/__init__.py:create_router`; reference and snapshot storage: `teamEvolver/context/repository.py:ContextRepository`.

## Identity and Scopes

`teamEvolver/security/agent_principal.py:resolve_agent_principal` resolves each request to `(tenant_id, account_id, user_id)`:

- `Authorization: Bearer tevt_...` is required, otherwise `401 TENANT_TOKEN_REQUIRED`.
- `user_id` is a query parameter in GET requests and a body field in POST requests.
- The OpenViking Account comes from the tenant's effective configuration `sharing_viking_account`; when missing the response is `503 TENANT_ACCOUNT_REQUIRED`.

The six scopes and their root URIs are listed in [Storage and Directory Layout](../concepts/09-storage-layout.md#scopes):

| Scope | Operations |
| --- | --- |
| `personal_memory` | resolve, read, remember, forget |
| `team_memory` | resolve, read |
| `personal_skills` | resolve, read |
| `team_skills` | resolve, read |
| `personal_resources` | resolve, read |
| `team_resources` | resolve, read |

Requests to OpenViking use the tenant Account and the OpenViking User of the scope; the Root Key stays on the server.

## References and Snapshots

`resolve`, `skills` and `remember` issue a `context_ref` (like `ctx_...`) and a receipt per item. References are bound to `(tenant_id, user_id)`, expire after 15 minutes, and are revoked by `forget`. Each `resolve` and `skills` call creates a `snapshot_id` (`ctxsnap_...`) recording the returned items. When reporting Sessions, Agents cite these IDs in `context_usage` and the server validates the usage record against them; see [Session Ingestion](./03-session-ingest.md#context_usage).

## Request Limits

| Item | Limit |
| --- | --- |
| Request body | 256 KiB |
| `resolve.query` | 1–8000 characters |
| `resolve.max_items` | 1–50, default 12 |
| `resolve.max_chars` | 500–100000, default 16000 |
| `remember.content` | 128 KiB |
| `sessions/append.content` | 128 KiB |
| `read` content | 500 KiB; Skill bundles at most 100 files |
| `external_session_id` | 1000 characters |
| `event_id` | 200 characters |

## Endpoints

### GET /internal/agents/context/describe

Query: `user_id`. Returns the protocol version, subject, scopes and budgets:

```json
{
  "protocol_version": "2.0",
  "subject": {"tenant_id": "demo", "user_id": "alice"},
  "scopes": {"personal_memory": {"kind": "memory", "space": "personal", "operations": ["resolve", "read", "remember", "forget"]}},
  "budgets": {"max_items": 50, "max_chars": 100000, "max_skill_bytes": 500000}
}
```

### POST /internal/agents/context/resolve

| Field | Required | Description |
| --- | --- | --- |
| `user_id` | Yes | User ID |
| `query` | Yes | Retrieval text |
| `scopes` | No | List of scopes, default all six; an unknown scope returns `403 CONTEXT_SCOPE_FORBIDDEN` |
| `max_items` | No | Max number of items |
| `max_chars` | No | Total `l0` + `l1` character budget |
| `context_session_id` | No | Related Context Session |

The server queries OpenViking for each scope in parallel and merges round-robin by scope until the item or character budget is reached. Skill items are collapsed to the Skill root directory; a Skill with the same name and description in both personal and team spaces keeps only the more recently modified copy, with personal winning ties.

```json
{
  "schema_version": "teamevolver.context-result",
  "snapshot_id": "ctxsnap_...",
  "items": [{
    "context_ref": "ctx_...", "receipt": {}, "scope": "team_skills", "kind": "skill",
    "title": "cost-report", "l0": "...", "l1": "...", "skill_metadata": {"name": "cost-report", "description": "..."},
    "content_hash": "...", "selected": true, "path_alias": "team_skills:1a2b3c4d5e6f"
  }],
  "subject": {"tenant_id": "demo", "user_id": "alice"},
  "receipts": [],
  "warnings": [],
  "skills_etag": "...",
  "budget": {"max_items": 12, "max_chars": 16000, "used_items": 1, "used_chars": 820, "truncated": false}
}
```

The result schema is [agent-context-result-v2.schema.json](../../schemas/agent-context-result-v2.schema.json) and the request schema [agent-context-request-v2.schema.json](../../schemas/agent-context-request-v2.schema.json).

### POST /internal/agents/context/read

| Field | Required | Description |
| --- | --- | --- |
| `user_id` | Yes | User ID |
| `context_ref` | Yes | Reference from resolve / skills / remember |
| `level` | No | `l0`, `l1` (default), `l2`, `full` |

`l0` reads the abstract, `l1` the overview, `l2` and `full` the body. A Skill reference with `full` returns the whole bundle: `{"bundle": {"SKILL.md": "...", "scripts/run.py": "..."}}`. A missing, expired or revoked reference returns `404 CONTEXT_REF_INVALID`.

### GET /internal/agents/context/skills

Query: `user_id`, `scope` (`personal`, `team`, `all`, default `all`), `context_session_id`. Lists the Skills visible to the user with their references:

```json
{
  "skills": [{"name": "cost-report", "description": "...", "modified_at": "...", "scope": "team_skills",
              "context_ref": "ctx_...", "receipt": {}, "qualified_skill_id": "team:cost-report"}],
  "snapshot_id": "ctxsnap_...",
  "etag": "..."
}
```

The authoritative team Skill versions are in PostgreSQL and the full bundles come from [Skill Pull](./06-skill-sync.md); this endpoint lists the Skill directories in OpenViking for retrieval and referencing.

### POST /internal/agents/context/remember

| Field | Required | Description |
| --- | --- | --- |
| `user_id` | Yes | User ID |
| `content` | Yes | Memory body, ≤128 KiB |
| `idempotency_key` | No | Idempotency key; computed from content when absent |
| `context_session_id` | No | Related Context Session |

Writes `agent/<hash>.md` under `personal_memory`. The same idempotency key writes the same file. Returns `{"remembered": true, "context_ref": "...", "receipt": {...}}`.

### POST /internal/agents/context/forget

Body: `user_id`, `context_ref`. Only entries in `personal_memory` can be deleted; other scopes return `403 CONTEXT_SCOPE_FORBIDDEN`. The reference is revoked after deletion and the response is `{"forgotten": true}`.

### POST /internal/agents/context/sessions/start

Body: `user_id`, `external_session_id`. The same `external_session_id` of the same user maps to the same `context_session_id` (`ctxs_...`), and a corresponding Session is created in OpenViking. Returns `{"context_session_id": "ctxs_...", "created": true}`.

### POST /internal/agents/context/sessions/append

| Field | Required | Description |
| --- | --- | --- |
| `user_id` | Yes | User ID |
| `context_session_id` | Yes | ID returned by `sessions/start` |
| `event_id` | Yes | Event ID, ≤200 characters |
| `sequence` | Yes | Consecutive from 1 |
| `role` | Yes | `user`, `assistant`, `system`, `tool` |
| `content` | No | Message content, ≤128 KiB |
| `created_at` | No | Event time |

Replaying the same `event_id` with the same content returns `duplicate: true`; the same `event_id` with different content, a gap in `sequence`, or an already committed Session returns 409.

### POST /internal/agents/context/sessions/commit

Body: `user_id`, `context_session_id`, `used_context_refs` (list of reference IDs, optional). The server writes the used Context as one assistant message into the OpenViking Session, then commits it. Repeated commits return `{"committed": true, "duplicate": true}`.

## Errors

| Status | detail | Cause |
| --- | --- | --- |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | `user_id` missing or invalid |
| 400 | `invalid context query` | `query` empty or longer than 8000 characters |
| 400 | `invalid context budget` | `max_items` / `max_chars` not integers |
| 400 | `unsupported content level` / `unsupported skill scope` | Invalid parameter value |
| 400 | `invalid memory content` | `remember.content` empty or too large |
| 401 | `TENANT_TOKEN_REQUIRED` | No valid tenant key |
| 403 | `CONTEXT_SCOPE_FORBIDDEN` | Unknown scope, or forget on non-personal Memory |
| 404 | `CONTEXT_REF_INVALID` | Reference missing, expired or revoked |
| 409 | `context session not found` and similar | Context Session not owned by the user, event conflict or already committed |
| 413 | `Context body exceeds 256 KiB` and similar | Request or read content over the limit |
| 503 | `TENANT_ACCOUNT_REQUIRED` | Tenant has no OpenViking Account configured |
| 4xx/5xx | `OpenViking HTTP <code>` | OpenViking returned an error (a retrieval 404 counts as an empty result) |

## Example

```bash
curl -X POST http://localhost:52010/internal/agents/context/resolve \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "alice", "query": "how to build the monthly cost report", "scopes": ["team_skills", "personal_memory"], "max_items": 5}'
```

The snapshot schema is [agent-context-snapshot-v2.schema.json](../../schemas/agent-context-snapshot-v2.schema.json).
