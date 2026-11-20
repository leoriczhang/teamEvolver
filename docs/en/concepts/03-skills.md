# Skills

A Skill is an **executable, reusable task method** that states when it applies, the steps, constraints and supporting resources. MA Skills use their native cloud version chain; TE reads, edits and publishes through cloud endpoints. Other team Skills are published in PostgreSQL and pulled through `GET /sync/skills`.

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

MA Skills use native `latest_version` and cloud packages. TE stores checked immutable historical packages in `ma_skill_snapshots`, keyed by connection, Skill ID and native version, without importing a team-library version chain. Cloud changes retire old Candidates and Evidence from automatic use while retaining history.

PostgreSQL is authoritative for non-MA team Skills (`003_skills.sql`):

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
| Storage | OpenViking `viking://resources/<root_prefix>/peers/<user>/skills/` | Native MA cloud versions or PostgreSQL for non-MA Skills |
| Editing | The user or an administrator, via "Personal and team assets" | Tenant administrators, via Candidate Review or `/api/skills` |
| Agent read | Context `personal_skills` scope | `GET /sync/skills`, and Context `team_skills` scope |
| Promotion to team | The user submits a publish request, an administrator approves | — |

## Skill States

| State | Description | Affects Agents |
|------|------|---------------|
| Published | Current version in `skills` | Agents pull this version |
| Candidate | Five states: `open`, `validating`, `published`, `rejected`, `archived` | Not before publishing |
| Historical | Versions in `skill_versions` superseded by newer ones | Restorable by Rollback |

An actual administrator change to a team Skill (editing, file changes, workbench saves, replacement
imports, rollback or deletion) records an automatic evolution reset time and archives the live
Candidate. Saving an identical file tree or retrying an idempotent mutation does not reset it.
Candidate Review publication does not trigger a manual reset.

Evidence, experience Patterns, Sessions and historical Test Datasets from before the reset no longer
enter that Skill's automatic generation or evaluation. Late Sessions use their business time, falling
back to ingestion time when unavailable. New candidates start from the current library file tree;
the old candidate package is not reused. Library changes during planning and late validation results
cannot overwrite the updated Skill. Historical candidates, versions, Cases, materials and replay
results remain available, and historical Datasets can still be replayed manually in the workbench.
The reset affects only the changed Skill.

Version numbers are monotonically increasing integers.

## Skill Lifecycle

```text
Evidence → skill.evolve → Skill Candidate (open)
                              │ evaluate / validate
                              ▼
                       validating: static checks + Candidate Validation
                              ├─ accept (auto) or force → published → new version
                              ├─ reject → rejected
                              └─ inconclusive → back to open, awaiting manual handling
Administrator edits via /api/skills ──────────────► new version
```

The Worker chooses experience review or Candidate execution from the actual differences and current
Evidence. Supported tool routing, applicability and missing-dependency instructions can pass an
evidence-cited review without claiming Runtime execution. Behavioral changes run one relevant
Candidate Case with known requirements upfront and at most two interactions. Stage validation states
its scope; complete delivery requires every requirement. One completed Candidate Case passes basic
validation, while A/B efficiency remains independent. Explicit full A/B uses both branches and the
original Runtime matrix.

The console's full A/B action or `validation_method: "full_ab"` on evaluate / validate selects an
efficiency comparison; `"auto"` is the default. Three historical Cases are no longer appended by
default. Completion reuse requires matching Candidate revision, file tree, task materials, assessment
scope and execution configuration. External blocks stop a Case. An unavailable judge preserves
returned outputs and artifacts; another validation assesses those archived outputs without remaking them.

Changed Agent/tools, Environment or Skill bindings archive automatic Cases and related Evidence with
a timestamp, reason and original execution fingerprint. Historical inputs, Sessions, artifacts and
verdicts stay unchanged. Manual workbench replay creates a separate experiment and does not reactivate
the archived Case.

## Agent Pull

```http
GET /sync/skills?user_id=alice
Authorization: Bearer tevt_...
If-None-Match: "<previous ETag>"
```

The response contains each Skill's name, version, `tree_sha256` and base64 file contents; the ETag is computed from the tenant, Account and each Skill's name, version and `tree_sha256`, and an unchanged snapshot returns `304`. See [Skill pull API](../api/06-skill-sync.md).

## Distribution to Agents

MA Skill publication runs as `ma.skill`: upload a native cloud version, update selected members and ancestors, and verify readback. Rollback creates a new cloud version from historical contents. Discovery/reconcile observe the cloud without pushing old TE versions. Other Agents pull team-library snapshots through `GET /sync/skills` with tenant credentials + `user_id` and ETag; publishing creates no delivery outbox.

## Skill and Memory Boundary

| Aspect | Skill | Memory |
|------|-------|--------|
| Nature | Executable task method (steps, procedure) | Retrievable facts and background |
| Prescribes execution | Yes | No |
| Validation | Experience review, Candidate execution or True Replay | Memory Replay; aggregated content is constrained by the aggregation Skill |
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
