# Storage Spaces and Directory Layout

All of teamEvolver's persistent data lives in the OpenViking backend. This document explains two things: how a teamEvolver **account maps to OpenViking spaces**, and **what every directory/file under the team Workspace root is for**. It is the foundation for understanding the evolution-loop data flow.

## Account ↔ OpenViking Space Mapping

A teamEvolver account does not own a dedicated OpenViking tenant. Instead it resolves into two things: an **API key (authentication) + a URI path (location)**. Each account maps to 6 scopes, split into personal and team categories.

The mapping is defined in `_scope_map()` in `teamEvolver/proxy/openviking_workspace.py`.

| Scope | OpenViking root URI | Space | Kind | Writable by regular user |
|-------|---------------------|-------|------|--------------------------|
| `personal_memory` | `viking://user/{personal user}/memories` | personal | memory | ✅ |
| `personal_skills` | `viking://resources/team-skill-evolver/peers/{account}/skills` | personal | skills | ✅ |
| `personal_workspace` | `viking://user/{personal user}` | personal | workspace root | ✅ |
| `team_memory` | `viking://resources/shared-knowledge` | team | memory | ❌ admin only |
| `team_skills` | `viking://resources/team-skill-evolver/skills` | team | skills | ❌ admin only |
| `team_workspace` | `viking://resources/team-skill-evolver` | team | workspace root | ❌ admin only |

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
- **Skills and shared resources** live in the `viking://resources/{root_prefix}/` namespace, shared by the team.
- **Personal skills** are isolated inside the shared namespace via a `peers/{account}/` path segment — see `peer_key_prefix()` in `teamEvolver/storage/base.py`.

### API keys and identity headers

Credentials used when calling OpenViking are resolved in `_workspace_headers()` in `teamEvolver/proxy/openviking_workspace.py`:

| Space | API key resolution order |
|-------|--------------------------|
| team | user `team_space` key → admin service key (inherited) → `sharing.viking_team_api_key` (compatibility field; semantically the service/admin key) → `sharing.viking_api_key` |
| personal | user `personal_space` key → `sharing.viking_personal_api_key` → `sharing.viking_api_key` |

Three identity headers are always sent: `X-OpenViking-Account` (default `default`), `X-OpenViking-User` (personal = account ID, team = `team`), and `X-OpenViking-Agent` (`team-skill-evolver`). **When the API key is empty (e.g. a locally self-hosted OpenViking), the `X-API-Key` and `Authorization` headers are omitted** and isolation relies on the three identity headers alone; the URI mapping is identical for local and cloud, differing only by endpoint (see `resolve_viking_endpoint()` in `teamEvolver/config.py`).

Regular users need not configure a service/admin key. The service directly reuses the admin-configured OpenViking key as the service key; regular users inherit only server-mediated access to team assets, not the plaintext key. The compatibility implementation lives in `_effective_team_key()` in `teamEvolver/proxy/users_admin.py`.

## Team Workspace Directory Map

The team Workspace (`viking://resources/team-skill-evolver/`) is the shared database for the whole evolution loop. The entries under its root fall into 7 functional groups.

### 1. Skill library (finished artifacts)

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `skills/` | dir | Official team skill library; one subdirectory per skill (`skills/<name>/SKILL.md` + versions). Pi Agent / Hermes read team skills from here | `teamEvolver/skills/hub.py` |
| `manifest.json` | file | Skill manifest index: skill name → version/hash, used to diff local vs. remote | `teamEvolver/skills/hub.py` |
| `evolve_skill_registry.json` | file | Skill ID registry keeping IDs stable across nodes | `teamEvolver/skills/registry.py` |

### 2. Skill lab and evolution material

This backs the "data-driven evolution" loop: mine datasets from historical sessions, generate matching test sets, and validate effectiveness.

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `skill_lab/` | dir | Skill lab. `skill_lab/datasets/<id>/` holds datasets, `skill_lab/runs/<id>/` holds experiment run results | `teamEvolver/skill_lab.py` |
| `skill_datasets/` | dir | Skill test sets, organized as `skill_datasets/<skill>/<dataset>` | `teamEvolver/dataset_store.py` |
| `evolution_datasets/` | dir | Evolution datasets synthesized from historical sessions | `teamEvolver/dataset_synthesizer.py` |
| `skill_evidence/` | dir | Skill effectiveness evidence (`<skill>.json`): injection counts, effectiveness, and other evolution-decision inputs | `teamEvolver/evolve/runtime/evidence.py` |
| `skill_version_context/` | dir | Per-version skill context (`<skill>/v<N>.json`) used as a True Replay baseline | `teamEvolver/validation/store.py` |

### 3. Session pipeline (evolution raw material)

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `sessions/` | dir | Pending session queue (`<session_id>.json`); removed after the evolution engine consumes it | `teamEvolver/session_store.py` |
| `session_archive/` | dir | Permanent session archive | `teamEvolver/session_store.py` |
| `session_filter_audit/` | dir | Session filter-decision audit (why queued/skipped) | `teamEvolver/session_store.py` |
| `session_ledger/` | dir | Session ledger recording the queued→consumed lifecycle transitions | `teamEvolver/evolve/runtime/orchestrator.py` |
| `session_index.json` | file | Session metadata index (title, turns, tokens, status) for fast console browsing | `teamEvolver/session_store.py` |

### 4. Evolution validation (True Replay loop)

Rules in `teamEvolver/validation/store.py`.

| Entry | Type | Purpose |
|-------|------|---------|
| `candidate_skills/` | dir | Candidate-skill staging (`<job_id>/SKILL.md` + files), not yet promoted to `skills/` |
| `validation_jobs/` | dir | Validation jobs (`<job_id>.json`) produced by the evolution service |
| `validation_claims/` | dir | Job claim locks (`<job_id>/<user_alias>.json`) preventing duplicate validation |
| `validation_results/` | dir | Per-client independent validation results (`<job_id>/<user_alias>.json`) |
| `validation_evaluations/` | dir | Aggregated evaluation of multiple results (`<job_id>.json`) |
| `validation_decisions/` | dir | Final publish/reject decision (`<job_id>.json`) |
| `validation_decision_index.json` | file | Decision index for fast lookup |

### 5. Human review

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `human_review/` | dir | Human-review task queue (`<job_id>.json`): escalated when an automated decision is uncertain | `teamEvolver/validation/store.py` |

### 6. DreamCycle team-memory maintenance

| Entry | Type | Purpose | Code entry |
|-------|------|---------|------------|
| `memory-changes/` | dir | Memory-change ledger (`teamevolver.memory-change.v1`): recorded when DreamCycle dedups/cleans/consolidates memory, enabling True Replay of memory edits | `teamEvolver/dreamcycle/memory_changes.py` |

### 7. Isolation and low-level structure

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
```

> Note: `skill_mutation_commits/` and `skill_sync_outbox/` form the skill-change pipeline — every publish/delete first writes a commit record, is then delivered to the sync outbox for runtimes, and finally updates `skills/` and `manifest.json`. See `teamEvolver/skills/mutations.py`.

## Console Visualization

In the console under "Assets → Context Space", switch to the **Team Workspace**; the file tree shows inline Chinese purpose descriptions for the known directories above, so the layout is self-explanatory while browsing. The frontend implementation is `web-ui/src/views/OpenVikingWorkspaceShell.tsx`. Admins can also inspect data directly via the built-in OpenViking CLI and Studio entry on the same screen.

## Code Entry Points

| Module | Path |
|--------|------|
| Scope mapping and workspace API | `teamEvolver/proxy/openviking_workspace.py` |
| Account registry and key resolution | `teamEvolver/proxy/users_admin.py` |
| OpenViking object store | `teamEvolver/storage/viking.py` |
| Isolation prefix `peers/` | `teamEvolver/storage/base.py` |
| Session storage | `teamEvolver/session_store.py` |
| Validation storage | `teamEvolver/validation/store.py` |
| Skill mutations | `teamEvolver/skills/mutations.py` |
| DreamCycle memory changes | `teamEvolver/dreamcycle/memory_changes.py` |
| Endpoint resolution (cloud/local) | `teamEvolver/config.py` |

## Related Docs

- [Architecture](./01-architecture): where storage sits in the overall architecture
- [Evolution Loop](./02-evolution-loop): how the directories drive evolution
- [Sessions](./05-sessions): the detailed session-pipeline structure
- [True Replay](./06-true-replay): how the validation directories are used
- [Memory](./04-memory): memory spaces and DreamCycle
