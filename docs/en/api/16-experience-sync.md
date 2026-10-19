# Successful Experiences API

Successful experiences come from Session analysis: the Worker writes `exemplary` (successful practice) and `defect` experiences from Sessions into `skill_experiences`, then compiles them per Skill into `experience_patterns` for Skill evolution. With experience sync enabled, the Worker projects `exemplary` experiences into an OpenViking directory for downstream readers such as knowledge base mining.

Code entry points: `team_skills/api/experiences.py:create_router`, `team_skills/experiences/supervisor.py:experience_tick`, `team_skills/experiences/transport.py`.

Reading experiences and Patterns requires a tenant member; compile, sync status and triggers require a tenant administrator. Accept endpoints return `{"status": "accepted", "job_id", "status_url"}` with task kind `experience.sync`.

## Experiences and Patterns

### GET /api/skill-experiences

| Parameter | Description |
| --- | --- |
| `kind` | `exemplary`, `defect` or empty |
| `skill` | Skill name, ≤200 characters |
| `search` | Full-text match, ≤500 characters |
| `limit` / `offset` | `limit` 1–200, default 50 |

Returns the experience documents, the total, and counts grouped by Skill and kind.

### GET /api/skill-experience-patterns

Same parameters, plus `status`: `ready` (enough evidence, usable by evolution) or `observing` (evidence accumulating).

### POST /api/skill-experience-patterns/compile

```json
{"skill": "cost-report", "force": false}
```

Recompiles the Patterns from the Skill's current experiences. Input is frozen at acceptance; when `source_hash` is unchanged and `force=false`, the Worker reuses the existing result.

## Experience Sync

The sync target is the `experience_sync` object in the tenant `skills` module configuration. It has no dedicated API; operators change the `skills` row in `tenant_configs`:

| Field | Description |
| --- | --- |
| `enabled` | Whether to project into OpenViking, default false |
| `endpoint` | OpenViking address, HTTP(S), without inline credentials, query or fragment |
| `account_id`, `user_id` | OpenViking Account and User used for writes |
| `secret_ref` | Secret Ref of the OpenViking key |
| `target_directory` | Default `viking://resources/agent_knowledge_workspace/input/proven_experiences` |
| `interval_seconds` | Automatic scan interval, default 30, minimum 5 |

On each tenant refresh the Worker runs `experience_tick`: when more than `interval_seconds` passed since the last scan, it accepts an incremental scan (starting 120 seconds before the last watermark); with `enabled=true` it then dispatches pending entries. The schedule is protected by a lease, so multiple Worker replicas do not run it twice.

Each experience is written to `<target_directory>/<tenant hash>/<subject_type>/<subject hash>.json`. It counts as `synced` only after the body is read back and verified and OpenViking's semantic and vector processing has finished.

### GET /api/experience-sync/status

```json
{
  "enabled": true,
  "target_directory": "viking://resources/agent_knowledge_workspace/input/proven_experiences",
  "counts": {"pending": 2, "synced": 10, "retry": 1, "dead_letter": 0},
  "index_pending": 1,
  "retry": {"due": 0, "deferred": 1, "next_attempt_at": 1800000000.0, "last_attempt_at": null},
  "manual": {"request_id": "experience-scan:...", "operation": "sync", "state": "completed",
             "requested_at": 1800000000.0, "finished_at": 1800000005.0, "error_code": ""},
  "last_scan": 1800000000.0
}
```

| Field | Meaning |
| --- | --- |
| `counts.pending` | Awaiting delivery or verification |
| `counts.retry` | Failed and awaiting retry |
| `counts.synced` | Read back, verified and indexed |
| `counts.dead_letter` | Exceeded retries, no further automatic retry |
| `index_pending` | Body written, semantic or vector processing not yet confirmed |
| `manual` | Latest scan command; `state` is `queued`, `running`, `completed`, `failed`, `cancelled` and similar |
| `last_scan` | Unix seconds of the latest scan |

Times are Unix seconds, null when not yet happened. The response contains no experience bodies or credentials.

### POST /api/experience-sync/trigger

Accepts an incremental scan now; the body must be empty.

### POST /api/experience-sync/import

Accepts a full import scan (ignoring the watermark); the body must be empty.

### GET /api/experience-sync/jobs/{job_id}/result

Result of a scan or compile command.

## Errors

| Status | detail | Cause |
| --- | --- | --- |
| 400 | `invalid experience kind or pattern status` and similar | Invalid parameter, or trigger / import sent a body |
| 400 | `invalid experience_sync configuration` and similar | Sync configuration incomplete or invalid |
| 403 | `TENANT_ADMIN_REQUIRED` | Not a tenant administrator |
| 404 | `experience command not found` | Command does not exist |
| 409 | `SYNC_DISABLED` | `experience_sync.enabled` is false |
| 503 | — | Tenant `skills` module configuration missing |

Operations notes are in the [Successful Experience Sync Guide](../guides/16-successful-experience-sync.md).
