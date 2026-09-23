# Storage Spaces and Directory Layout

teamEvolver configures Session state and Skill assets independently. In the Platform Assets view, Session runtime state comes from PostgreSQL or local/NAS storage, and Skill-evolution artifacts come from local/NAS storage. OpenViking remains the backend for Personal Memory, Team Resources, and the Agent-facing Skill mirror. Published Skills may still use local/NAS or OpenViking according to deployment configuration. This document describes the logical key layout and how a teamEvolver **account maps to OpenViking spaces**.

## Account ↔ OpenViking Space Mapping

A teamEvolver account does not own a dedicated OpenViking tenant. Instead it resolves into two things: an **API key (authentication) + a URI path (location)**. The backend defines eight OpenViking scopes, while the console asset page directly exposes only the `personal_workspace` and `team_workspace` namespace roots. Platform Assets is not one of these scopes.

The mapping is defined in `_scope_map()` in `teamEvolver/proxy/openviking_workspace.py`.

| Scope | OpenViking root URI | Space | Kind | Writable by regular user |
|-------|---------------------|-------|------|--------------------------|
| `personal_memory` | `viking://user/{personal user}/memories` | personal | memory | ✅ |
| `personal_skills` | `viking://resources/team-skill-evolver/peers/{account}/skills` | personal | skills | ✅ |
| `personal_resources` | `viking://user/{personal user}/resources` | personal | resources | ✅ |
| `personal_workspace` | `viking://user` | personal | personal-memory root | ✅ |
| `team_memory` | `viking://resources/shared-knowledge` | team | memory | ❌ admin only |
| `team_skills` | `viking://resources/team-skill-evolver/skills` | team | skills | ❌ admin only |
| `team_resources` | `viking://resources/team` | team | resources | ❌ admin only |
| `team_workspace` | `viking://resources` | team | team-resources root | ❌ admin only |

URI variables:

| Variable | Source | Default |
|----------|--------|---------|
| `root_prefix` | `sharing.viking_root_prefix` | `team-skill-evolver` (data-contract constant, do not rename) |
| `personal user` | user `personal_space.viking_user` → account ID → `sharing.viking_personal_user` | account ID |
| `team user` | `sharing.viking_user` | `team` |
| `account` | `id` in the user registry | — |
| `shared_knowledge_prefix` | `aggregation.shared_knowledge_prefix` | `shared-knowledge` |

### Namespace split

- **Personal memory** lives in the `viking://user/{user}/` namespace, isolated per person.
- **Aggregated team memory** lives in `viking://resources/{shared_knowledge_prefix}/`, shared for account-wide retrieval.
- **The team Skill mirror** lives under `viking://resources/{root_prefix}/`; **team Resources** map separately to `viking://resources/team/`.
- **Personal skills** are isolated inside the shared namespace via a `peers/{account}/` path segment — see `peer_key_prefix()` in `teamEvolver/storage/base.py`.

### Root Key and identity headers

Console access to OpenViking always uses the tenant-level Trusted Root Key. User-level keys are neither read nor stored. A space is selected only by:

- `X-OpenViking-Account`: the Account bound to the current tenant
- `X-OpenViking-User`: the user's bound Name for Personal Memory, or the team Name for Team Resources

`X-OpenViking-Agent` remains `team-skill-evolver`. The Root Key stays server-side and supplies `X-API-Key` and `Authorization`; regular users never receive it. `sharing.viking_team_api_key` remains only as a legacy field name for the Root Key, while `sharing.viking_personal_api_key(s)` no longer participates in Workspace authentication.

## Platform Asset Directory Map

Platform Assets is a read-only view of service-owned state and no longer reads the OpenViking file tree. The console exposes Session state from PostgreSQL (or local/NAS) under `platform://session/...` and Skill-evolution artifacts from NAS under `platform://skill/...`. Only allowlisted internal paths are visible; Agent assets such as `skills/` and `peers/` are excluded. The Agent-facing `skills/<name>/` subtree may still be mirrored to OpenViking asynchronously, but that mirror is not a Platform Assets data source.

### 1. Skill library (finished artifacts) — local/NAS or OpenViking

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `skills/` | dir | Official team Skill library. Each Skill has the current `SKILL.md` and immutable `versions/vN/` bundles. In local/NAS mode, `team_skills/library/mirror.py:VikingSkillMirror` can mirror the Agent-facing subtree to OpenViking asynchronously | `team_skills/library/hub.py`, `team_skills/library/mirror.py` |
| `manifest.json` | file | Skill manifest index: skill name → version/hash, used to diff local vs. remote | `team_skills/library/hub.py` |
| `evolve_skill_registry.json` | file | Skill ID registry keeping IDs stable across nodes | `team_skills/library/registry.py` |

### 2. Skill lab and evolution material — Skill backend

This backs the "data-driven evolution" loop: mine datasets from historical sessions, generate matching test sets, and validate effectiveness.

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `skill_lab/` | dir | Skill lab. `skill_lab/datasets/<id>/` holds datasets, `skill_lab/runs/<id>/` holds experiment run results | `team_replay/lab/service.py` |
| `skill_datasets/` | dir | Skill test sets under `skill_datasets/by-id/<dataset>` with peer associations in `skills[]` | `team_replay/datasets/store.py` |
| `evolution_datasets/` | dir | Evolution datasets synthesized from historical sessions | `team_replay/datasets/synthesis.py` |
| `skill_evidence/` | dir | Skill effectiveness evidence (`<skill>.json`): injection counts, effectiveness, and other evolution-decision inputs | `team_skills/evolution/runtime/evidence.py` |
| `skill_version_context/` | dir | Per-version skill context (`<skill>/v<N>.json`) used as a True Replay baseline | `team_skills/candidates/store.py` |

Every newly written Test Dataset uses `team-replay.dataset.v2`. The document
envelope contains `dataset_id`, peer `skills[]`, `source`, and `cases[]`; each
Replay Case contains `query`, `skill_ids[]`, `checks[]`, `materials[]`,
`provenance`, and `replay`.
Session snapshots are stored separately and referenced by
`provenance.snapshot_ref`. Legacy progressive, Skill Lab, Session collection,
and Benchmark JSONL shapes remain read-only adapter inputs and are rewritten as
v2 when modified. JSONL, Markdown, and ZIP are transport or human-readable
views rather than separate internal schemas.
Entries in `skills[]` are peers with no primary role.
`cases[].skill_ids[]` explicitly identifies the Skills used by each task. A
multi-Skill Replay installs the same Skill set in both branches and replaces
only the Skills changed by that experiment; the dataset relationship itself
does not designate a primary Skill.

### 3. Session pipeline (evolution raw material) — Session backend

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `sessions/` | dir | Pending session queue (`<session_id>.json`); removed after the evolution engine consumes it | `teamEvolver/session_store.py` |
| `session_archive/` | dir | Permanent session archive | `teamEvolver/session_store.py` |
| `session_filter_audit/` | dir | Session filter-decision audit (why queued/skipped) | `teamEvolver/session_store.py` |
| `session_ledger/` | dir | Session ledger recording the queued→consumed lifecycle transitions | `team_skills/evolution/runtime/orchestrator.py` |
| `session_datasets/` | dir | `team-replay.dataset.v2` collections built from historical Sessions, plus detached snapshots | `team_replay/datasets/collections.py` |
| `session_index.json` | file | Session metadata index (title, turns, tokens, status) for fast console browsing | `teamEvolver/session_store.py` |

### 4. Evolution validation (True Replay loop) — Skill backend

Rules in `team_skills/candidates/store.py`.

| Entry | Type | Purpose |
|-------|------|---------|
| `candidate_skills/` | dir | Candidate-skill staging (`<job_id>/SKILL.md` + files), not yet promoted to `skills/` |
| `validation_jobs/` | dir | Validation jobs (`<job_id>.json`) produced by the evolution service |
| `validation_claims/` | dir | Job claim locks (`<job_id>/<user_alias>.json`) preventing duplicate validation |
| `validation_results/` | dir | Per-client independent validation results (`<job_id>/<user_alias>.json`) |
| `validation_evaluations/` | dir | Aggregated evaluation of multiple results (`<job_id>.json`) |
| `validation_decisions/` | dir | Final publish/reject decision (`<job_id>.json`) |
| `validation_decision_index.json` | file | Decision index for fast lookup |

### 5. Human review — Skill backend

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `human_review/` | dir | Human-review task queue (`<job_id>.json`): escalated when an automated decision is uncertain | `team_skills/candidates/store.py` |

### 6. DreamCycle team-memory maintenance — local storage

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `memory-changes/` | dir | Memory-change ledger (`teamevolver.memory-change.v1`): recorded when DreamCycle dedups/cleans/consolidates memory, enabling True Replay of memory edits | `team_memory/memory_changes.py` |

### 7. Isolation and low-level structure — OpenViking (remote structure)

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `peers/` | dir | Per-customer/user isolation area (`peers/{account}/...`). Personal skills live under `peers/{account}/skills` | `teamEvolver/storage/base.py` |
| `knowledge/` | dir | OpenViking's own top-level data category (alongside memories/resources/skills), not created by teamEvolver business code | — |
| `.abstract.md` | file | OpenViking auto-generated directory **L0 abstract** (one-line summary) | — |
| `.overview.md` | file | OpenViking auto-generated directory **L1 overview** (structured description) | — |

## Data Flow

```
Agent session ingest
   → sessions/ ──(ledger)→ session_ledger/ ──(archive)→ session_archive/
                                │ mine / synthesize
                    evolution_datasets/ + skill_datasets/ → skill_lab/ (experiments)
                                │ produce candidate
                    candidate_skills/ + skill_version_context/ (baseline)
                                │ validate (True Replay)
   validation_jobs/ → validation_claims/ → validation_results/
                    → validation_evaluations/ → validation_decisions/
                                │  (skill_evidence/ records effectiveness)
                                │  (uncertain → human_review/)
                                ▼ approved
   skill_mutation_commits/ → skill_sync_outbox/ → skills/ + manifest.json

[Parallel] DreamCycle maintains team memory → memory-changes/ (change ledger)
[Isolation] peers/{account}/ holds per-user data (personal skills, etc.)
[Aggregation] viking://user/<user>/memories/
            → viking://user/<merge-user>/resources/teamEvolver/<staging_dir>/<target-hash>/ (private work data)
            → viking://resources/<shared_knowledge_prefix>/ (team Memory)
```

> Note: `skill_mutation_commits/` and `skill_sync_outbox/` form the skill-change pipeline — every publish/delete first writes a commit record, is then delivered to the sync outbox for runtimes, and finally updates `skills/` and `manifest.json`. See `team_skills/library/mutations.py`.

## Console Visualization

**Asset Center → Personal and Team Assets** exposes two spaces directly: Personal Memory at `viking://user` and Team Resources at `viking://resources`. It supports browse mode, edit mode, multi-file Diff review, and conditional batch writes. Administrators can still use the OpenViking CLI there, and self-hosted deployments show a Studio link. Skill Lab and Memory Lab are no longer standalone pages; dataset selection and True Replay debugging remain in the Experiment Workbench.

**Asset Center → Platform Assets** uses a separate storage visualization. It shows read-only Session, Candidate, Validation, and Evidence objects from PostgreSQL and NAS with source filters, a logical-key tree, content preview, and category counts. It does not show the OpenViking CLI. The frontend is `web-ui/src/views/PlatformAssetsView.tsx`; the backend is `teamEvolver/proxy/platform_assets.py`.

## Code Entry Points

| Module | Path |
|--------|------|
| Scope mapping and workspace API | `teamEvolver/proxy/openviking_workspace.py` |
| Platform Assets PostgreSQL/NAS API | `teamEvolver/proxy/platform_assets.py` |
| Account registry and key resolution | `teamEvolver/proxy/users_admin.py` |
| OpenViking object store | `teamEvolver/storage/viking.py` |
| Built-in local object store | `teamEvolver/storage/local.py:LocalObjectStore` |
| Skill library async mirror (spool + flusher) | `team_skills/library/mirror.py:VikingSkillMirror` |
| Isolation prefix `peers/` | `teamEvolver/storage/base.py` |
| Session storage | `teamEvolver/session_store.py` |
| Validation storage | `team_skills/candidates/store.py` |
| Skill mutations | `team_skills/library/mutations.py` |
| DreamCycle memory changes | `team_memory/memory_changes.py` |
| Cross-user team-Memory aggregation | `team_memory/service.py` |
| Endpoint resolution (cloud/local) | `teamEvolver/config.py` |

## Related Docs

- [Architecture](./01-architecture): where storage sits in the overall architecture
- [Evolution Loop](./02-evolution-loop): how the directories drive evolution
- [Sessions](./05-sessions): the detailed session-pipeline structure
- [True Replay](./06-true-replay): how the validation directories are used
- [Memory](./04-memory): memory spaces and DreamCycle
