# Publish and Rollback

Every change to a team Skill is written to an append-only version chain in PostgreSQL. Publish, update, rollback and delete share one set of transaction rules (`team_skills/library/repository.py:SkillLibraryRepository`) and produce the same result on any API or Worker replica.

## Mutation Types

| action | Source | Result |
| --- | --- | --- |
| `publish` | First write of a Skill | Version 1 |
| `update` | Candidate publish, `POST /api/skills`, `POST /api/skills/{name}/files`, file deletion | New version; returns `unchanged` when the content did not change |
| `rollback` | `POST /api/skills/{name}/rollback` | Writes a new version with historical content |
| `delete` | `DELETE /api/skills/{name}` | Writes a tombstone version; the Skill leaves the current manifest |

## Transactional Commit

A mutation completes inside the caller's tenant transaction:

1. `SELECT ... FOR UPDATE` locks the `skills` head row, so concurrent mutations of the same Skill are serialized across Pods.
2. `skill_mutations` is queried after locking: the same `mutation_id` replays the first result; the same ID with a different request returns a conflict.
3. The bundle is validated before writing: `SKILL.md` format, path safety and tenant bundle limits.
4. `skill_versions`, `skill_version_files`, `skill_blobs`, the `skills` head and `skill_mutations` are written in the same transaction.

Nothing is written when the transaction fails. After publishing, Agents receive the Skill through pull (see `GET /sync/skills` in [Skills](./03-skills.md)).

## Idempotency

| Entry | mutation_id |
| --- | --- |
| Console `/api/skills*` | `Idempotency-Key` header; a new ID is generated each time when absent |
| Candidate publish | `candidate:<candidate_id>:r<revision>` |
| Skill import | Derived from the import command ID |

## Monotonic Versions

Version numbers are monotonically increasing integers per Skill. Versions are append-only and historical versions are never modified or deleted; `GET /api/skills/{name}/versions` returns the full history and `GET /api/skills/{name}/versions/{version}` returns one version's content.

## Tombstone

Delete writes a tombstone version: it has no files, the `skills` head is marked deleted, and the Skill no longer appears in `GET /api/skills` or `GET /sync/skills`. Earlier versions remain readable and can be republished by rollback.

## Rollback Is a New Version

```http
POST /api/skills/my-skill/rollback?target_version=3
Idempotency-Key: rollback-my-skill-v3
```

Rollback writes the full content of v3 as current max version + 1; the response contains `restored_from` and `new_version`, and all history after v3 and the audit chain stay unchanged.

## Audit Chain

| Record | Content |
| --- | --- |
| `skill_mutations` | mutation_id, action, version, request hash, actor, source, metadata |
| `skill_candidate_events` | Every Candidate state change, edit and feedback |
| `audit_log` | Platform-level audit |

## Code Entry Points

| Module | Path |
|------|------|
| Library repository and commit | [repository.py](../../../team_skills/library/repository.py) |
| Publication primitives | [publication.py](../../../team_skills/library/publication.py) |
| Schema | [003_skills.sql](../../../teamEvolver/database/schema/003_skills.sql) |

## Related Docs

- [Skills](./03-skills.md)
- [Evolution Loop](./02-evolution-loop.md)
- [Skill Management API](../api/09-skills-admin.md)
