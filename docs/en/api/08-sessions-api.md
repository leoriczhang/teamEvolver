# Session Query API

Queries formal Sessions in PostgreSQL (the `sessions` table). A formal Session is written only after the normalize, analyze and archive stages all succeed, and its `decision` is `valuable` or `skipped`. Each record carries both the business time `business_time` (from the report or upstream) and the ingest time `ingested_at`.

Code entry points: `session_ingestion/api/console.py:create_router`, `session_ingestion/queries.py:SessionQueries`.

All endpoints require a tenant administrator (console login or Root Key).

## GET /api/sessions

Keyset-paged list in descending `business_time` order.

| Parameter | Description |
| --- | --- |
| `limit` | Default 50, max 200 |
| `before` | `business_time` of the last record of the previous page; returns earlier records |

```json
{"sessions": [{"session_id": "session_...", "source": "push", "external_id": "task-001", "user_id": "alice",
               "decision": "valuable", "judge": {}, "business_time": "...", "ingested_at": "..."}],
 "count": 1}
```

## GET /api/sessions/page

Console list in descending `ingested_at` order, with filters.

| Parameter | Description |
| --- | --- |
| `limit` | 1–200, default 20 |
| `offset` | ≥0 |
| `search` | Matches `session_id` or metadata |
| `decision` | `valuable` / `skipped` |
| `skill` | Matches Skill names in metadata |
| `start` / `end` | `business_time` range (ISO 8601) |

```json
{"sessions": [{"session_id": "...", "user_id": "alice", "source": "push", "decision": "valuable",
               "judge": {}, "meta": {}, "title": "...", "user_alias": "alice", "status": "archived",
               "timestamp": "...", "business_time": "...", "ingested_at": "..."}],
 "total": 120, "limit": 20, "offset": 0, "has_more": true}
```

## GET /api/sessions/analysis

Counts by `decision`:

```json
{"stats": {"total": 120, "decisions": {"valuable": 80, "skipped": 40}, "modes": {"model": 120}}}
```

## GET /api/sessions/export

| Parameter | Description |
| --- | --- |
| `format` | `json` (default) or `csv` |
| `ids` | Comma-separated `session_id` values; empty exports the filtered result |
| `search` / `decision` | Same as `/api/sessions/page` |

At most 200 records per export; the `X-Export-Count` response header holds the actual count. CSV columns are `session_id, user_id, decision, business_time, ingested_at`.

## GET /api/sessions/{session_id}

Metadata of one formal Session, including `ingestion_id`, `account_id`, `content_ref`, `content_hash`, `judge`, `filter_audit`, `consumption`, `meta`, `business_time`, `ingested_at`. Returns 404 when absent.

## GET /api/sessions/{session_id}/content

Reads the full archived Session content (the immutable payload referenced by `content_ref`), plus `meta`, `judge`, `summary` (`title`, `user_alias`, `status`, `num_turns`) and both time fields.

## GET /api/sessions/{session_id}/stages

Results of each processing stage:

```json
{"stages": [
  {"stage": "normalize", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "analyze", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "trace_analyze", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "archive", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""}
]}
```

## Evolution History

`GET /history?session_id=&limit=20` returns Skill evolution cycle records `{"reachable": true, "cycles": [...]}`, optionally filtered by Session. It accepts console login, the Root Key and tenant keys. Code entry point: `team_skills/api/candidates.py:create_router`.

## Errors

| Status | Cause |
| --- | --- |
| 400 | `limit` / `offset` out of range, or `format` is not `json` / `csv` |
| 403 | Not a tenant administrator |
| 404 | `Session not found` |
