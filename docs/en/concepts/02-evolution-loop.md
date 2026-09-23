# Evolution Closed Loop

The evolution closed loop is teamEvolver's core operating mechanism: collecting Sessions from real Agent work → extracting reusable experience → generating candidate improvements → validating in real isolated environments → reviewing and publishing → distributing to Agents, forming a continuously enhancing cycle.

## Loop Overview

```
   ┌──────────┐     ┌──────────┐     ┌──────────┐
   │ Session  │────►│ Evidence │────►│Candidate │
   │  Ingest  │     │ Extract  │     │ Generate │
   └──────────┘     └──────────┘     └────┬─────┘
        ▲                                 │
        │                                 ▼
   ┌────┴─────┐     ┌──────────┐     ┌──────────┐
   │  Publish │◄────│  Review  │◄────│ Validate │
   │ & Sync   │     │  Gate    │     │(TrueReplay)
   └──────────┘     └──────────┘     └──────────┘
```

## Stage Details

### 1. Session Ingest

After each session concludes, the Agent reports the complete trajectory via `/ingest_session`:
- Complete message sequence (system/user/assistant/tool)
- Tool calls and tool results
- Injected and used Skill lists
- Efficiency metrics (turns, tool call count, Token consumption)
- Context references used

Reports carry a tenant Key and runtime_context.user_id; the server binds the tenant principal before queueing.

### 2. Evidence Extraction

Evidence extraction happens in two layers at different stages:

**Session-level analysis (Analyze stage)**: Before persistence, the merged analyzer (`team_skills/evolution/stages/analyze.py:analyze_session`, system prompt in the module constant `_ANALYZE_SYSTEM`) performs value classification, summarization, and four-dimensional scoring (0.0-1.0) in one model call:

| Dimension | Weight | Meaning |
|-----------|--------|---------|
| `task_completion` | 0.55 | Whether the user's goal was accomplished |
| `response_quality` | 0.30 | Correctness, completeness, and clarity of the final result |
| `efficiency` | 0.05 | Whether the execution path avoided unnecessary retries/detours |
| `tool_usage` | 0.10 | Whether tool usage was appropriate and effective |

The weighted result is `overall_score`; the output also carries per-dimension scoring bullets (`reasons`, a Chinese bullet list) and an overall `rationale`. Every non-empty Session must complete merged classification, summary, and scoring before ingest. Existing benchmark/aggregate scores are input evidence and do not bypass analysis. A later evolution cycle only reuses a Session when complete `_summary` and `_judge_scores` outputs have already been persisted.

**Evidence routing (inside evolution prompts)**: There is no standalone "evidence classification" stage. During candidate generation, the evolution prompt (`team_skills/evolution/stages/execute.py:evolve_skill_from_sessions`, routing rules in the module constant `_EVIDENCE_ROUTING_RULES`) requires every candidate observation to be assigned to exactly one bucket:

| Bucket | Meaning |
|--------|---------|
| `team_skill` | Reusable SOPs, stable environment facts, tool/domain operating procedures |
| `user_memory` | Preferences or habits attributable to an individual user |
| `task_requirement` | Explicit requirements or corrections for the current deliverable only |
| `agent_runtime` | Runtime issues such as interruptions, context loss, tool failures, orchestration failures |
| `insufficient_evidence` | Observations with no demonstrable causal link to a Skill |

Only `team_skill` evidence can modify shared Skills; if all observations fall into the other buckets, evolution chooses `skip` and the Session is archived. The former DreamCycle Memory routing has been superseded: sessions whose ingest classification is not `valuable` are skipped and archived directly (see [Sessions](./05-sessions)).

### 3. Candidate Generation

In each evolution cycle, the engine groups consumed Sessions by their associated Skill; each group is an independent branch that runs its own evolution pass (`team_skills/evolution/runtime/orchestrator.py:_evolve_skill_group`), producing a revision, a new Skill, or a `skip` decision based on the group's Sessions plus the cross-cycle Evidence ledger. There is no "accumulate evidence to a threshold to trigger candidates" mechanism; `evidence_change_debt_threshold` only feeds cross-cycle guidance in the Evidence ledger and is not a candidate-generation threshold.

Branch-level constraints:

- **Team-evidence minima**: Each branch's planning evidence must span at least `evolve.min_group_sessions` (default 2) distinct Sessions and `evolve.min_group_users` (default 2) distinct Users (`team_skills/evolution/kernel/settings.py:EvolveServerConfig`); otherwise the branch is skipped this cycle. Setting 0 disables a check.
- **Parallelism cap**: The number of concurrently evolving group branches plus the no-skill create branch is bounded by `evolve.max_parallel_groups`.
- **Partial commit**: When a branch fails, its Sessions stay queued for retry on a later cycle; Sessions from successful branches are consumed and archived normally, so one permanently failing group cannot block the whole queue (`team_skills/evolution/runtime/orchestrator.py:_run_once`).

Candidate creation does not affect published team assets; they exist only in the validation queue.

### 4. Dataset Synthesis

Automatically generates test cases from homologous Evidence:
- Extracts user inputs from Sessions as test tasks
- Generates `dataset_test_cases=2` or more test cases per Candidate
- Starts validation after accumulating `dataset_min_requirements=12` cases

### 5. True Replay Validation

In the integrating Agent's real Runtime, executes each test case in parallel:
- **Baseline branch**: Loads currently published Skills
- **Candidate branch**: Loads the Skill Candidate to validate

Both share identical frozen Context (guaranteed consistent via Snapshot Hash) and run in isolated environments. Result comparison:
1. **Checklist gate**: Both Baseline and Candidate must complete all Checklist items; failures are rejected immediately
2. **Efficiency comparison**: After Checklist passes, ranked by turns → tool call count → total Token consumption; Candidate must not be inferior to Baseline

### 6. Review Gate

Candidates passing automatic validation enter admin review queue:
- Admins view Evidence, change diff, True Replay comparison results in console
- Can approve, reject, or request modifications
- Timeout (`human_review_timeout_seconds=86400`) triggers automatic handling per configuration

### 7. Publish & Sync

After review approval:
1. `SkillMutationService` transactionally commits new version (records commit history + tombstones old version)
2. Persistent outbox writes distribution queue
3. Registered Agents receive new version on next `context/skills` pull or webhook push
4. Skill Sync Adapter ensures at-least-once delivery; Agent acknowledges `{"ok": true, "results": {...}}` upon receipt

### 8. Rollback

Can rollback to historical versions at any time:
- Restores historical content as a new version (preserves version chain and audit records)
- Does not delete other versions simultaneously

## Evolution Triggers

| Trigger Method | Description |
|----------------|-------------|
| Automatic periodic | `evolve.interval_seconds=600` (10 minutes) scans queue |
| Manual trigger | `POST /trigger` executes one evolution cycle immediately |
| Session-driven | Automatically wakes when sufficient Evidence accumulates |
| Continuous drain | With `evolve.drain_max_per_cycle` set (default 0 = unlimited), when a capped drain leaves a backlog or new sessions arrive mid-cycle, the next cycle starts after roughly 1 second instead of waiting a full interval (`team_skills/evolution/runtime/orchestrator.py:run_periodic`); the drain itself reads sessions in batches (`team_skills/evolution/runtime/mixins.py:_drain_sessions`), and an empty queue still idles for the full interval |

## Publish Modes

`evolve.publish_mode` accepts exactly two values:

- `validated`: Candidates enter the validation queue. The background process may publish after result-count, approval-count, and runtime-compatibility gates pass; gray-zone results enter human review when `human_review_enabled` is on.
- `direct`: Evolution output is published directly without the Candidate validation queue.

There is no `evolve.enabled` master switch. To pause periodic scanning, stop the service or suspend the evolution process at the deployment layer rather than using an undefined setting.

## Related Documentation

- [Skill System](./03-skills): Skill structure, versioning, lifecycle
- [True Replay](./06-true-replay): Detailed explanation of validation mechanism
- [Checklist Gate](./07-checklist): Completion judgment rules
- [Publish & Rollback](./08-publish-rollback): Version management and auditing
