# Skills

A Skill is an **executable, reusable task method** that states when it applies, the steps, constraints and supporting resources. The team Skill version chain is stored in PostgreSQL; new versions come from Candidate Review or administrator changes, and Agents pull them through `GET /sync/skills`.

## Skill Structure

A Skill Bundle is a set of files; `SKILL.md` is required:

```text
my-skill/
├── SKILL.md          # entry: YAML frontmatter + body
├── references/       # reference documents (optional)
├── scripts/          # helper scripts (optional)
└── assets/           # supporting resources (optional)
```

### SKILL.md Frontmatter

```yaml
---
name: my-skill           # unique identifier, same as the directory name
description: What this Skill is for and when it triggers
category: general        # category, default general
---
```

`name` and `description` are required; other frontmatter fields are kept as is. Parsing and rendering are in `team_skills/library/frontmatter.py`.

### Bundle Limits

The tenant `skills` module configuration limits bundle size, validated before writes:

| Field | Default |
| --- | --- |
| `max_bundle_files` | 1024 |
| `max_bundle_bytes` | 64 MiB |
| `max_file_bytes` | 16 MiB |

## Team Skill Library

PostgreSQL is the only authority for the team Skill library (`003_skills.sql`):

| Table | Content |
| --- | --- |
| `skills` | Current manifest head: name, `skill_id`, current version, description, category, `runtime_policy`, `sha256`, `tree_sha256` |
| `skill_versions` | Append-only version history |
| `skill_version_files` / `skill_blobs` | File list per version and content-addressed file bytes |
| `skill_mutations` | Mutation idempotency ledger |
| `skill_candidates` / `skill_candidate_files` / `skill_candidate_events` | Candidates, files and events |

Write rules (`team_skills/library/repository.py:SkillLibraryRepository`):

- Each mutation first locks the Skill head row inside the tenant transaction, serializing across Pods without in-process locks.
- The mutation ID is looked up after locking: a repeated request returns the first result, and the same ID with a different request returns a conflict. Console write endpoints pass it via the `Idempotency-Key` header.
- Version row, files, blobs, head and mutation are written in one transaction.
- Versions are append-only: rollback and delete both write new versions, delete is a tombstone version, and history is never rewritten.

The local `skills_runtime_cache_dir` is only a rebuildable cache materialized from these rows; a failed refresh does not affect committed state.

## Personal and Team Skills

| Aspect | Personal Skill | Team Skill |
|------|------------|------------|
| Storage | OpenViking `viking://resources/<root_prefix>/peers/<user>/skills/` | PostgreSQL version chain |
| Editing | The user or an administrator, via "Personal and team assets" | Tenant administrators, via Candidate Review or `/api/skills` |
| Agent read | Context `personal_skills` scope | `GET /sync/skills`, and Context `team_skills` scope |
| Promotion to team | The user submits a publish request, an administrator approves | — |

## Skill States

| State | Description | Affects Agents |
|------|------|---------------|
| Published | Current version in `skills` | Agents pull this version |
| Candidate | Four states: `open`, `validating`, `published`, `rejected` | Not before publishing |
| Historical | Versions in `skill_versions` superseded by newer ones | Restorable by Rollback |

Version numbers are monotonically increasing integers.

## Skill Lifecycle

```text
Evidence → skill.evolve → Skill Candidate (open)
                              │ evaluate / validate
                              ▼
                       validating: static checks + True Replay
                              ├─ accept (auto) or force → published → new version
                              ├─ reject → rejected
                              └─ inconclusive → back to open, awaiting manual handling
Administrator edits via /api/skills ──────────────► new version
```

## Agent Pull

```http
GET /sync/skills?user_id=alice
Authorization: Bearer tevt_...
If-None-Match: "<previous ETag>"
```

The response contains each Skill's name, version, `tree_sha256` and base64 file contents; the ETag is computed from the tenant, Account and each Skill's name, version and `tree_sha256`, and an unchanged snapshot returns `304`. See [Skill pull API](../api/06-skill-sync.md).

## Distribution to Agents

Skills are distributed only by pull: Agents call `GET /sync/skills` with the tenant credential + `user_id` to fetch a snapshot of all current Skills (with ETag). Publishing writes only versions and tombstones, creates no delivery outbox, and every API replica returns the same result.

## Skill and Memory Boundary

| Aspect | Skill | Memory |
|------|-------|--------|
| Nature | Executable task method (steps, procedure) | Retrievable facts and background |
| Prescribes execution | Yes | No |
| Validation | True Replay comparison | Memory Replay; aggregated content is constrained by the aggregation Skill |
| Writes | Candidate Review, administrator edits, publish requests | Agents write only personal Memory; team Memory is written by aggregation and DreamCycle |
| Typical example | "How to do Code Review" | "The team uses pnpm" |

## Code Entry Points

| Module | Path |
|------|------|
| Library repository | [repository.py](../../../team_skills/library/repository.py) |
| Bundle model | [bundle.py](../../../team_skills/library/bundle.py) |
| Frontmatter | [frontmatter.py](../../../team_skills/library/frontmatter.py) |
| Candidate repository | [candidates/repository.py](../../../team_skills/candidates/repository.py) |
| Static checks | [static_checks.py](../../../team_skills/candidates/static_checks.py) |
| HTTP | [api/](../../../team_skills/api/) |
