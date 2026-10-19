# True Replay

For each Test Dataset Case, True Replay opens isolated Baseline and Candidate sessions with the same query, materials, Context, model, tools and limits; the only difference is the treatment (the Skill set or Memory content under validation).

## Execution

Each branch is run by `team_replay/execution.py:run_branch`:

1. The tenant-bound ReplayAdapterFactory opens an independent session; the first turn sends only the dataset query.
2. Real Agent replies, trajectory, artifacts and metrics are collected (`AgentObservation`).
3. An independent judge (`team_replay/judging.py:judge_checklist`) gives per-item evidence against the Checklist and decides the completion gate.
4. When incomplete, `team_replay/policy.py:select_disclosure_items` deterministically selects the unmet items that may be disclosed this round (default 4 per batch), and an independent user simulator (`render_user_feedback`) produces natural feedback.
5. The feedback is appended to the user history and execution continues; the branch ends and closes its session when everything is met or `max_interactions` is reached.

Customer adapters never see the Checklist, judge state or undisclosed requirements. Feedback only states the current goal and gaps, with no item IDs, scores or A/B internal wording. When the judge is unavailable the run fails closed.

## Verdict

`team_replay/policy.py:progressive_replay_decision` looks at the Checklist first, then efficiency:

| Situation | verdict | decision_basis |
| --- | --- | --- |
| Judge unavailable on any branch | inconclusive | `checklist_judge_unavailable` |
| Neither Checklist completed | inconclusive | `both_checklists_incomplete` |
| Candidate incomplete | reject | `candidate_checklist_incomplete` |
| Candidate complete, Baseline incomplete | accept | `candidate_only_completed_checklist` |
| Both complete | Efficiency comparison | See below |

The efficiency comparison (`team_replay/metrics.py:objective_replay_decision`) uses interaction turns as the primary metric and tool calls and total tokens as secondary metrics:

| Condition | verdict |
| --- | --- |
| `interaction_turns` decreases | accept |
| `interaction_turns` increases | reject |
| Turns equal, secondary metrics improve with no regression | accept |
| Turns equal, a secondary metric regresses | reject |
| Everything equal or metrics unavailable | inconclusive |

Missing metrics are recorded as `unavailable`, never filled with 0. Each verdict keeps the evidence, conversation, disclosure record and Adapter revision.

## Entry Points

| Entry | Task | Description |
| --- | --- | --- |
| Candidate validation | `skill.validate` | Pins Candidate, Baseline version, `replay` configuration and Adapter revision |
| Dataset run | `dataset.run` | `POST /api/datasets/{id}/runs`, freezes the dataset revision and current Skill versions |
| Explicit Replay | `replay.run` | `POST /api/replays/{skill\|memory}`, the caller supplies the full Baseline/Candidate treatment |
| Experiment workbench | `replay.run` | `POST /api/replay-lab/runs`, runs against an unsaved Candidate `SKILL.md` |
| Memory Replay | `memory.replay` | See [Memory](./04-memory.md) |
| Adapter test | `replay.run` | `POST /api/replay-adapter/code/test`, service Root Key only |

Every run first persists its complete input (viewable via `GET /api/replays/{domain}/{run_id}/input`) before executing; after a Worker reclaims it, it resumes from completed Cases.

Candidate validation checkpoints each Case and executes Cases with `replay.concurrency` (1–2); each Case runs Baseline and Candidate in parallel. `GET /api/replays/skill/{run_id}` returns `total`, `completed`, `succeeded`, `failed` and individual Case states. Branch timeouts are recorded as failures and remaining Cases continue; reclaimed tasks do not execute recorded Cases again. Binding a new Adapter does not rerun historical candidates. Submit candidate `evaluate` again to pin the new revision.

Candidate validation uses two current Cases and up to three historical Cases. The model derives each current Case from a common task pattern supported by at least two different Sessions, gives it a 6–8 item Checklist, and never converts every Session one by one. The historical pool includes evolution Datasets and regression Datasets marked with `metadata.enabled_for_evolution`; ordinary experiments are excluded. Historical sampling is without replacement, excludes tasks already represented by the current Cases, and covers different Datasets first. When fewer than three Cases are available, all available Cases are used. Dataset revisions, materials and the selection are frozen at acceptance; Worker retries never resample.

Candidate Review's Dataset evidence section displays each Dataset's verdict, three efficiency metrics, and every Case's A/B conversation, Checklist evidence, tool trajectory and artifacts. `replay.datasets` stores per-Dataset results and frozen inputs; `replay.sampling` records the selection counts.

## Concurrency and Limits

`replay` module configuration (`tenant_configs`):

| Field | Default | Description |
| --- | --- | --- |
| `concurrency` | 2 | Concurrent Replay slots per tenant, controlled by `resource_leases` |
| `timeout_seconds` | 600 | Per-branch timeout |
| `max_interactions` | 4 | Max interaction turns per branch |
| `llm_model_id` | Empty | Model for the judge and user simulator; empty uses the tenant model configuration |

The configuration must not contain key or password fields.

## Test Dataset

Datasets use the `team-replay.dataset` format; the top-level `skills[]` declares the related Skill set, and each Case contains:

| Field | Description |
| --- | --- |
| `case_id` | Case identifier |
| `query` | First-turn input |
| `skill_ids` | Subset of Skills actually installed for this Case |
| `checks` | Checklist items: `id`, `text`, `kind` (`output` / `trajectory`), optional `polarity: must_not` |
| `materials` | Input files |
| `provenance` | Source Session and similar |
| `replay` | Optional `progressive_disclosure.batch_size` |

Datasets are stored as immutable revisions (`dataset_revisions`) and edits use `expected_revision` optimistic locking. Endpoints are in [ReplayAdapterFactory and Replay API](../api/05-replay-branch.md).

## Related Docs

- [Checklist Gate](./07-checklist.md)
- [Evolution Loop](./02-evolution-loop.md)
- [ReplayAdapterFactory and Replay API](../api/05-replay-branch.md)
