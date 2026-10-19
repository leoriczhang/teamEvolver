# Team Skill Library API

The team Skill library is stored in PostgreSQL: `skills` holds the current version, `skill_versions` each immutable version, and bundle files are stored content-addressed. Every write creates a new version in one transaction, and every API replica reads the same data. After a write commits the local runtime cache is refreshed on a best-effort basis; a cache failure is only reported in the response's `runtime_cache` and does not affect the committed version.

Code entry points: `team_skills/api/__init__.py:create_router`, `team_skills/library/repository.py:SkillLibraryRepository`.

Agent pulls are covered in [Skill Pull](./06-skill-sync.md). Personal Skills (`/api/users/{user_id}/skills*`) are stored in the OpenViking personal space; see [Console Identity and Tenant Permissions](./20-console-rbac.md#personal-skills-and-publish-requests).

## Permissions and Common Rules

- Read endpoints require console login (tenant member) or the Root Key; write endpoints require a tenant administrator.
- Skill names may contain only letters, digits, `.`, `-` and `_`.
- Bundle limits come from the tenant `skills` module configuration: `max_bundle_files` (default 1024), `max_bundle_bytes` (default 64 MiB), `max_file_bytes` (default 16 MiB). Exceeding them returns 413.
- Write endpoints support `Idempotency-Key`: retrying with the same key returns the first result without creating another version.
- Write bodies are limited to 96 MiB.

## Reads

### GET /api/skills

```json
{
  "skill_backend": "postgres",
  "delivery_mode": "pull",
  "skills": [{"name": "cost-report", "skill_id": "...", "version": 3, "description": "...", "category": "general",
              "sha256": "...", "tree_sha256": "...", "files": [], "file_count": 2, "runtime_policy": {},
              "updated_by": "admin", "updated_at": "..."}]
}
```

### GET /api/skills/{name}

Current version, with `body`, `skill_md` (full `SKILL.md`), `files`, `file_count`, `version`, `updated_at`.

### GET /api/skills/{name}/versions

```json
{"name": "cost-report", "skill_id": "...", "current_version": 3, "versions": [1, 2, 3],
 "history": [{"version": 3, "action": "rollback", "timestamp": "...", "tree_sha256": "...", "actor": "admin", "restored_from": 1}]}
```

`versions` excludes tombstone versions from deletes; `history` includes all versions.

### GET /api/skills/{name}/versions/{version}

The given version's `content`, `raw_md`, `files` (`path`, `sha256`, `size`), `tree_sha256`, `is_current`, and `evolution`: which evolution cycle or Candidate produced it.

## Writes

### POST /api/skills

Create or update `SKILL.md`, keeping existing attachments.

| Field | Description |
| --- | --- |
| `name` | Required |
| `skill_md` | Full `SKILL.md`; when given, the structured fields below are ignored |
| `description` | Required for structured edits |
| `category` | Default `general` |
| `body` | Body |
| `base_version` | Optional; returns 409 when the current version has changed |

Structured edits keep other keys of the existing frontmatter. The response contains `version`, `files`, `file_count`, `created`, `runtime_cache`.

### POST /api/skills/{name}/files

```json
{"files": [{"path": "scripts/run.py", "content_b64": "..."}]}
```

Adds or replaces attachments. Paths must be safe relative paths inside the bundle; `SKILL.md` can only be changed with `POST /api/skills`.

### DELETE /api/skills/{name}/files/{rel_path}

Deletes one attachment; `SKILL.md` cannot be deleted.

### POST /api/skills/{name}/rollback?target_version=N

Publishes the content of version N as the new current version and returns `restored_from` and `new_version`.

### DELETE /api/skills/{name}

Writes a tombstone version; the Skill disappears from the list and Agent pulls, and historical versions are kept.

## Errors

| Status | Cause |
| --- | --- |
| 400 | Invalid name, missing field, unsafe path, invalid base64, or an attempt to change `SKILL.md` through the attachment endpoint |
| 403 | Not a tenant administrator |
| 404 | Skill or version does not exist |
| 409 | `base_version` conflict, or an `Idempotency-Key` reused for a different request |
| 413 | Bundle or body over the limit |
| 503 | Tenant `skills` module configuration missing |
