# Publish & Rollback

All team Skill changes (publish, update, rollback, delete) in teamEvolver must execute through `SkillMutationService`, ensuring transactional commits, monotonically increasing versions, complete audit chains, and reliable sync distribution. **No version is ever physically deleted**—rollback restores historical content by creating new versions; audit chain always complete.

## SkillMutationService

`SkillMutationService` is the sole entry point for team Skill changes, responsible for:

- Transactional commits (commit records + tombstone)
- Persistent sync outbox (ensuring distribution reliability)
- Monotonically increasing version numbers
- Preserving complete audit chains
- Idempotent execution (same mutation_id not re-executed)

All writes directly operating SkillHub storage bypassing this service are non-compliant and break audit chains and sync guarantees.

### Mutation Commands (SkillMutationCommand)

All changes described via `SkillMutationCommand`:

| Field | Description |
|-------|-------------|
| `action` | Operation type: `publish`, `update`, `rollback`, `delete` |
| `name` | Skill unique identifier name |
| `mutation_id` | Change unique ID (idempotency key; duplicate submissions return existing result) |
| `skills_dir` | For publish/update, local Skill Bundle directory path |
| `target_version` | For rollback, target rollback version number |
| `tenant_ids` | Target tenant/integration ID list (for targeted sync) |
| `skill_filter` | Optional filter |
| `metadata` | Additional metadata |

## Transactional Commits

When executing changes, `SkillMutationService` writes records in following order:

1. **Idempotency check**: Queries `skill_mutation_commits/{mutation_id}.json`; if exists returns existing result directly
2. **Execute change**: Calls SkillHub to perform actual write
   - `publish`/`update`: Calls `push_skills` to upload Skill Bundle to storage
   - `rollback`: Reads `target_version` historical Bundle, writes back as current active version
   - `delete`: Deletes Skill object subtree, writes tombstone
3. **Write Commit record**: Writes change result to `skill_mutation_commits/`
4. **Write/update Outbox event**: Creates or updates sync event in `skill_sync_outbox/`
5. **Write Tombstone** (delete operations only): Records deletion marker in `skill_tombstones/{name}/v{version}.json`

All write operations atomic at object storage level (put_object); Commit records and Outbox events deduplicated idempotently via stable fingerprints (`_stable_id`), ensuring same change doesn't produce duplicate events.

> **Note**: If SkillHub's `push_skills` operation returns `uploaded=0` (content unchanged), MutationService returns `status: "unchanged"`, no new Outbox event created.

## Commit Records

Each change produces one immutable Commit record at `skill_mutation_commits/{mutation_id}.json`, schema version `teamevolver.skill-mutation-commit.v1`:

```json
{
  "schema_version": "teamevolver.skill-mutation-commit.v1",
  "mutation_id": "...",
  "action": "publish|update|rollback|delete",
  "expected": {
    "name": "skill-name",
    "version": 5,
    "sha256": "...",
    "tree_sha256": "..."
  },
  "tenant_ids": ["integration-1", "integration-2"],
  "event_id": "skill_evt_...",
  "result": { ... },
  "metadata": { ... },
  "committed_at": "2025-01-01T00:00:00+00:00"
}
```

Commit records are audit chain core, recording who made what change when, expected Skill state after change (version, sha256, tree_sha256), and associated sync event ID.

## Tombstone

Delete operations do not directly erase history; they write Tombstones:

- Path: `skill_tombstones/{name}/v{version}.json`
- Content: Contains `name`, `version`, `sha256`, `tree_sha256`, `deleted: true`, `deleted_at`, `mutation_id`
- Version number: On deletion, version number increments by 1 from current (e.g., current v3, writes v4 tombstone after deletion)
- Purpose: Marks Skill deleted while preserving version chain continuity; `reconcile` operation can reconstruct missing commit records from tombstones

Tombstones ensure even if Skill object deleted, version history and change records remain traceable.

## Persistent Sync Outbox

After Commit succeeds, changes not pushed directly to Agents, but written to persistent Outbox queue, distributed asynchronously by background drain process. This guarantees "at-least-once" delivery semantics.

### Outbox Event Structure

Path: `skill_sync_outbox/{event_id}.json`, schema version `teamevolver.skill-sync-outbox.v1`:

```json
{
  "schema_version": "teamevolver.skill-sync-outbox.v1",
  "event_id": "skill_evt_...",
  "action": "publish|update|rollback|delete",
  "mutation_id": "...",
  "skills": [{ "name": "...", "version": 5, "sha256": "...", ... }],
  "tenant_ids": ["integration-1"],
  "status": "pending|synced|dead_letter|cancelled",
  "attempt": 0,
  "next_retry_at": "...",
  "deliveries": {
    "integration-1": {
      "status": "synced|pending|failed|cancelled",
      "attempt": 2,
      "acked_at": "...",
      "last_error": "...",
      "next_retry_at": "..."
    }
  },
  "created_at": "...",
  "updated_at": "..."
}
```

### Delivery Mechanism

`drain()` method consumes Outbox events:

1. Scans events under `skill_sync_outbox/` prefix
2. Skips events in `synced`/`cancelled` state
3. Checks `next_retry_at`; not due counted as pending
4. Due events call `deliverer` (default `sync_skill_event`) for actual distribution
5. Updates each integration's delivery state based on distribution result:
   - **synced**: Agent acknowledges receipt (`{"ok": true, "results": {...}}`)
   - **cancelled**: Integration not supported or explicitly cancelled
   - **pending**: Failed, exponential backoff retry (2^attempt seconds, max 3600 seconds)
   - **dead_letter**: Retry count ≥8, enters dead letter queue
6. After all deliveries reach terminal state (synced/cancelled), event marked `synced`

Retry backoff strategy: After Nth failure wait `min(3600, 2^N)` seconds before retry.

### Outbox Repair (Reconcile)

`reconcile()` method repairs inconsistent states:

- Scans all commit records, checks corresponding outbox events exist, creates if missing
- Scans all tombstones, creates commit and outbox events for tombstones without commit records
- Scans Skills in current manifest, ensures each current version has corresponding commit record
- This restores consistency after service restarts, partial storage failures, or manual repair

## Monotonically Increasing Versions

Skill version numbers in teamEvolver always increase monotonically, old version numbers never reused:

- Each publish/update creates new version number (allocated by `SkillIDRegistry.record_update`)
- Delete operations increment by 1 from current version when writing tombstone
- Rollback operations also create new version numbers: reads historical version Bundle content, writes new version number
- Version chain append-only, existing version records not modified

Version numbers managed by `SkillIDRegistry`, incrementing on each `record_update`. Registry itself also persisted in object storage.

## Audit Chain

Complete audit chain consists of:

1. **Commit records** (`skill_mutation_commits/`): Immutable records per change, containing who (actor/uploaded_by), when (committed_at), what (action, expected state), why (metadata)
2. **Version history** (Registry): Complete version number sequence per Skill and each version's action (create/update/rollback/delete), timestamps
3. **Version Bundles** (`skills/{name}/versions/v{n}/`): Complete Skill Bundle snapshot per version, reconstructible anytime
4. **Tombstone** (`skill_tombstones/`): Deletion operation markers
5. **Outbox events** (`skill_sync_outbox/`): Distribution records, containing per-integration delivery history, retry counts, error messages
6. **Delivery audit** (audit field in deliveries): Operator, reason, time for cancel/retry operations

From current state at any point in time, can trace through version chain to complete change history for that Skill, including every publish, update, rollback, and delete.

## Rollback as New Version

Rollback is not restoring old version then continuing modification on old version number—the essence of rollback is: **recreating historical version's complete content as a new version**.

`rollback_skill` execution logic:

1. Reads `target_version` historical Bundle (`versions/v{target_version}/`)
2. Validates SKILL.md exists
3. Writes Bundle content back to current active location (`skills/{name}/SKILL.md` and accessory files)
4. Cleans up redundant old Bundle files
5. Registry records new version number, action marked `rollback:v{target_version}`
6. Saves new version Bundle snapshot to `versions/v{new_version}/`
7. Updates manifest; new version number becomes current version

This means:
- Version numbers only increase after rollback, never decrease
- Rolled-back version (problematic version) and pre-rollback historical versions both fully preserved
- Can rollback again to any historical version, including previous "bad version" before rollback
- Audit chain shows "rolled back to v3 content at v7, creating v8"

> **Note**: Rolled-back content exactly matches target_version (tree_sha256 identical), but version number is new. Agents see normal new version release on pull; no special rollback logic needed.

### Why not directly delete or overwrite old versions?

- **Auditability**: Can always answer "what did that problematic v5 look like at the time"
- **Replayability**: True Replay can re-execute validation on any historical version
- **Traceability**: If rollback itself found problematic after rollback, can rollback again to pre-rollback version
- **Distribution consistency**: Agents only need to understand "new version number = update needed"; no special version rollback logic to handle

## Outbox Health Monitoring

`health()` method returns Outbox queue health status:

- `backlog`: Pending event count (excluding synced/cancelled)
- `oldest_age_seconds`: Wait time of oldest incomplete event
- `dead_letter`: Dead letter event count (8 retries still failed)
- `last_error`: Most recent error message

Management operations:
- `retry(event_id, integration_id?)`: Resets delivery status for event or specific integration, redelivers
- `discard(event_id, integration_id?, actor, reason)`: Cancels delivery for event or specific integration, records audit info

## Code Entry Points

| Module | Path |
|--------|------|
| SkillMutationService | [skills/mutations.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/mutations.py) |
| SkillHub (underlying storage ops) | [skills/hub.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/hub.py) |
| Skill Bundle model | [skills/bundle.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/bundle.py) |
| Sync adapters | [integrations/skill_sync_adapters.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/skill_sync_adapters.py) |
| Skill ID registry | [skills/registry.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/registry.py) |

## Related Documentation

- [Skill System](./03-skills): Skill structure, version states, lifecycle
- [Evolution Loop](./02-evolution-loop): Publish stage position in evolution loop
- [True Replay](./06-true-replay): Candidates only enter publishing after validation
- [Checklist Gate](./07-checklist): Checklist as prerequisite gate for automatic publishing
- [Architecture Overview](./01-architecture): Skill Mutation Service position in system architecture
