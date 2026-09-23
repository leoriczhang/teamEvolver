# Successful experience synchronization API

`GET /api/experience-sync/status` is a read-only tenant-admin console endpoint. It uses the authenticated current tenant, accepts no tenant selector, and rejects ordinary users with 403.

The response includes `enabled`, `tenant_id`, `target_directory`, `account`, Unix-second `last_scan` / `last_full_scan`, `counts` (`pending`, `synced`, `retry`) and a safe `last_error` code. Timestamps are null before scanning. `synced` requires matching content readback and completed semantic/vector processing. Counts are persisted-state observations and may briefly differ from concurrent work.

`target_directory` reflects the normalized `experience_sync.target_directory` exactly; no tenant subdirectory is appended. Its default is `viking://resources/agent_knowledge_workspace/input/proven_experiences`.

When disabled, the endpoint returns zero counts without reading old state. Configuration or storage errors appear in `last_error`; zero counts before scanning do not prove the source is empty. It returns no document bodies, Session bodies, credentials or DSNs, and provides no delete operation. See the [operations guide](../guides/16-successful-experience-sync.md).


## POST /api/experience-sync/trigger

Tenant-admin console operation, with no request body. The authenticated current tenant and its effective sharing configuration determine the destination; body fields cannot override identity, destination or the enabled flag. The Experience Library page exposes **Sync to OV now** to administrators.

After persisting the intent, returns HTTP 202 with `tenant_id` and `manual: {request_id, state: "queued", requested_at}`. This acknowledges acceptance, not successful uploads. GET status adds a nullable `manual` object. States are `queued`, `running`, `completed`; `started_at` and `finished_at` use Unix seconds. `scan_complete` marks the end of discovery. Completion means this pass ended, and may leave retries or rejected source documents: inspect counts and `last_error`.

The worker performs a paginated full reconciliation using the existing digest comparison and readback. Unchanged documents are not rewritten; retry backoff is preserved and OV files are not deleted. Page filters do not narrow the pass. Requests use the existing tenant advisory lock and the same two-worker scheduler. Pending/running requests coalesce to the same ID when the lock is available, and durable progress survives restart.

Errors: 403 for non-admin/inactive tenant; 409 `SYNC_DISABLED` or `SYNC_ALREADY_RUNNING` (worker holds the lock); 429 `SYNC_BUSY` (two concurrent registrations or 32 pending wakeup tenants on this instance); 503 `SYNC_NOT_RUNNING`, `SYNC_STOPPING`, safe configuration error codes or `SYNC_STORAGE_FAILURE`. Configuration errors include `PG_REQUIRED`, `SHARING_DISABLED`, `OV_IDENTITY_OR_KEY_MISSING`, `INVALID_SYNC_CONFIG`, `INVALID_SYNC_TARGET_DIRECTORY`, and `INVALID_OV_ENDPOINT`.

Only exemplary records already in per-Skill experience JSON are eligible. The Experience Library list is sourced from Session analysis; listed entries outside this legacy JSON flow are not uploaded by this action.


## POST /api/experience-sync/import

Tenant-admin explicit historical import, with no request body. It shares authentication, enabled/config requirements, durable acceptance and concurrency limits with trigger. HTTP 202 returns `tenant_id` and `manual` with `operation="import_all"`; trigger uses `operation="sync"`. Repeated pending operations of the same kind coalesce. Another active operation kind returns 409 `SYNC_OTHER_OPERATION_RUNNING`; an actively held worker lock returns `SYNC_ALREADY_RUNNING`.

Imports exemplary lessons from aggregate JSON, all paginated Session index rows, archives and historical experience records, independent of UI filters or its 10,000-row limit. It accepts no source body/account overrides and makes no model calls. Automatic scanning remains aggregate-JSON only.

GET status adds `manual.import`: `phase` (`objects`, `index`, `prepare`, `complete`), database-time `until`, `processed_sources`, `eligible_records`, `prepared_documents`, `rejected_sources`, safe `last_error` and optional pagination cursors. No lesson bodies or credentials are returned. Import `complete` means documents have been prepared; assess overall `manual.state`, delivery counts, top-level errors and import rejection counts separately. See the operations guide for deduplication, budgets and concurrent source-update boundaries.


### Additive gap and index fields

| Field | Type | Meaning |
| --- | --- | --- |
| `index_pending` | integer | Written content whose semantic/vector processing is unconfirmed; a subset of pending/retry, not an additional document total |
| `manual.import.rejected_records` | integer | Array entries with gaps; valid siblings remain eligible |
| `manual.import.error_counts` | object | Safe error code → occurrence count, including source-level and record-level failures |
| `manual.import.errors_sample` | array | At most 20 summaries in discovery order; complete records persist in sync state |

Summaries include `source_key`, optional `session_id`, `code`, `source_bytes`, `revision`, `stage`, one-based `ordinal` (0 for source-level gaps), `record_bytes` and `limit_bytes`. Unavailable sizes are null. Older tasks may omit new fields; clients keep existing counters and default missing detail. `rejected_sources` counts sources with at least one gap, which may also contribute valid lessons. Completed runs with gaps are partial completion; the state enum is unchanged.

Optional YAML `experience_sync.import_max_source_mb` defaults to 64 (integer 1–1,024); it is not a request parameter. Session/history projection is bounded to 100 records/1 MiB per page and 256 KiB per record. Private pages cannot enter delivery before source version confirmation. Implementation: `teamEvolver/storage/experience_import.py`; see the [operations guide](../guides/16-successful-experience-sync.md) for budgets and error recovery.


### Retry execution diagnostics

Status adds `server_time`, `last_error_at`, `retry` and `last_delivery_pass`. `retry` contains due/deferred counts, earliest eligible `next_attempt_at`, latest `last_attempt_at`, error counts and at most five samples. Samples expose URI, source/experience identifiers, attempt counts, timestamps and safe `failure` metadata (stage, method, path, HTTP status and OV request ID), never bodies or credentials. Old records may have null failure details.

The durable `last_delivery_pass` summarizes the most recent page: start/finish times, examined/attempted/deferred counts and that page's earliest retry. It does not represent the entire corpus. `worker` describes the responding instance's running/idle/blocked/lock_busy/waiting/stopped state, PID, scheduler status and safe errors. In multi-instance deployments, an idle local worker does not imply all workers are idle. Admin-only access is unchanged.

Retry timestamps are eligibility times, subject to scheduling, paging and tenant locks. Re-importing unchanged content preserves backoff. After write timeouts, TE attempts readback of the same URI; matching content confirms persistence only, leaving semantic/vector status unknown and delivery unsynced.
