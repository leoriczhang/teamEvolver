# Multi-round Skill Mining and Human Evidence Supplements

The console uses teamEvolver to manage real mining rounds, with one OpenViking Compile task per round. New jobs first plan independently executable task methods and compile them into a Skill Candidate collection. When human checkpoints are enabled, a completed round waits for an administrator to review its candidate collection, answer Evidence gaps and add sources before starting the next round.

This workflow uses the model and execution environment configured in OpenViking. The console's global model settings do not select the model for Compile. The original source is frozen when the mining job starts; later changes to the source directory do not silently change the job's input.

The first round determines and saves the primary language of the frozen original sources; predominantly Chinese materials use Simplified Chinese. Candidate prose, evaluation notes, case Queries, Checklist text, shared analysis and review questions retain that language across rounds, regardless of earlier English artifacts or the language of later supplements. Field names, IDs, file paths, source URIs, code, fixed enum values and direct source quotations remain unchanged, as do structural labels such as `Checklist`, `Evidence` and `Materials`.

The console has no separate output-language selector. The runtime adds the job's language rules to new Compile requests while preserving saved custom business instructions verbatim, without a reset. Frozen accepted requests and completed artifacts are not changed retroactively, automatically translated or recompiled when the language rules are updated.

## Create a job

Select a directory tagged for Skill use on the source page, then create a Skill mining job.

| Setting | Behavior |
| --- | --- |
| Maximum mining rounds | 1–5, default 3; each round has a separate Compile task |
| Human Evidence supplements | When enabled, every completed round requires human review, including the final round |
| Runtime limit per round | 60–7200 seconds, default 3600; waiting for human input is excluded |

With human checkpoints disabled, teamEvolver executes the configured number of rounds automatically. Each round still retains separate inputs, artifacts and execution records. At most 8 Compile mining tasks execute concurrently; jobs waiting for human input do not consume an execution slot.

## Capability planning and candidate collections

The model determines candidate count from task triggers, inputs, outputs and boundaries. Each candidate must be independently executable and independently verifiable through True Replay. Document, chapter and topic counts do not determine the number of candidates. One document may support multiple methods, and one method may draw on multiple documents. Merge overlapping methods and keep workflow steps together when they cannot be used independently.

Each round allows 0–8 candidates. Eight is an execution bound, not a count to fill. Pure facts or insufficient Evidence may produce zero candidates, with deferral reasons. The result is a collection of Skill Candidates awaiting validation; individual candidates enter the existing Skill library after passing their own gates.

Start with the shared `analysis/capability-plan.json` (`schema_version: "skill-miner.plan.v1"`). Its `skills` entries describe `name`, `task`, `trigger`, `inputs`, `outputs`, `boundary`, `rationale` and `source_refs`. The `deferred` entries retain `reason` and `source_refs` for deferred content. The plan explains which methods are separated or merged and which sources do not yet support an executable method.

Each round has four shared analysis files: `evidence-map.md`, `gaps.md`, `review.json` and `capability-plan.json`. Every candidate has its own `SKILL.md`, `EVALUATION.md`, `BENCHMARK.md` and `benchmark.json` under `compiled_skill/<skill-name>/`. The JSON file is that candidate's Test Dataset. A zero-candidate round retains only shared analysis; continue with supplements or finish without submitting a candidate.

The console groups artifacts by Skill Candidate. Each card shows the capability description, that candidate's structural validation result and its four files. Shared analysis, deferred content and Evidence questions for the whole job appear separately. Historical rounds use the same grouping and are read-only. Passing structural validation does not confirm that business facts are correct or that True Replay has passed.

Existing jobs retain their original single-candidate, seven-file contract during recovery and continuation. New jobs using the current default compiler Skill use the multiple-candidate contract. If a previously saved custom compiler still uses the single-candidate contract, the system displays a compatibility warning and continues with one candidate. Reset to the current default compiler Skill in mining settings to enable capability planning for subsequently created jobs.

Each candidate undergoes its own package and Test Dataset structural validation. A missing `SKILL.md` or another mandatory file marks that candidate incomplete and prevents its submission. Other complete candidates that pass structural validation may continue through their own validation workflow.

## Review and supplement Evidence

After a round finishes, the console presents its artifacts and structured questions from `analysis/review.json`. These questions correspond to the Evidence gaps in `analysis/gaps.md`. A question has its own identifier, question text, explanation, source references and required flag.

1. Review the capability plan and each candidate's `SKILL.md`, `EVALUATION.md`, `BENCHMARK.md` and `benchmark.json`. Each JSON file is a separate Test Dataset in the shared `team-replay.dataset` format, with 12–24 cases for that candidate's True Replay.
2. Answer the questions and optionally attach references to accessible OpenViking sources. Explicitly mark questions that cannot be answered as unresolved.
3. Upload supplementary documents and provide review feedback where needed. Attachments are staged in the job's local directory; they are not written to OpenViking until continuation.
4. Continue to freeze the next round's inputs and start a new Compile task. Once the round limit is reached, complete the final review or finish the job.

During initial artifact synchronization, the system deterministically normalizes the generated `BENCHMARK.md` from a structurally valid `benchmark.json`. Its displayed cases, Checklists and sources match the Test Dataset without introducing new cases or facts. Missing mandatory files still require repair; normalization does not supply them.

An empty question list still requires human confirmation when checkpoints are enabled. The administrator may continue or finish immediately. Browser refreshes and service restarts preserve the waiting state and saved attachments.

## Inputs to the next round

Each round receives three inputs with distinct roles:

- **Original Evidence**: the source snapshot frozen when the job started.
- **Human supplements**: accumulated answers, sources and supplementary documents, retaining the submitting identity and time.
- **Revision context**: each previous Skill body, Test Dataset case identifiers, and current review questions and feedback, used to revise the candidate collection.

Compile `from` contains only the frozen source Evidence and human supplement directory. Revision context is delivered as quoted text in `instruction`; it is not extracted again as source files and cannot be cited as provenance. The complete previous shared analysis and candidate artifacts remain archived locally and remotely. The full Test Dataset and duplicate Markdown representations are not added back to extraction inputs.

The revision-context JSON delivered to the model is limited to 30000 characters. If necessary, question text, feedback or the Skill body is shortened with explicit excerpt or omission flags. Complete content remains archived. An excerpt does not instruct the model to use tools to find the remaining files.

Previous model output is not established Evidence. The model must cite original sources or human supplement sources. A human answer that contradicts the original source must retain the conflict rather than silently replace the source's assertion.

Every round uses a separate output directory to prevent files from previous rounds leaking into the result. The console retains the remote task ID, input references, artifacts, validation results and elapsed time for each round, and allows reviewing different rounds.

## Finish and validate the result

A job can finish early at a human checkpoint. Its final round also requires final review. Review notes and unresolved questions can be retained when finishing. A final candidate collection with outstanding Evidence gaps remains marked for review.

Supplements added after the last round do not automatically change that round's artifacts. To incorporate them, continue while rounds remain or create another mining job. Finishing mining does not automatically Publish. The administrator selects a specific candidate and submits it with its own Test Dataset. Every Skill Candidate separately enters existing True Replay, Candidate Review and Publish, retaining independent state and results.

Test Dataset validation checks the file contract. It does not prove that business facts, numeric comparisons or completion conditions are correct. Multiple rounds provide opportunities to revise the result; administrators must still compare artifacts with their sources.

OpenViking may report `completed` after retaining usable artifacts from a run with failed source processing. Valid shared analysis and complete candidate bundles do not prove that all Evidence was processed. The console preserves these warnings in the checkpoint, round history and final artifact quality, marking the current result for review. For example, two source documents can produce a complete candidate collection even when extracting a third document failed.

When a warning identifies omitted sources, inspect those documents and provide review feedback. Continuing reprocesses the complete original frozen source and accumulated supplements. The previous candidate remains revision context and cannot replace omitted original Evidence. Review the next round's result again; historical warnings remain attached to their original round records.

## Recovery and cancellation

teamEvolver persists rounds, checkpoints and submission state locally. After a restart it resumes observing an existing remote task rather than creating another accepted Compile task. If a network interruption leaves acceptance unknown, the same round's idempotency key is used to query or recover its submission.

Initial preparation also freezes each round's compiler Skill body and input archive. The compiler is installed at a separate Skill URI whose name contains the source-body digest. Records include `compiler_source_sha256`, the actual installed body's `compiler_sha256`, and `compiler_uri`. Existing versions are reused only when their contents match; conflicts stop submission rather than overwrite that version.

Retries use the same compiler URI, body, inputs and Compile request. If acceptance is unknown and no remote task is recorded, a missing compiler package can be restored from its frozen body. Recorded tasks resume status observation directly. Service updates or other jobs installing new compiler versions do not change the version bound to this round.

Stopping an executing task requests cooperative cancellation from OpenViking and waits for its terminal state. A job waiting for human input can finish directly. Stop executing work before deleting its job.

See [Skill Mining API](../api/17-skill-mining.md) for interfaces and errors. Implementation lives in the [job repository](../../../team_miner/ov_compile_mining.py) and [main-service bridge](../../../team_miner/bridge.py).
