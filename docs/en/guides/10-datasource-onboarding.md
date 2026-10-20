# Data Source Onboarding Guide

A data source adapter converts Agent sessions in an upstream system (Doris, Langfuse and others) into standard Sessions that the Worker pulls, analyzes and evolves from. An adapter is a Python file whose source is stored in PostgreSQL as immutable versions; a tenant binds to one fixed revision. API and field constraints are in [Session Ingest API](../api/03-session-ingest.md#data-source-adapters).

## Components

| Location | Content |
|------|------|
| `session_ingestion/adapters/*.py` | Per-tenant adapter templates, each defining `SOURCE` and `build_adapter()` |
| `session_ingestion/adapters/sources/` | Shared transports: `doris.py` (`DorisSourceAdapter`), `langfuse.py` (`LangfuseSource`) |
| `session_ingestion/adapters/_shared/` | Field mapping, Langfuse client and conversion, builder functions |
| PostgreSQL `adapter_versions` | Saved adapter source; each save creates a new revision |
| PostgreSQL `adapter_bindings` | Tenant binding; `binding_key='datasource'` points to `(adapter_id, revision)` |

Templates in the repository take no effect until saved to PostgreSQL and bound. A running pull task keeps the revision it was accepted with; saving a new version does not affect in-flight tasks.

## Adapter File Conventions

See `session_ingestion/adapters/example-doris.py`; configure the upstream and change `enabled` to true before use:

```python
SOURCE = {
    "label": "Example Doris", "provider": "doris",
    "host": "doris.example.com", "project_id": "example-project",
    "enabled": False, "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    from session_ingestion.adapters._shared.builders import build_doris_adapter
    return build_doris_adapter(SOURCE, "agent-call")
```

Validation rules (`session_ingestion/adapters/_runtime.py:validate_content`):

- The file name ends with `.py`, does not start with `_` and has no path; source is at most 256 KiB
- `build_adapter()` and a literal dictionary `SOURCE` must be defined at top level
- `required_filters` must be a subset of `supported_filters`; `max_sessions` is 1–1000
- `exclude_session_id_patterns` is a case-sensitive glob list that drops noisy Sessions before fetching details; it does not apply when `session_id` is given explicitly

The object returned by `build_adapter()` provides `health()`, `iter_session_id_batches(filters, batch_size)`, `fetch_session(session_id)`, `convert_session(session, traces)` and `close()`. The Worker handles at most 1000 Session IDs per batch and fetches and converts with 8-way concurrency; a single failure is recorded as an error without stopping the batch.

## Credentials

Upstream credentials are read from the Worker process environment, never from adapter source or `SOURCE`:

| Upstream | Environment variables |
|------|----------|
| Doris | `DORIS_HOSTS`, `DORIS_PORT`, `DORIS_USER`, `DORIS_PASSWORD`, `DORIS_DB`; table names: `DORIS_TRACES_TABLE`, `DORIS_OBSERVATIONS_TABLE`, `DORIS_MAPPING_TABLE`; optional `DORIS_PROXY_URL`, `DORIS_FORCE_PROXY`, `DORIS_CA_BUNDLE` |
| Langfuse | `LANGFUSE_PULL_HOST`, `LANGFUSE_PULL_PUBLIC_KEY`, `LANGFUSE_PULL_SECRET_KEY` |

Restart the Worker after changing environment variables. Outbound trace observability uses the separate `langfuse_tracing_*` settings and has nothing to do with data source credentials; see the [Trace Observability Guide](./04-observability.md).
Direct Doris access requires the optional `pymysql` driver. The HTTP query proxy is available only with an explicit `DORIS_PROXY_URL`; TLS is verified by default, with an optional certificate in `DORIS_CA_BUNDLE`.

## Onboarding Steps

### 1. Save an Adapter Version

Saving and reading source requires the Root Key. The following saves the repository template as the first version for tenant `demo`:

```bash
export TEAMEVOLVER_ROOT_API_KEY=...
jq -Rs '{file: "example-doris.py", code: ., expected_revision: 0}' \
  session_ingestion/adapters/example-doris.py |
curl --fail -X PUT http://127.0.0.1:52010/api/datasource/code \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" -H 'X-Tenant-Id: demo' \
  -H 'Content-Type: application/json' -d @-
```

`expected_revision` must equal the current latest revision (0 the first time), otherwise 409 is returned. When the content equals the latest version, the existing revision is returned and no new version is created.

Before saving, `POST /api/datasource/code/test` runs `validate`, `health`, `preview` or `session` against a draft; `validate` returns synchronously and the other modes run as background tasks.

### 2. Bind

```bash
teamEvolver datasource --tenant demo bind example-doris.py
```

Binding takes the latest revision by default; `PUT /api/datasource` with `{"file", "revision"}` binds a specific version, and an empty `file` unbinds. Tenant administrators can also choose and bind a file in the console "Data Source Onboarding" page.

### 3. Verify

```bash
teamEvolver datasource --tenant demo test
teamEvolver datasource --tenant demo list --from-timestamp 2026-10-01T00:00:00+08:00 \
  --to-timestamp 2026-10-01T02:00:00+08:00 --max-sessions 20
```

Both commands return a `job_id`; read results with `GET /api/datasource/jobs/{job_id}/result`. Preview a narrow time window first and check Session grouping, users, tool calls and Skills used.

### 4. Pull

```bash
teamEvolver datasource --tenant demo pull \
  --from-timestamp 2026-10-01T00:00:00+08:00 --to-timestamp 2026-10-02T00:00:00+08:00
```

A pull walks the whole filter window, accepts Sessions one by one and enqueues `session.normalize`. Analysis, archiving and evolution afterwards are described in [Sessions](../concepts/05-sessions.md).

### 5. Scheduled Pull

```json
{"enabled": true, "time": "02:00", "timezone": "Asia/Shanghai", "window": "previous_day"}
```

Save it with `PUT /api/datasource/schedule` or in the console. The Worker checks once per tenant refresh round: after local time reaches `time`, it accepts one pull for the previous calendar day, at most once per window. `POST /api/datasource/schedule/run` pulls that window immediately.

## Console

The "Data Source Onboarding" page (tenant administrators) provides binding, connection test, preview, pull and schedule settings. Editing and uploading source requires a Root Key identity; with Cookie sign-in, `GET /api/datasource` returns `source_editable=false` and the page shows saved versions only.

## Common Errors

| Status | Cause |
|------|------|
| 400 | Adapter validation failed, unsupported filter, missing required filter, `max_sessions` out of range |
| 403 | Reading or saving source without the Root Key |
| 404 | The bound revision does not exist |
| 409 | Testing or pulling with no adapter bound; `expected_revision` is stale on save |

## Related Documents

- [Session Ingest API](../api/03-session-ingest.md)
- [Sessions](../concepts/05-sessions.md)
- [Operations CLI](./05-daemon-cli.md)
