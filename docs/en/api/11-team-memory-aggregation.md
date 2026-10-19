# Team Memory API

Team Memory aggregates the personal Memory of every user in the Account into shared team Memory and updates it through the maintenance pipeline. Concepts, pipeline and configuration fields are in [Memory](../concepts/04-memory.md). Administrators can run it manually or enable periodic Worker-triggered maintenance.

Code entry points: `team_memory/api/__init__.py:create_router`, `team_memory/api/skills.py:create_router`, `team_memory/api/replay.py:create_router`, `team_memory/pipeline.py`.

All endpoints require a tenant administrator. Request bodies are limited to 256 KiB.

## Memory Runs

### POST /api/aggregation/run

| Field | Description |
| --- | --- |
| `pipeline` | `both` (default), `aggregation`, `maintenance` |
| `mode` | `incremental` (default) or `full` |
| `user_ids` | Optional; aggregate only these users, non-empty and unique, at most 50000 |
| `kinds` | Optional list of Memory kinds; defaults to configuration `aggregation.kinds` |
| `target_uri` | Optional, default `viking://resources/<shared_knowledge_prefix>` |
| `account_id` | Optional; must equal the tenant Account, otherwise 400 `TENANT_ACCOUNT_MISMATCH` |
| `last_compile_time` | Optional, ISO 8601 |

```json
{"job_id": "memory:...", "task_id": "memory:...", "status": "queued", "status_url": "/api/jobs/memory:...",
 "target_uri": "viking://resources/shared-knowledge"}
```

Supports `Idempotency-Key`. Only one Memory task runs per Account at a time; later tasks wait for the lease on the Worker side.

### POST /api/memory/maintenance/run

Same as `/api/aggregation/run` with `pipeline` fixed to `maintenance`.

### Queries

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/aggregation/runs` | Run list: `task_id`, `target_uri`, `status`, `result`, `created_at`, `finished_at`, `config_revision` |
| GET | `/api/aggregation/status/{task_id}` | Single run; 404 when absent |
| GET | `/api/memory/maintenance/status` | Latest maintenance run; `{"status": "idle"}` when none |
| GET | `/api/memory/maintenance/dry-run` | Current `memory` module configuration and revision, performs nothing |

## Settings

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/aggregation/settings` | Aggregation settings, `maintenance_trigger_mode`, `maintenance_interval_seconds`, and `revision` |
| POST | `/api/aggregation/settings` | Saves aggregation and maintenance trigger settings; requires an integer `expected_revision`, conflicts return 409 |

Field descriptions are in [Memory · Configuration](../concepts/04-memory.md#configuration).

## Aggregation and Maintenance Skills

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/aggregation/okf-skill?stage=aggregation` | Read the Skill: `skill_uri`, `skill_name`, `body`, `revision` |
| PUT | `/api/aggregation/okf-skill?stage=aggregation` | `{"body": "<SKILL.md>"}` publishes, returns 202 |
| POST | `/api/aggregation/users` | List OpenViking users in the Account |

`stage` is `aggregation` or `maintenance`, mapping to configuration `okf_skill_uri` and `maintenance_skill_uri`. When publishing, the `name` in `SKILL.md` must equal the last URI segment and the body must not exceed 128 KiB; it is written to OpenViking as a `memory.compile` task.

## Memory Change and Memory Replay

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/memory/changes?limit=100` | Memory Change list, `limit` 1–500 |
| POST | `/api/memory/changes/{change_id}/true-replay` | Run Replay with the before/after snapshots as Baseline / Candidate, returns 202 |
| GET | `/api/memory/changes/{change_id}/true-replays?limit=100` | Replay history of the change |
| POST | `/api/openviking/memory/true-replay` | Submit `memory_path`, `before_content`, `after_content` and a dataset directly, returns 202 |

The remaining Replay request fields (dataset, `runtime`, `timeout_seconds`, `max_interactions`) match [Replay Runs](./05-replay-branch.md#replay-runs). Returns `{"job_id", "run_id", "status_url"}`.

## Errors

| Status | Cause |
| --- | --- |
| 400 | Invalid field, `TENANT_ACCOUNT_MISMATCH`, Skill name does not match the URI, missing `expected_revision` |
| 403 | Not a tenant administrator |
| 404 | Run or Memory Change does not exist |
| 409 | Configuration revision conflict |
| 413 | Body exceeds 256 KiB |
| 503 | `memory` module configuration missing, OpenViking configuration unavailable, Memory Replay integration unavailable |
