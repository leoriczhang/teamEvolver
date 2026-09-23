# Successful experience synchronization

TE asynchronously projects legacy per-Skill experience JSON from PostgreSQL into OpenViking. PostgreSQL remains authoritative; OV outages do not block experience persistence. This feature does not invoke models, publish Skills, or build/publish Ontology assets.

## Scope and configuration

Automatic synchronization and **Sync to OV now** only scan `experience_library/{skill_slug}.json`, including existing storage prefixes. Session documents, hidden markers and non-`exemplary` entries are excluded. **The current Judge/Session pipeline does not produce these legacy aggregate documents.** Automatic synchronization does not restore that writer. The new explicit **Import historical successful experiences** action also reads Session-derived history without changing Judge writes.

```yaml
experience_sync:
  enabled: true
  target_directory: viking://resources/agent_knowledge_workspace/input/proven_experiences
  interval_seconds: 30
  batch_size: 100
  full_scan_interval_seconds: 86400
  import_max_source_mb: 64
```

SIT enables the feature; code, local and PRD defaults disable it. Apply the actual mounted YAML and restart TE. No new credentials or environment variables are required. Enable PostgreSQL storage and configure the tenant's existing `sharing` connection. The backend uses `viking_team_api_key`, falling back to `viking_api_key`, with the effective account and user.

State uses the configured schema's existing `objects` table under `successful_experience_sync/v1/`, with tenant RLS. No new business tables are required. There are at most two concurrent passes; the PG pool needs at least four connections for locks and queries.

## Documents and incremental updates

Each successful experience becomes one JSON file at `{target_directory}/{source_key_hash}/{experience_id}.json`. The default directory is `viking://resources/agent_knowledge_workspace/input/proven_experiences`. Configure another concrete resources directory with `experience_sync.target_directory`; trailing slashes are normalized. Scope roots, private user paths, traversal, empty interior segments, query strings, fragments and percent-encoded paths are rejected. Unsafe experience identifiers are hashed. Stable business fields determine a canonical SHA-256 digest; descriptions normalize line endings and surrounding whitespace. Counts, user/Session lists, scores and timestamps are excluded. New lessons create files; changed descriptions replace the same URI. Source deletion or retention trimming does not delete remote files.

History is backfilled on first enablement. Timestamp/key pagination with a 120-second overlap reduces scanning, while daily full reconciliation handles late commits and verifies remote content. Matching daily readbacks do not rewrite files. Work is persisted before advancing the cursor. Writes use native mkdir/content-write/download APIs, never append. A successful state requires readback digest equality and explicit completion of both semantic and vector processing.

HTTP requests time out after 30 seconds. Durable exponential retry starts at five seconds, capped at 15 minutes and subject to scheduler cadence. Restart resumes unfinished work. Endpoint/account/user/target_directory changes create a new target checkpoint and backfill current source documents; old remote files remain. Trimmed source history is not migrated to a new target. This is eventual consistency, not a cross-service transaction.

Automatic aggregate-JSON sources over 1 MiB or 1,000 entries, or malformed documents, are reported and skipped until corrected. Explicit Session/history import uses the separate budgets below. The TE tenant subdirectory is no longer appended; identical account, directory, source key and experience ID target the same file. OV Account/ACL remains the access boundary. Resource files are not published Ontology facts.

## Operations

Use the tenant-admin [status endpoint](../api/16-experience-sync.md). Logs use `experience_sync.started`, `delivery`, `source_rejected`, `blocked`, `recovered` and `stopped` events with tenant, source key, experience ID, digest and URI; document bodies and credentials are omitted. Check `PG_REQUIRED`, `SHARING_DISABLED`, `OV_IDENTITY_OR_KEY_MISSING`, authentication errors, network errors and `OV_INDEX_NOT_READY` separately.

Disabling the setting and restarting stops uploads while retaining both PG state and OV files. Deployment and PRD enablement are separate operator actions.

## Local validation, 2026-09-21

Baseline `48150e8` plus the uncommitted implementation; Python 3.12.9 and isolated PostgreSQL 17.11 with a non-superuser, non-BYPASSRLS role. All 41 new tests passed, including five real PG tests for pagination, tenant RLS, replica locks, lock loss, CAS and late commits, plus configurable-directory routing, target switching and URI validation. A real TE process was started and restarted against isolated PG and an OV HTTP contract fixture; backfill, count-only suppression, fixed-URI updates, incomplete-index retry and restart deduplication passed.

Compilation, changed-file Ruff and documentation references passed. Full pytest: 693 passed, 47 skipped, three existing failures: Dataset Collection route history, SF Agent envelope-only title extraction, and Memory personal-source configuration merging. No frontend changes were made.

Actual OV retrieval, SIT/PRD deployment and scale measurements were not executed. Fixture index statuses do not establish real OV retrieval quality. PG tests require an explicit disposable `TE_EXPERIENCE_TEST_PG_DSN`; each test creates/removes its own schema, and skips when that variable is absent.


## Manual run from Experience Library (2026-09-22)

Sign in as an administrator, select the tenant and open Experience Library. The **Successful experience sync to OV** panel displays the effective destination and status. With PG, sharing and `experience_sync.enabled: true` configured, click **Sync to OV now**. This queues a paginated full reconciliation for the current tenant, independent of list filters; writes remain incremental by content digest.

Status refreshes every five seconds. Acceptance is not upload success; a completed pass can still have pending/retry items or rejected sources. Check counts and the last error. Unchanged content, including statistics-only changes, is not rewritten; retry backoff is preserved. Busy responses mean an existing worker holds the lock. Progress survives TE restart in the existing objects table, with no migration or new configuration. OV documents are not deleted and Ontology is unaffected.

The list uses Session analysis, whereas automatic synchronization only reads the legacy per-Skill JSON. The current Judge does not automatically populate that JSON flow; **Sync to OV now** does not bridge that gap; use the historical-import action instead. Ordinary users cannot invoke the operation. See the [API reference](../api/16-experience-sync.md).


### Local verification

2026-09-22, baseline `e4704c7` plus this working-tree change; Python 3.12.9, isolated non-superuser PostgreSQL 17.11 and OV HTTP test doubles. Sync-specific tests: 48 passed. Full Python suite: 749 passed, 48 skipped, three pre-existing failures (`test_routes_accept_batch_and_preserve_historical_source_after_removal`, `test_envelope_only_prompt_title_comes_from_nested_query`, `test_agentshub_config_sync_merges_personal_sources`). Frontend: 71 passed; production build passed with the existing chunk-size warning. Python compilation, changed-file Ruff, documentation references, local foreground startup and daemon restart checks passed.

No SIT/PRD deployment or real OV upload was triggered in this change. Verify destination content and index processing after deployment.


## Import historical successful experiences

Administrators can select **Import historical successful experiences** in the Experience Library sync panel, calling `POST /api/experience-sync/import`. It includes legacy aggregate JSON plus the tenant's Session index, archives, queued Sessions, historical `experience_library/sessions/` records and `skill_evidence/`. Only existing exemplary lessons are extracted; no model calls, local file uploads or new Session judgments are involved.

A durable paginated worker scans history, scans the Session index without the UI's 10,000-record cap, prepares deduplicated documents, reconciles aggregate JSON and delivers through the existing OV writer. It shares the sync lock, concurrency and retry limits and resumes after restart. An active operation of the other kind returns 409.

Imported history groups by Skill, exemplary kind and experience key, with stable IDs. Latest observed timestamp (or ingestion timestamp), then Session ID selects the description; ties prefer the index, then stable source/digest ordering. Legacy aggregate documents preserve their existing source identities and IDs rather than being overwritten by this grouping. Statistics and trajectories are not exported. Repeated imports do not rewrite unchanged content; changed text replaces the fixed URI, and source removal does not delete OV files.

The logical source key `experience_library/session-derived-{skill-hash}.json` is not a newly created aggregate source file. Intermediate groups live under the private sync state prefix. New Session lessons and destination changes require another explicit import; automatic scanning still covers aggregate JSON only.

Progress reports sources scanned, exemplary occurrences found, deduplicated documents prepared and sources with gaps. Prepared counts do not mean new writes or completed indexes. Metadata discovery returns only keys, sizes, timestamps and row versions. Dedicated PG queries parse one source and project lesson fields plus merge identity/time fields; full trajectories never reach TE.

`experience_sync.import_max_source_mb` defaults to 64 MiB and accepts integers 1–1,024. It does not change general object-read limits. Pages contain at most 100 records and 1 MiB of projected data; individual projections are limited to 256 KiB. Historical arrays no longer have a 1,000-entry total cap. Queries use at most 30 seconds, honoring shorter existing PG timeouts. Pagination reparses the source in PG, so larger source budgets require resource assessment and do not imply throughput guarantees.

Source failures do not stop other sources; oversized individual records do not discard valid siblings. Import status adds `error_counts` and up to 20 `errors_sample` summaries. Complete error records remain under private sync state `import-errors/{request_id}/`. Logs include source identity, version, stage and available sizes/budgets, never lesson or trajectory bodies. Completed runs with gaps display partial completion, separately from upload retries and written-but-unconfirmed indexes. Older progress is retained without resetting counters.

| Code | Meaning and recovery |
| --- | --- |
| `IMPORT_SOURCE_TOO_LARGE` | Raw source exceeds configured budget; inspect source_bytes/limit_bytes and split or reassess resources |
| `IMPORT_RECORD_TOO_LARGE` | Individual projection exceeds 256 KiB; inspect ordinal/record_bytes and repair that record |
| `INVALID_IMPORT_SOURCE` / `INVALID_IMPORT_RECORD` | Invalid JSON or lesson structure; repair the indicated source/array entry |
| `IMPORT_QUERY_TIMEOUT` | Parsing or version confirmation timed out; inspect PG load and effective timeout |
| `IMPORT_SOURCE_CHANGED` | Row changed during pagination; unconfirmed pages do not merge; repeat import for the new version |

Each source persists its version, parse phase and array cursor. Pages are private until the complete source version is confirmed, then merge. Restarts resume checkpoints; changed-source staging is cleaned without blocking other confirmed sources. Lesson bodies are not additionally truncated and no model is used.

The request records a database-time upper bound, not a cross-source immutable snapshot. Concurrent updates or late commits may require another import. Inspect delivery counts, import gaps and errors before declaring completion.


### Historical-import local validation (2026-09-22)

Baseline `5a954c8` plus this working-tree change, Python 3.12.9, isolated non-superuser PostgreSQL 17.11 and OV HTTP fixtures. Sync/import suite: 58 passed, including pagination across 10,005 Sessions, tenant isolation, recovery, unchanged-content suppression, fixed-URI updates and source-gap reporting. Full Python: 759 passed, 48 skipped, the same three pre-existing failures listed above. Frontend: 72 passed; production build, compilation, changed-file Ruff, documentation references and local foreground/daemon startup-restart checks passed. The added malformed Judge-field rejection case also passed.

No SIT/PRD deployment or actual historical import was performed. The large pagination case proves traversal completeness, not a throughput target or real OV retrieval quality.


### Upgrade and OV conflict recovery

This TE-only fix changes neither OV nor business tables or document identities. Deploy and restart TE; existing delivery records (including the 73 from the reported incident) keep their URIs, digests and retry schedule. Do not clear sync state. After the old import finishes, start another historical import to revisit sources skipped by the former cap. Unchanged successful records and statistics-only changes do not cause writes.

On mkdir `409 CONFLICT` or `ALREADY_EXISTS`, TE calls native `/api/v1/fs/stat` with the same identity. Only the exact URI with `isDir=true` confirms an existing directory. The directory cache lasts one worker pass. A file at that path produces `OV_PATH_TYPE_CONFLICT`; stat 403/404/timeouts remain failures, and malformed responses produce `OV_INVALID_STAT`. A create-file 409 similarly requires confirmation of a regular file before one replacement retry.

`experience_sync.ov_request` logs mkdir/stat/write/readback stage, method/path, URI, latency, status and OV request ID (successes at DEBUG, failures at WARNING). `experience_sync.index_pending` indicates incomplete semantic/vector processing. Readback equality and both complete statuses remain required. Real destination content and retrieval must be verified after deployment; local HTTP fixtures are not real OV acceptance evidence.


### Large-source and conflict fix validation (2026-09-22)

Baseline `a789d9c` plus this working-tree change; Python 3.12.9, isolated non-superuser PostgreSQL 17.11 and OV HTTP contract fixtures.

| Check | Result |
| --- | --- |
| Sync/import suite | 79 passed, including 16 real isolated PG cases |
| Budgets/recovery | Session larger than 1 MiB projects only lessons; 1,005 valid historical records plus one oversized gap; 10,005 Session metadata pagination; source changes, private staging, restart and tenant isolation passed |
| OV HTTP contracts | mkdir/stat conflict handling, wrong types, 403/404/timeouts, create races, pending indexes and repeated imports passed |
| Full Python | 801 passed, 48 skipped, 5 pre-existing baseline failures; no failures in this feature's tests |
| Frontend | 73 passed; production build passed with existing chunk-size warning |
| Engineering | Compilation, changed-file Ruff, documentation references and diff whitespace checks passed |
| Local service | Isolated foreground startup and daemon restart passed with PG/OV disabled; not real OV integration evidence |
| SIT/PRD and real OV retrieval | Not executed; no real import triggered |

The five baseline failures are `test_routes_accept_batch_and_preserve_historical_source_after_removal`, `test_engine_candidate_is_visible_to_tenant_validation_worker`, `test_tenant_model_settings_are_isolated_and_secrets_are_masked`, `test_envelope_only_prompt_title_comes_from_nested_query`, and `test_storage_status_reports_local_skills_and_pg_sessions`. Earlier verification sections refer to earlier baselines and are retained as history. Large fixtures establish completeness, not performance targets.


### Remove the TE tenant subdirectory (subsequent path change)

Current URIs are `{target_directory}/{source_key_hash}/{experience_id}.json`, without an appended `t_...` directory. No new configuration is required. This supersedes the tenant-directory layout in earlier validation records. PG state remains tenant-isolated; existing tenant advisory locks prevent same-tenant cross-instance reentry. Stable IDs and digest deduplication remain. Directory snapshots freeze content, not execution; cross-tenant overwrite protection for shared destinations is intentionally outside this change.

The worker recognizes legacy tenant-prefixed URIs and CAS-migrates each existing state record to the new URI, retaining its lesson ID, body and digest. Previous URI/error/attempt metadata is retained as `previous_destination`. Because the destination changed, delivery starts pending and reconciles the new address instead of waiting on the old address's backoff. Previously synced records are also copied to the new location. `experience_sync.destination_migrated` logs the move. Migration is idempotent, never resets the database or lesson identities, and never deletes old OV directories/files. Unchanged destinations retain existing backoff.

Path-change verification: 72 focused tests passed; full Python including isolated PG: 810 passed, 48 skipped, the same five baseline failures above. New cases cover migration of retry/synced records, idempotent migration, retained old files and tenant-lock reentry prevention. Local service restart used isolated configuration; no SIT deployment or real OV file deletion.
