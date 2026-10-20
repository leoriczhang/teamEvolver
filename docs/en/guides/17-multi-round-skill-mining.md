# Multi-round Skill Mining and Human Supplements

New default jobs use `te-native-mining.v1`. TE manages sources, tasks, human supplements and acceptance. One OV Compile per normal round uses a compiler Skill to discover, consolidate and generate complete methods and Cases. A Compile may contain multiple model calls and internal jobs.

The first round builds complete artifacts. Later rounds incrementally update the same remote target. Original materials and the compiler are frozen; changing the source directory does not alter an existing job.

## Create a job

Select a registered directory tagged for Skill use.

| Setting | Behavior |
| --- | --- |
| Maximum rounds | 1–5, default3; an upper bound rather than mandatory empty rounds |
| Human supplements | Wait for real questions; an empty question list skips supplementation and finishes mining |
| Round deadline | 60–7200 seconds, default3600; a budget protection, not an ETA |
| Free discovery | Optional direction; discover complete independent methods supported by sources |
| Single Skill | Required complete-task direction; integrate steps and scenario parameters |
| Target count | 1–100, optional direction; explain insufficient support without forced splits or merges |

Compile uses OV's models and runtime. Fewer external Compile requests do not prove fewer model calls or lower latency; output and Case volume matter.

## Capability splitting and candidate collections

The compiler uses triggers, complete steps, applicability and independent completion outcomes. People, amounts, channels and scenario numbers are usually parameters. Different customers' values are not automatically source contradictions.

Each method produces `compiled_skill/<slug>/SKILL.md`, `EVALUATION.md`, `benchmark.json` and `METHOD.json`. The receipt contains task, trigger, inputs, outputs, boundary, sources and supplement questions. TE deterministically aggregates receipts into `analysis/capability-plan.json` using `skill-miner.plan.v1`.

Each `team-replay.dataset` contains12 Cases with at least12 output conditions and one trajectory condition per Case. TE renders `BENCHMARK.md` from validated JSON without generating duplicate content or inventing conditions.

Non-method content uses `analysis/notes/*.json` for source-backed dispositions. Facts and out-of-scope content do not automatically create questions. A zero-method result retains explanations and cannot be submitted as a candidate.

Incremental consolidation declares existing members in `METHOD.json.supersedes`. TE validates references and cycles without making semantic merging decisions. Original files remain in remote storage, local lineage and round archives. The surviving method must integrate valid steps, conditions and sources.

## Review and supplements

1. Inspect methods, evaluations and Test Datasets against original conditions and scenario boundaries.
2. Answer real questions with sources; mark unknown facts unresolved.
3. Attach materials, provide feedback or edit permitted current artifacts.
4. Continue to freeze incremental inputs and submit the next Compile.

An empty question list finishes mining with a separate `skipped_checkpoints` receipt. It fabricates no human approval or Evidence. Missing or invalid receipts are not empty reviews. Skipping supplementation does not replace True Replay or Candidate Review.

When human supplementation is disabled, finish the current round while preserving unanswered questions. Do not start another round without new Evidence.

## Inputs to the next round

The same job retains the same `to`. The next `from` contains actual supplements, attachments, implicated frozen originals and changed revision context. Explicit question references select implicated originals; feedback with unknown impact conservatively includes all original material.

Unchanged artifacts are not copied into new sources. Compile recalls related history using `against=target`. Edited artifacts under `revision/` are context, never factual provenance. Old citation identities remain validated. Questions are not answers.

Every round archives inputs and complete outputs. The current workspace switches only after deterministic acceptance. Original materials, requests, raw outputs and previous versions remain available.

## Failures and recovery

- Synchronization failure preserves the task and retries with backoff.
- A lost acceptance response is reconciled by the original idempotency key; any resubmission uses the identical frozen request.
- Remote or artifact failures retain the real error, request and result. At most two whole-task repairs per round follow; remaining failures are reported honestly.
- Chinese, source and Case checks remain enforced. Unknown business facts require human supplements.

The UI reports actual Compile admissions and repairs. Saved workspace versions support switching retries without overwriting history.

## Finish and validate the result

Structural acceptance checks language, complete files, provenance, Test Dataset format and count constraints. It does not prove business correctness or True Replay success. Existing candidate selection, True Replay, Candidate Review and Publish gates remain in force. Mining never publishes automatically.

## Historical jobs

Existing jobs retain their frozen single-candidate or staged protocols without request or artifact migration. Custom compiler Skills retain their content; resetting settings enables native whole-round execution for newly created jobs.
