# Session Ingestion

This top-level module owns both ways a Session enters teamEvolver:

- `push/`: an Agent submits a versioned Session to `POST /ingest_session`;
- `pull/`: a tenant imports from its bound upstream Adapter;
- `adapters/`: tenant source definitions and shared transports;
- `service.py`: shared deduplication, Evidence Classification, archive,
  queue, and Evolution-trigger behavior.

Both transports produce the standard Session dictionary before calling
`service.ingest()`. Authentication, subject mapping, upstream connection
details, and source conversion remain private to their transport.

The FastAPI shell uses one interface:

```python
from session_ingestion import register_routes

register_routes(owner, app, invalidate_cache=invalidate_cache)
```

Tenant bindings store only the Adapter filename, so this relocation does not
require a database migration.

## Scheduled pulls

Admins can configure one daily pull per tenant through
`PUT /api/datasource/schedule`. A schedule contains an IANA timezone, local
trigger time, and Session limit. At each trigger, the service computes the
previous calendar day in that timezone, converts the half-open
`[00:00, next 00:00)` window to UTC, and passes it to the tenant's bound
Adapter as `from_timestamp` and `to_timestamp`.

The imported Sessions use the normal ingestion queue with
`defer_evolution_trigger=true`, so the pull does not block on analysis.
`POST /api/datasource/schedule/run` starts the same previous-day job
immediately and returns `202 Accepted`. Current and previous run details are
available from `GET /api/datasource/schedule`; they include both the business
date/window and the actual system start/finish timestamps.

Schedules are tenant-scoped and require an enabled Adapter that supports both
timestamp filters. Manual and scheduled pulls share per-tenant exclusion and a
process-wide concurrency limit.
