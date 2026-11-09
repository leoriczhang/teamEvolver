# True Replay

For each Test Dataset Case, True Replay opens isolated Baseline and Candidate sessions with the same query, materials, Context, model, tools and limits; the only difference is the treatment (the Skill set, targeted business Prompt/Context components, or Memory content under validation). A pure Prompt composition may have zero Skills and still requires verification of actual target text and the complete plan's non-target environment; see [Business Prompt and Context Evolution](./12-execution-evolution.md).

## Execution

Each branch is run by `team_replay/execution.py:run_branch`:

1. The tenant-bound ReplayAdapterFactory opens an independent session; the first turn sends only the dataset query.
2. Real Agent replies, trajectory, artifacts and metrics are collected (`AgentObservation`).
3. An independent judge (`team_replay/judging.py:judge_checklist`) gives per-item evidence against the Checklist and decides the completion gate.
4. When incomplete, `team_replay/policy.py:select_disclosure_items` deterministically selects the unmet items that may be disclosed this round (default 4 per batch), and an independent user simulator (`render_user_feedback`) produces natural feedback.
5. The feedback is appended to the user history and execution continues; the branch ends and closes its session when everything is met or `max_interactions` is reached.

Customer adapters never see the Checklist, judge state or undisclosed requirements. Feedback only states the current goal and gaps, with no item IDs, scores or A/B internal wording. When the judge is unavailable the run fails closed.

For the same interaction and evidence, the judge uses `checklist-format-retry-v1`: at most two provider calls, with one additional call only after a confirmed JSON parsing, schema validation or truncated-output format failure. The Checklist, evidence, model and request parameters stay the same, and the Agent request is not resent. Valid negative judgments, provider errors, timeouts, insufficient budget, invalid input and generic unavailability without evidence of a format failure are not retried. Usage from every actual call is accumulated, and missing usage is never filled with zero. Strict validation, no JSON repair and the original completion standards remain in force; historical results are not rewritten.

## Verdict

The default Checklist and efficiency mode uses `team_replay/policy.py:progressive_replay_decision` to look at the Checklist first, then efficiency:

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

## Optional External Scoring

The evolution pipeline and standalone experiment workbench expose Replay evaluation settings. Checklist and efficiency remain the default. Tenant administrators may instead select an external evaluation Agent and save its HTTP URL, rubric, credential Secret Ref, timeout, minimum Candidate mean score and minimum improvement. Changes affect newly accepted tasks only.

External mode still genuinely executes Baseline and Candidate and retains actual turns, tool calls and token records. After execution, each Case sends the same frozen task inputs, variables, public Context and both branches' actual output and trajectory to the external Agent. Historical scores, reference answers and Candidate editing evidence are excluded. See the [Replay API](../api/05-replay-branch.md) for the protocol.

Scores use a fixed 0–100 scale, higher is better. All selected Cases receive equal weight. Accept requires a Candidate mean at or above the configured minimum and a positive improvement at least as large as the configured minimum improvement. A decrease or unmet requirement rejects; a complete tie is inconclusive. Current and historical Cases share the same evaluator. Dataset means are displayed separately; overall scoring uses all Cases rather than a Dataset majority vote or the default per-Case efficiency veto. Required runtimes remain independently validated.

In external mode, `max_interactions=1` executes only the original Query without invoking the built-in Checklist judge or feedback generator. The Checklist is explicitly `not_run`, never passed. Multi-interaction runs retain progressive Checklist feedback; an unavailable auxiliary judge or feedback generator stops additional feedback and sends the actual execution already obtained to the external Agent without inventing completion. The default Checklist and efficiency mode still requires the built-in judge. Checklist completion and token increases are not adoption criteria in external mode. Real business execution failure, unverified treatment loading, unavailable scoring, missing branch scores, invalid scores or missing Cases cannot pass and never silently fall back to the default policy. Single-branch Dataset runs have no A/B pair and continue to report original completion results.

The mode, thresholds, rubric and endpoint configuration are frozen with the task. Reports retain request hashes, per-Case scores and reasons, means and deltas. Configuration changes never rescore historical results. External evaluator usage is recorded separately; missing usage remains unknown and is not counted as business-Agent efficiency tokens or reported as reconciled TE model usage.

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

Candidate validation generates two current Cases and samples up to three historical Cases by default. Tenant `skills` configuration can adjust these counts through `evolution_current_case_count` (1–6, default 2) and `evolution_historical_case_count` (1–30, default 3), keeping the generation and historical sampling strategies unchanged. The model derives each current Case from a common task pattern supported by at least two different Sessions, gives it a 6–8 item Checklist, and never converts every Session one by one. Recorded-anchor deduplication can reduce the effective current Case count. Split segments are grouped by `source_session_keys`; multiple segments of one original source do not count as independent sources. The historical pool includes evolution Datasets and regression Datasets marked with `metadata.enabled_for_evolution`; ordinary experiments are excluded. Historical sampling is without replacement, excludes tasks already represented by the current Cases, and covers different Datasets first. When fewer Cases are available than the configured limit, all available Cases are used. Generation uses its accepted configuration snapshot; validation acceptance freezes the historical count, Dataset revisions, materials and selection. Worker retries never resample, and existing commands without these settings retain the original defaults.

Records explicitly declared as offline import batches do not establish historical conversation continuity. Their Cases still derive Checklists across sources, but the model must select a recorded request anchor and retain its exact query and embedded history. It cannot assemble adjacent records into history or change first/subsequent business-condition triggers. Missing or invalid anchors, rewritten requests and duplicate anchors fail instead of falling back to a different summary-derived task. The request hash is retained and checked again when freezing a Test Dataset.

Business-Prompt and explicitly batched synthesis also receives complete target Evidence and source disclosures, including recorded answers and declared external evaluations. Feedback retains both its original text and any parsed JSON, including conflicts and unknowns; it is not TE Judge output or a Checklist score. Oversized complete Evidence uses lossless string sharing; inputs that still exceed the model budget fail without silent truncation. Ordinary Skill Sessions retain their existing synthesis path.

Verified `analysis_context` may supply Context-only comparison records to preserve existing successful behavior. For explicitly declared offline batches, these records supplement complete materials without changing target Evidence, value decisions, source counts or Case anchors. Targets take precedence over duplicates; conflicting source references or contents fail explicitly. Scores in Context remain original external evaluations.

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
