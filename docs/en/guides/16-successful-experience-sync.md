# Successful Experience Sync Guide

Successful experience sync projects `exemplary` experiences produced by Session analysis into an OpenViking directory, where Agents can retrieve them and knowledge base mining can read them. PostgreSQL is the authoritative source; OpenViking being unavailable never affects saving experiences or Skill evolution. Sync calls no model and never triggers a Skill publish. The API is in [Successful Experience API](../api/16-experience-sync.md).

## Data Flow

1. Session analysis writes `exemplary` (successful practice) and `defect` experiences into the Session record.
2. The Worker's experience scan (`experience.sync` task, operation `sync`) pages through Sessions by ingest time and writes experiences into `skill_experiences` and `experience_occurrences`.
3. When the body of an experience (`id`, `skill_name`, `kind`, `experience_key`, `description`) changes, an `exemplary` experience appends a pending record to `experience_sync_outbox`.
4. With sync enabled, the Worker dispatches delivery tasks that write the record to OpenViking and read it back for verification.

Occurrence counts, Session IDs, users and scores stay in PostgreSQL only and are not part of the projection digest, so observing the same experience again causes no new write.

## Configuration

The sync configuration is the `experience_sync` object in the tenant `skills` module configuration. There is no dedicated write API; operators edit the `module='skills'` row in `tenant_configs`:

```json
{
  "experience_sync": {
    "enabled": true,
    "endpoint": "https://openviking.example.com",
    "account_id": "demo",
    "user_id": "team",
    "secret_ref": "secret:...",
    "target_directory": "viking://resources/agent_knowledge_workspace/input/proven_experiences",
    "interval_seconds": 30
  }
}
```

| Field | Description |
|------|------|
| `enabled` | Whether to deliver to OpenViking, default false; scanning still runs when off |
| `endpoint` | OpenViking address, HTTP(S), without inline credentials, query or fragment |
| `account_id`, `user_id` | Account and User used for writes |
| `secret_ref` | Secret Ref of the OpenViking key, decrypted by the Worker |
| `target_directory` | Target directory under `viking://resources/` |
| `interval_seconds` | Automatic scan interval, default 30, minimum 5 |

Changes take effect on the next Worker tenant refresh round without a restart.

## Scheduling

On every tenant refresh round (about 30 seconds) the Worker runs `experience_tick`:

- When more than `interval_seconds` have passed since the last scan, it accepts an incremental scan starting 120 seconds before the last watermark, covering data delayed by long transactions
- With `enabled=true`, it dispatches delivery tasks for pending records

Scan and delivery each hold a lease (`experience:scan`, `experience:deliver`), so multiple Worker replicas never run them twice. A scan reads 100 rows per page and writes a checkpoint; an interrupted task resumes from it.

## OpenViking Write and Verification

Each experience is written to a fixed URI under the target directory. Writes use `wait=true`; afterwards the body is read back and its digest compared. A record counts as `synced` only when the digest matches and OpenViking reports semantic and vector processing complete. When the body is written but the index is not ready, the record is marked `OV_INDEX_NOT_READY` and retried.

Each request times out after 30 seconds, and a response larger than 2 MiB is an error. Retryable errors (network errors, 408, 429, 5xx, index not ready, content mismatch) are attempted at most 3 times and then go to `dead_letter`; non-retryable errors (such as 401, 403, path type conflict) go to `dead_letter` directly.

When an experience is deleted or changed at the source, the old file in OpenViking is kept and not withdrawn. After `target_directory` changes, new records are written to the new directory and old files are not moved.

## Manual Operations

| Operation | API | Description |
|------|------|------|
| Sync now | `POST /api/experience-sync/trigger` | Accepts one incremental scan |
| Import existing | `POST /api/experience-sync/import` | Full scan of all Sessions without the watermark |
| View status | `GET /api/experience-sync/status` | Per-status counts, retry schedule, last manual command |

The console "Experience Library" page offers the same actions to tenant administrators.

## Status and Troubleshooting

`GET /api/experience-sync/status` returns:

| Field | What to watch |
|------|--------|
| `counts.pending` | Steady growth means delivery is not running; check `enabled` and the Worker |
| `counts.retry` / `retry.next_attempt_at` | Records in backoff and the next attempt time |
| `counts.dead_letter` | Abandoned records; fix the cause and import again |
| `index_pending` | OpenViking indexing is slow |
| `manual.state` / `manual.error_code` | Result of the last manual scan |

Common error codes:

| Error code | Cause |
|--------|------|
| `SYNC_DISABLED` | Manual trigger while `enabled=false` |
| `OV_HTTP_401` / `OV_HTTP_403` | Invalid key or no write permission on the target directory |
| `OV_NETWORK_ERROR` | The Worker cannot reach OpenViking |
| `OV_PATH_TYPE_CONFLICT` | A directory or file of the same name exists on the target path |
| `OV_INDEX_NOT_READY` | Body written, index not yet complete |
| `OV_CONTENT_MISMATCH` | The body read back differs from the projection |
| `experience_secret_unavailable` | `secret_ref` cannot be decrypted; check `TEAMEVOLVER_SECRET_MASTER_KEY` |

## Related Documents

- [Successful Experience API](../api/16-experience-sync.md)
- [Knowledge Base Mining Guide](./07-skill-miner.md)
- [Web Console User Guide](./03-console.md)
