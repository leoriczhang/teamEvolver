# Checklist Gate

Checklist is a **flat completion condition set** that each Replay Case in True Replay must satisfy. It is a completion gate (pass/fail), not a quality score—Candidate must complete all Checklist items; no partial credit, no weighting, no quality scoring.

## Checklist is Completion Gate, Not Scoring

Checklist's core purpose is **determining whether task is complete**, not determining quality of completion. This is fundamentally different from quality scoring:

| Dimension | Checklist Gate | Quality Scoring |
|-----------|---------------|----------------|
| Output | Boolean (all_satisfied: true/false) | Continuous score or grade |
| Item relationship | All items must be satisfied (AND logic) | Weighted sum, can compensate each other |
| Partial completion | Any single item fails → fail | Partial item scores can raise total |
| Judgment basis | Only looks for concrete Evidence of completion | Evaluates quality, elegance, efficiency, etc. |
| Role in decision | Upfront hard gate, fail → immediate reject | Efficiency comparison only after Checklist passes |

> **Note**: Checklist Judge system prompt explicitly requires "Evaluate each checklist item using only the supplied responses, tool trajectory, and real workspace artifacts. Output JSON {items:[{id,satisfied,evidence}],all_satisfied}. Do not infer success without concrete evidence."—no completion inference without concrete Evidence.

## Single Replay Case Checklist

Each Replay Case carries a set of Checklist items, each containing:

| Field | Description |
|-------|-------------|
| `id` | Stable identifier; output items `R{NN}` (e.g., R01, R02), trajectory items `T{NN}` (e.g., T01, T02) |
| `text` | Natural language description of completion condition |
| `kind` | `output` (output requirement) or `trajectory` (execution trajectory requirement) |
| `satisfied` | (filled after evaluation) Whether satisfied, boolean |
| `evidence` | (filled after evaluation) Concrete Evidence for satisfaction or non-satisfaction |

### Checklist Item Sources

Checklist items can be set two ways:

1. **Explicit specification**: Case directly provides `checklist` array, each item containing `id`, `text`, `kind`
2. **Auto-generation**: When Case doesn't explicitly provide checklist, auto-flattened from `requirements` and `trajectory_requirements` fields:
   - `requirements` flattened to output items (R01, R02, ...)
   - `trajectory_requirements` flattened to trajectory items (T01, T02, ...)
   - Supports Markdown list format, automatically strips list prefixes and deduplicates

Flattening logic handles: lists/tuples/dicts (extracts text/requirement fields), multi-line text, Markdown list prefixes (`-`, `*`, numeric numbering, etc.).

## Checklist Judge Evaluation

After each interaction turn, Checklist Judge uses LLM to evaluate current execution state:

1. **Input**: Checklist item list, interaction history (interactions), tool trajectory (tool_trajectory), workspace artifact file list (workspace_artifacts with text preview, max 40 files)
2. **Output**: `{items: [{id, satisfied, evidence}], all_satisfied}`
3. **Conservative fallback**: When Judge unavailable, all items marked `satisfied: false`, evidence "checklist judge unavailable", judge status marked "unavailable"

Judge makes decisions based only on provided Evidence, no speculation. Evaluation results used by progressive disclosure protocol to determine which unsatisfied items to expose next round.

### Checklist in Progressive Disclosure

Progressive disclosure protocol uses Checklist evaluation results to drive subsequent interaction:

1. After first round execution, Judge evaluates which items satisfied
2. Takes batch (batch_size, default 4) of unsatisfied and undisclosed items as next round prompt
3. Agent addresses these unsatisfied items
4. Repeats until all items satisfied or no more items to disclose

Per-round disclosure prompt format:
```
Round N Checklist check still has unsatisfied items.
Preserve completed content, only address following requirements:
1. [R01] Specific requirement text
2. [T01] Specific requirement text
...
After completion, re-examine existing artifacts and provide latest results.
```

## Aggregate Case Checklist

One Skill Candidate's True Replay contains multiple Replay Cases (from Test Dataset). All Cases' Checklist results aggregate to candidate version's overall Checklist result:

- **Per-Case evaluation**: Each Case runs Baseline and Candidate branches independently, each gets Checklist report
- **Aggregation logic**: `aggregate_case_checklists` summarizes all Case results
  - `all_satisfied`: **All** Checklist items across **all** Cases must be satisfied for true (AND logic)
  - `case_count`: Number of Cases with Checklist items
  - `total`/`satisfied_count`/`unmet_count`: Cross-Case aggregated item statistics
  - `reports`: Detailed Checklist report per Case

> **Note**: Aggregation is strict—if even one Case has one Checklist item unsatisfied, entire Candidate's Checklist gate fails. This ensures Candidate not accepted under known-failure scenarios.

## How Checklist Gates Candidate Acceptance

Checklist results together with efficiency comparison determine whether Candidate passes; decision priority:

```
Checklist Gate ──► Efficiency Comparison
    │                  │
    ├─ Candidate fails ──► Reject immediately (candidate_checklist_incomplete)
    │
    ├─ Candidate passes, Baseline fails ──► Accept immediately (candidate_only_completed_checklist)
    │
    └─ Both pass ──► Enter efficiency comparison (turns→tool calls→Token)
```

Specific decision rules implemented in `progressive_replay_decision`, strategy ID `progressive_checklist_then_turn_priority_v1`:

| Scenario | Decision | decision_basis |
|----------|----------|----------------|
| Candidate has Checklist but not all satisfied | Reject | `candidate_checklist_incomplete` |
| Candidate passes, Baseline has Checklist but fails | Accept | `candidate_only_completed_checklist` |
| Both pass or both have no Checklist | Enter efficiency comparison | Determined by objective_replay_decision |
| Checklist Judge unavailable | All items considered unsatisfied → Reject | Conservative fail-closed |

## Checklist vs Quality Scoring

Understanding Checklist's boundaries is important:

**Questions Checklist answers**: Are all required outputs and steps for the task completed?
- E.g.: "Was config file generated?", "Were tests run?", "Was PR created?"
- Only judges existence and completion, not goodness

**Questions Checklist does NOT answer**:
- Is generated code high quality? → This is efficiency metrics (fewer turns/tool calls imply more direct solutions) and human review's responsibility
- Is solution optimal? → Efficiency comparison can reflect优劣 on turn/Token dimensions
- Are edge cases handled? → This requires Test Dataset containing corresponding edge Cases

This design ensures objectivity and reproducibility of automatic validation: completion is judgeable fact, quality assessment left to humans or more complex mechanisms.

## Checklist in Memory Replay

DreamCycle's Memory True Replay also uses Checklist gate, but at smaller scale:

- Each Memory Replay has only one Case (not multiple Cases from Test Dataset)
- Checklist passed by caller; minimum 1 item, maximum 50 items
- Validation logic identical to Skill Replay: Baseline and Candidate execute in parallel, Checklist gate takes priority over efficiency comparison
- Requires before and after Memory content hashes differ (`before_hash != after_hash`), otherwise treated as no change and errors directly

## Code Entry Points

| Module | Path |
|--------|------|
| Progressive disclosure & Checklist decisions | [progressive_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/progressive_replay.py) |
| Checklist item generation | [dataset_synthesizer.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dataset_synthesizer.py) |
| Local Checklist Judge | [true_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/true_replay.py) (`_evaluate_local_checklist`) |
| Efficiency comparison decisions | [replay_metrics.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/replay_metrics.py) |
| Memory Replay Checklist | [dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py) |

## Related Documentation

- [True Replay](./06-true-replay): Checklist role in True Replay and progressive disclosure protocol
- [Evolution Loop](./02-evolution-loop): Checklist gate position in evolution loop
- [Publish & Rollback](./08-publish-rollback): Publishing process after passing gate
