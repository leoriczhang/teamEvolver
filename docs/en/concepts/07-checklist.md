# Checklist Gate

A Checklist is the **flat set of completion conditions** each Replay Case in True Replay must satisfy. It is a completion gate (pass/fail), not a quality score: the Case is complete only when every item is satisfied, with no partial credit and no weighting.

## A Completion Gate, Not a Score

| Aspect | Checklist gate | Quality score |
|------|---------------|---------|
| Output | Boolean `all_satisfied` | Continuous score or grade |
| Item relationship | All must hold (AND) | Weighted sum, items can compensate each other |
| Partial completion | Any unmet item fails | Partial scores raise the total |
| Basis | Only concrete evidence of completion | Quality, elegance and similar |
| Role in decisions | Pre-gate | Efficiency is compared only after the gate passes |

## Checklist of a Single Replay Case

The Checklist comes from the `checks` field of a Test Dataset Case (`team_replay/policy.py:normalize_case_checklist`):

| Field | Description |
|------|------|
| `id` | Stable identifier, unique within the Case |
| `text` | Natural-language completion condition |
| `kind` | `output` (output requirement) or `trajectory` (process requirement) |
| `polarity` | Optional; `must_not` marks a forbidden behavior |

Items missing `id` or `text`, or with an invalid `kind`, are dropped. When Candidates are generated, the dataset synthesis step (`team_replay/datasets/synthesis.py`) produces `checks` from the same Evidence; they can also be edited on the "Datasets" page.

## Checklist Judge

After each interaction turn an independent judge evaluates the Checklist (`team_replay/judging.py:judge_checklist`):

1. **Input**: Checklist, per-turn interactions, full message trajectory and artifacts.
2. **Output**: `{items:[{id, satisfied, evidence}], all_satisfied, positive_observations}`. The output must cover every `id`, and each item needs a boolean `satisfied` and a string `evidence`.
3. **Evidence requirement**: an item with `satisfied=true` but empty `evidence` counts as unmet.
4. **Fail closed**: when the model is unavailable or the output is invalid, every item is marked unmet, the report says `judge="unavailable"`, and the branch stops.

The judge system prompt is `JUDGE_SYSTEM` and can be overridden through the Replay judge configuration.

## Progressive Disclosure

1. The first turn sends only the Case `query`.
2. After each judge evaluation, `select_disclosure_items` picks the next batch, in order, from unmet and undisclosed items (`replay.progressive_disclosure.batch_size`, default 4).
3. The user simulator sees only this batch and already disclosed requirements, and produces natural user feedback as the next turn input; feedback contains no item IDs or scores.
4. The branch ends when everything is satisfied, nothing is left to disclose, or `max_interactions` is reached.

## Aggregating Case Checklists

`team_replay/policy.py:aggregate_case_checklists` aggregates multiple Cases:

- `all_satisfied`: true only when every item of every Case is satisfied.
- `case_count`, `total`, `satisfied_count`, `unmet_count`: cross-Case counts.
- `reports`: per-Case reports.
- If the judge is unavailable for any Case, the aggregate has `judge="unavailable"`.

## How the Checklist Gates Candidates

```text
Any judge unavailable ────────────► inconclusive (checklist_judge_unavailable)
Neither completed ────────────────► inconclusive (both_checklists_incomplete)
Candidate incomplete ─────────────► reject (candidate_checklist_incomplete)
Candidate done, Baseline not ─────► accept (candidate_only_completed_checklist)
Both completed ───────────────────► efficiency comparison: turns → tool calls / tokens
```

The rules are implemented in `team_replay/policy.py:progressive_replay_decision`. The efficiency comparison is described in [True Replay](./06-true-replay.md#verdict).

## Checklist vs Quality Scoring

**The Checklist answers**: were all required outputs and steps completed? For example "was the configuration file generated", "were the tests run".

**The Checklist does not answer**:

- Whether the code is good: judged by efficiency metrics and human review.
- Whether the approach is optimal: reflected in turns, tool calls and tokens in the efficiency comparison.
- Whether edge cases are handled: the Test Dataset must contain corresponding edge Cases.

## Checklist in Memory Replay

Memory Replay uses the same judge, disclosure and verdict rules (`team_memory/replay.py:specification` converts the request into a single-Case `ReplaySpec`):

- Each Memory Replay has exactly one Case; the request must provide `query` and `checklist`. `checklist` can be an array of objects or newline-separated text, each line becoming an `output` item.
- Baseline is the Memory content before the change and Candidate the content after; the treatment revision is the content's SHA-256.
- Replays of historical Memory Changes read the before and after snapshots automatically; see [Memory](./04-memory.md#memory-change-and-true-replay).

## Code Entry Points

| Module | Path |
|------|------|
| Checklist normalization, disclosure and verdict | [policy.py](../../../team_replay/policy.py) |
| Judge and user simulator | [judging.py](../../../team_replay/judging.py) |
| Branch execution | [execution.py](../../../team_replay/execution.py) |
| Efficiency comparison | [metrics.py](../../../team_replay/metrics.py) |
| Checklist generation | [synthesis.py](../../../team_replay/datasets/synthesis.py) |
| Memory Replay input | [team_memory/replay.py](../../../team_memory/replay.py) |

## Related Docs

- [True Replay](./06-true-replay.md)
- [Evolution Loop](./02-evolution-loop.md)
- [Publish and Rollback](./08-publish-rollback.md)
