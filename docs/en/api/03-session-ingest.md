# Session Ingestion and Data Source API

Sessions enter in two ways: Agent push and tenant data source pull. Both share one accept path: the API checks identity and protocol, writes the `ingestions` record, the raw payload and `worker_tasks` in one short transaction, and returns `202`. The Worker runs `session.normalize → session.analyze → session.archive`; a pull first runs `session.pull` and then derives a normalize task for each Session it fetched.

Code entry points:

- Acceptance: `session_ingestion/api/__init__.py:create_router`
- Protocol validation: `session_ingestion/push/protocol.py:normalize_session_envelope`
- Data source control plane: `session_ingestion/api/datasource.py:create_router`
- Worker stages: `session_ingestion/handlers/__init__.py`
- Scheduled pulls: `session_ingestion/schedule.py:enqueue_due`

## Agent Push

```
POST /ingest_session
Authorization: Bearer tevt_...
Content-Type: application/json
```

### Request Body

The envelope is `teamevolver.agent-session.v2`; the schema is [agent-session-v2.schema.json](../../schemas/agent-session-v2.schema.json).

| Field | Type | Required | Description |
| --- | --- | --- | --- |
| `schema_version` | string | Yes | Always `teamevolver.agent-session.v2` |
| `protocol_version` | string | Yes | Always `2.0` |
| `session_id` | string | Yes | Caller's Session ID |
| `runtime.type` | string | Yes | Runtime label such as `custom-agent`, `pi`; not used for authentication |
| `runtime.version` | string | No | Runtime version |
| `runtime_context.user_id` | string | Yes | User ID within the tenant; rules in [Protocol v2](../agent-integrations/06-protocol-v2.md#identity) |
| `turns` | array | Yes | Non-empty array of objects |
| `turns[].turn_num` | integer | No | Turn number |
| `turns[].prompt_text` / `response_text` | string | No | User input and Agent answer |
| `turns[].messages` | array | No | Full message list |
| `turns[].tool_calls` / `tool_results` | array | No | Tool calls and results |
| `turns[].injected_skills` / `used_skills` / `modified_skills` | array | No | Skill usage |
| `turns[].metrics` | object | No | Metrics observed in this turn |
| `turns[].context_usage` | object | No | Context references used in this turn, see below |
| `metrics` | object | No | Session-level metrics |
| `title`, `system_prompt` | string | No | Session title and system prompt |
| `source_materials` | array | No | Initial task materials |
| `business_time` | string | No | Business time (ISO 8601) |
| `meta` | object | No | Extra metadata |

`runtime.integration_id` and `runtime_context.external_subject` belong to the retired Agent registration protocol and return 400 when present. The server determines the tenant from the tenant key and the OpenViking Account from the tenant's effective configuration; clients cannot override either.

### context_usage

```json
{
  "context_snapshot_id": "ctxsnap_...",
  "memory_refs": [{"context_ref": "ctxref_...", "operation": "injected"}],
  "skill_refs": [{"context_ref": "ctxref_...", "operation": "read"}],
  "feedback": {}
}
```

At acceptance `teamEvolver/context/usage.py:verify_usage` checks that snapshots and references belong to the current `(tenant_id, user_id)` and are not revoked, that reference kinds match their field, that `operation` is one of `retrieved`, `injected`, `read`, `selected`, and that there are at most 200 per kind. When a snapshot is given, references must belong to it.

### Response

```json
{
  "accepted": true,
  "ingestion_id": "3f0c...",
  "job_id": "3f0c...:session.normalize",
  "status": "accepted",
  "status_url": "/api/jobs/3f0c...:session.normalize",
  "dedupe": false
}
```

Pushing the same `session_id` again within a tenant returns the first `ingestion_id`, `job_id` and current status with `dedupe` set to `true`.

`job_id` refers only to the first stage. The final Session state is visible in the console, or via `/api/sessions/{session_id}/stages` in [Session Queries](./08-sessions-api.md).

### Processing Stages

| Stage | Worker kind | Content |
| --- | --- | --- |
| normalize | `session.normalize` | Normalize the envelope and write the stage result |
| analyze | `session.analyze` | Two model stages, Session Analyze (value classification) and Trace Analyze; either failing retries |
| archive | `session.archive` | Writes the formal Session (business time + ingest time) after all required stages succeed |

Sessions classified `valuable` become evolution input; `skipped` Sessions are also kept for audit. A failed model call never produces a formal Session.

### Errors

| Status | detail | Cause |
| --- | --- | --- |
| 400 | `unsupported session schema` | `schema_version` is not v2 |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED` | `protocol_version` is not `2.0` |
| 400 | `retired Agent identity fields are not accepted` | Contains `integration_id` or `external_subject` |
| 400 | `V2 session runtime.type is required` | Missing `runtime.type` |
| 400 | `V2 session session_id is required` | Missing `session_id` |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | `user_id` missing or invalid |
| 400 | `V2 session turns must be a non-empty list of objects` | `turns` empty or elements are not objects |
| 400 | `invalid or expired context reference` and similar | `context_usage` validation failed |
| 400 | `session body must be valid JSON` / `session body must be an object` | Malformed body |
| 401 | `TENANT_TOKEN_REQUIRED` | No valid tenant key |
| 413 | `session body exceeds <n> bytes` | Exceeds `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` (default 32 MiB) |
| 503 | `TENANT_ACCOUNT_REQUIRED` | Tenant has no OpenViking Account configured |

### Example

```bash
curl -X POST http://localhost:52010/ingest_session \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "schema_version": "teamevolver.agent-session.v2",
    "protocol_version": "2.0",
    "session_id": "task-001",
    "runtime": {"type": "my-agent"},
    "runtime_context": {"user_id": "alice"},
    "turns": [{"turn_num": 1, "prompt_text": "Analyze cost", "response_text": "Cost report done."}]
  }'
```

## Data Source Adapters

A data source Adapter is a tenant-supplied Python file that connects to the upstream (Doris, Langfuse and others), filters records and converts them into standard Sessions. Adapter source is versioned in PostgreSQL `adapter_versions` and the tenant binding is in `adapter_bindings`; running tasks pin the revision current at acceptance. Files under `session_ingestion/adapters/` in the repository are bundled templates and must be written to PG with the Root Key before they can be bound.

Adapter file requirements (`session_ingestion/adapters/_runtime.py:validate_content`):

- The file name ends with `.py`, does not start with `_`, and the content is at most 256 KiB.
- It defines `build_adapter()` at the top level.
- Optional literal dict `SOURCE`: `enabled` (bool), `supported_filters` / `required_filters` (string lists; required must be contained in supported), `exclude_session_id_patterns`, `max_sessions` (1–1000, default preview count).
- Credentials are read from environment variables, never written in source.

### Endpoints

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/api/datasource` | Tenant administrator | Current binding, available Adapters, schedule and last pull status |
| PUT | `/api/datasource` | Tenant administrator | `{"file": "x.py", "revision": 3}` binds; an empty `file` unbinds; omitting revision takes the latest |
| GET | `/api/datasource/code?file=x.py` | Root Key | Read latest source and revision |
| PUT | `/api/datasource/code` | Root Key | `{"file", "code", "expected_revision"}` saves a new version; a revision mismatch returns 409 |
| POST | `/api/datasource/code/test` | Root Key | `{"file", "code", "mode", ...}` tests an unsaved draft; `mode` is `validate`, `health`, `preview` or `session` |
| POST | `/api/datasource/test` | Tenant administrator | Connectivity check of the bound Adapter, returns 202 |
| POST | `/api/datasource/sessions` | Tenant administrator | `{"filters": {...}, "max_sessions": 100}` previews the Session list, returns 202 |
| POST | `/api/datasource/pull` | Tenant administrator or tenant key | `{"from_timestamp", "to_timestamp", "filters"}` pulls and ingests, returns 202 |
| GET | `/api/datasource/jobs/{job_id}/result` | Tenant administrator | Result of test, preview and pull tasks |
| GET | `/api/datasource/schedule` | Tenant administrator | Same as `GET /api/datasource` |
| PUT | `/api/datasource/schedule` | Tenant administrator | Save the scheduled pull configuration |
| POST | `/api/datasource/schedule/run` | Tenant administrator | Pull once now using the scheduled window (previous calendar day) |

`validate` mode returns `{"ok": true, "metadata": {...}}` synchronously; other tests, previews and pulls run as `session.pull` tasks on the Worker. `max_sessions` ranges 1–1000. A pull walks the full filter window and is not limited by `max_sessions`.

### Scheduled Pull

```json
{"enabled": true, "time": "02:00", "timezone": "Asia/Shanghai", "window": "previous_day"}
```

All four fields are required and `window` only supports `previous_day`. An Adapter must be bound before enabling. The configuration is stored as `datasource_schedule` in the tenant `session` module configuration. The Worker calls `enqueue_due` on each tenant refresh: once local time reaches `time`, it accepts one pull for the previous calendar day; the window is deduplicated by `dedupe_key`, so multiple Worker replicas never accept it twice.

### CLI

`teamEvolver datasource` calls these endpoints with the Root Key and needs `TEAMEVOLVER_ROOT_API_KEY`:

```bash
teamEvolver datasource --tenant demo status
teamEvolver datasource --tenant demo bind example-doris.py
teamEvolver datasource --tenant demo test
teamEvolver datasource --tenant demo list --from-timestamp 2026-10-01T00:00:00+08:00 --to-timestamp 2026-10-02T00:00:00+08:00 --max-sessions 20
teamEvolver datasource --tenant demo pull --from-timestamp 2026-10-01T00:00:00+08:00 --to-timestamp 2026-10-02T00:00:00+08:00
```

`--url` sets the service address, default `http://127.0.0.1:<api_port>`.

Step-by-step instructions are in [Data Source Onboarding](../guides/10-datasource-onboarding.md).
