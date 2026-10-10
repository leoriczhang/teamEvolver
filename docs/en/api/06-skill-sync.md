# Skill pull API

Skill distribution uses pull mode only. teamEvolver does not maintain
registered Agent callbacks or a Skill Sync webhook outbox.
`SkillMutationService` records versions and tombstones for publish, rollback,
and deletion; clients retrieve the current state through snapshot endpoints.

## Complete Bundle snapshot

```http
GET /sync/skills?user_id=alice
Authorization: Bearer tevt_<random>
```

The server derives the Account from the tenant credential and validates
`user_id`. A client-provided tenant declaration cannot change the data scope.

| Field | Description |
|-------|-------------|
| `status` | `ok` or `error` |
| `skills` | Skill Bundles for the current tenant |
| `skills[].name` | Skill name |
| `skills[].version` | Published version |
| `skills[].files` | Bundle files with base64 content |
| `skills[].tombstone` | Deletion marker; remove the local version when present |
| `total` | Number of Skills |

## Context Skill inventory

```http
GET /internal/agents/context/skills?user_id=alice&scope=team
Authorization: Bearer tevt_<random>
```

This endpoint returns `name`, `qualified_skill_id`, and `context_ref`; use
Context `read` to retrieve content. `integration_id` may be supplied as a
source label but requires no registry record.

## Client synchronization rules

1. Compare `name + version` to decide whether an update is required.
2. Atomically replace the complete Bundle instead of merging individual files.
3. Keep the latest successful snapshot and continue using it when a pull fails.
4. Remove a local Bundle when its tombstone is present.
5. Never persist the tenant Key from responses or logs.
