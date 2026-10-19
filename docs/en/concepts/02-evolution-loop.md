# Evolution Loop

The evolution loop turns real Agent work into team Skill versions: Session intake → mandatory analysis → Skill Evolution generates Skill Candidates → True Replay validation → Candidate Review and Publish → Agent pull. Every step is a persisted Worker task that any API or Worker replica can pick up.

## Loop Overview

```text
 Agent push / data source pull
        │ 202 + session.normalize
        ▼
 normalize → split → analyze → trace_analyze → archive ──► formal Session + Evidence
                                                     │ POST /api/skill-evolution/trigger
                                                     ▼
                                     skill.evolve: plan per Skill group → Skill Candidate
                                                     │ auto_evaluate or manual evaluate/validate
                                                     ▼
                                     skill.validate: static checks + True Replay
                                                     │ accept / reject / inconclusive
                                                     ▼
                         Candidate Review ──► Publish (new version) ──► GET /sync/skills
```

## Stages

### 1. Session Intake

Agents push v2 Sessions with `POST /ingest_session`, or administrators pull them from data sources. In one transaction the API writes `ingestions`, the raw payload and a `session.normalize` (or `session.pull`) task, then returns `202`. Repeated pushes of the same `session_id` map to the first acceptance. See [Sessions](./05-sessions.md).

### 2. Mandatory Analysis

The Worker runs `normalize`, `split`, `analyze`, `trace_analyze` and `archive` in order; each stage writes a `session_stage_results` row. `split` uses one model call to identify contiguous topic boundaries. A multi-topic Session fans out into child ingestions carrying source metadata, and each child runs the complete pipeline independently. The immutable split plan is stored before fan-out, so retries reuse the same boundaries. Only after all five stages succeed does `archive` write the formal `sessions` row; a failure at any stage produces no formal Session or Evidence.

`analyze` is done by `team_skills/evolution/stages/analyze.py:analyze_session` in a single model call covering value classification, summary and the four scoring dimensions:

| Dimension | Weight | Meaning |
|------|------|------|
| `task_completion` | 0.55 | Whether the user's goal was met |
| `response_quality` | 0.30 | Correctness, completeness and clarity of the final result |
| `tool_usage` | 0.10 | Whether tools were used appropriately and effectively |
| `efficiency` | 0.05 | Whether the path avoided unnecessary retries or detours |

The system prompt is `_ANALYZE_SYSTEM` and can be overridden per tenant in Prompt Studio.

### 3. Skill Evolution

An administrator calls `POST /api/skill-evolution/trigger` (optional `session_ids`, at most 200); the API writes a `skill.evolve` command pinned to the current `skills` module configuration revision. The Worker runs `team_skills/evolution/cycle.py`:

- **Frozen input**: on first execution it selects, per `evolution_max_sessions` (default 100, max 200), Sessions stored before acceptance and not yet consumed, and saves the input set; a resumed run uses the same Sessions.
- **Grouping**: Sessions are grouped by related Skill and each group is an independent branch; Sessions without a related Skill go to a new-Skill branch.
- **Team evidence threshold**: each branch's evidence must cover at least `min_group_sessions` (default 2) Sessions and `min_group_users` (default 2) users, otherwise the branch is skipped this cycle.
- **Planning**: the model decides `skip`, revise or create based on grouped Sessions, cross-cycle Evidence and successful experience patterns. Evidence routing rules are `_EVIDENCE_ROUTING_RULES` in `team_skills/evolution/stages/execute.py`; only `team_skill` evidence may change a shared Skill.
- **Concurrent Dataset synthesis**: once the evidence threshold passes, planning and Test Dataset synthesis start together from the same Evidence. Synthesis does not read this round's proposal or its classification. Both must finish before the Candidate is written and validation is submitted. A skipped proposal cancels unfinished synthesis; a failed branch leaves Evidence unconsumed, and retries reuse the frozen input.
- **Candidate generation**: writes `skill_candidates` and their files, recording Evidence Classification, edit summary, file changes, bundle diff and static check results; published versions are unaffected. If someone modified a Candidate of the same Skill during planning, the proposal is discarded.

### 4. Test Dataset

Test Dataset synthesis runs independently alongside Skill planning. Each Candidate carries two Replay Cases derived from the same Evidence, each with `query`, `skill_ids`, 6–8 `checks` (Checklist) and `materials`. A new Skill's name is bound to the Cases after planning finishes; True Replay starts only when both branches are ready. Datasets can also be selected or synthesized from Sessions on the "Datasets" page; see [True Replay](./06-true-replay.md).

### 5. Validation

With `auto_evaluate` (default `true`) in the `skills` configuration, `evaluate` is submitted automatically after a Candidate is generated; administrators can also call:

| Endpoint | mode | Result |
| --- | --- | --- |
| `POST /api/skill-candidates/{id}/evaluate` | `evaluate` | Evaluate only, no publish |
| `POST /api/skill-candidates/{id}/validate` `{"mode":"auto"}` | `auto` | Publishes when static checks and the runtime gate pass and the verdict is accept; rejects on reject |
| `POST /api/skill-candidates/{id}/validate` `{"mode":"force"}` | `force` | Publishes when static checks and the runtime gate pass, without requiring accept |

`skill.validate` pins the Candidate revision, the Baseline version, the `replay` configuration revision and the tenant-bound Replay Adapter revision, then runs static checks and True Replay. When static checks fail the verdict is always reject; when the runtime gate fails, accept is downgraded to inconclusive.

### 6. Candidate Review and Publish

Administrators review Evidence, diff, static checks and True Replay results in the console, and can:

- Edit content: `PUT /api/skill-candidates/{id}/content` (with `candidate_revision` for optimistic locking)
- Mark feedback: `PATCH /api/skill-candidates/{id}/feedback`
- Reject: `POST /api/skill-candidates/{id}/reject`
- Publish: `validate` in `auto` or `force` mode

Publish writes the new `skill_versions`, files and `skill_mutations` in one transaction; see [Publish and Rollback](./08-publish-rollback.md).

### 7. Distribution

Agents request `GET /sync/skills?user_id=...` before model calls; with `If-None-Match` an unchanged snapshot returns `304`. Skills are distributed only by this pull; publishing does not push to Agents.

### 8. Rollback

`POST /api/skills/{name}/rollback` restores historical content as a new version and keeps all old versions and the audit chain.

## Triggers

| Trigger | Description |
|------|------|
| Manual | Console or `POST /api/skill-evolution/trigger`, optionally with `session_ids` |
| Automatic | With `evolution_trigger_mode=automatic`, the Worker checks for unevolved Sessions every `evolution_interval_seconds` |
| Auto evaluation | With `auto_evaluate=true`, every new Candidate enters `evaluate` automatically |
| Successful experiences | The Worker periodically scans analyzed Sessions, maintains the experience library and experience patterns, and feeds them into later planning; see [Successful Experience Sync](../guides/16-successful-experience-sync.md) |

Automatic acceptance uses a PostgreSQL advisory lock and durable-task deduplication, so multiple Workers do not enqueue duplicate cycles for a tenant. Manual triggering remains available.

## Evolution Settings

`GET/PUT /api/skill-evolution/settings` reads and writes these fields of the `skills` module configuration (`revision` optimistic lock):

| Field | Default | Description |
| --- | --- | --- |
| `evolution_max_sessions` | 100 | Max Sessions consumed per evolution (cap 200) |
| `min_group_sessions` | 2 | Minimum Sessions per branch; 0 disables the check |
| `min_group_users` | 2 | Minimum users per branch; 0 disables the check |
| `evolution_trigger_mode` | manual | `manual` trigger or `automatic` periodic trigger |
| `evolution_interval_seconds` | 600 | Automatic trigger interval, 60–604800 seconds |
| `auto_evaluate` | true | Whether new Candidates are evaluated automatically |
| `publish_mode` | manual | `manual` waits for human review after validation; `automatic` publishes accepted validation automatically |
| `bundle_allow_delete` | true | Whether evolution may delete bundle files |
| `bundle_edit_max_bytes` | 262144 | Per-file edit limit |
| `bundle_prompt_max_bytes` | 786432 | Bundle size limit sent to the model |

## Related Docs

- [Skills](./03-skills.md): Skill structure, versions and lifecycle
- [True Replay](./06-true-replay.md): validation mechanism
- [Checklist Gate](./07-checklist.md): completion rules
- [Publish and Rollback](./08-publish-rollback.md): versioning and audit
- [Validation and Candidate API](../api/10-validation.md)
