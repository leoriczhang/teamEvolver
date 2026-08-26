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

Reports carry integration-scoped tokens and `external_subject`; the server validates identity mapping then writes to queue.

### 2. Evidence Extraction

The evolution engine's Judge stage analyzes Sessions to determine which content can be elevated to team assets:

| Evidence Type | Destination |
|---------------|-------------|
| Reusable task methods | Skill Candidate |
| Long-term facts/preferences/consensus | Memory Change (via DreamCycle) |
| Task-specific requirements | Discarded (not team assets) |
| Agent runtime issues | Marked as runtime-issue, no evolution |
| Insufficient evidence | Archived, awaiting more Evidence accumulation |

### 3. Candidate Generation

When Evidence of the same type accumulates to threshold (`evidence_change_debt_threshold=3`):
- **Skill Candidate**: Based on successful patterns across multiple Sessions, merged into a Skill revision or new version
- **Memory Change**: Generated after DreamCycle's React engine aggregates and deduplicates

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
