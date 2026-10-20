# Skill Mining

Skill Mining turns domain documents and human Evidence supplements into a reviewable Skill Candidate and a Test Dataset derived from the same sources. teamEvolver manages jobs, rounds and candidate submission; OpenViking Compile reads the Evidence and generates the artifacts.

## Prepare and create a job

Start teamEvolver and configure a reachable OpenViking endpoint, account and server credentials in the console. Mining uses the model and execution environment configured in OpenViking.

1. Register or upload a source and give its directory the Skill purpose tag. Knowledge compilation can use the Knowledge purpose tag on the same source.
2. Choose 1–5 rounds (default 3), human Evidence checkpoints and a base round deadline of 60–7200 seconds (default 3600, extended with input shards and artifact-generation workload).
3. Inspect progress, execution events, remote task IDs and errors. At most eight jobs execute concurrently; waiting for human review does not occupy an execution slot.

The source is frozen when the job starts. Later changes to the original directory do not change its Evidence. Every round has separate inputs and outputs and a local archive.

## Native round delivery

New jobs submit one public OpenViking Compile per round. Within that Compile, the compiler Skill reads sources, consolidates methods, assigns content units, generates artifacts and reviews them. More content units do not add TE-side Compile calls or directly increase the number of final Skills.

`te-native-components.v2` organizes independent content units for each method while retaining v1 compatibility:

- A `core` unit generates `METHOD.json`, an executable `SKILL.md`, `EVALUATION.md` and a component manifest declaring resources, Case IDs, order and source dispositions. The main workflow selects configurations, loads resources and defines branches and completion conditions.
- The Skill may declare original tables, rule dictionaries and existing prompts as source resources. TE verifies their SHA against this job's frozen inputs and copies the exact bytes, preserving conflicts, formatting and language. Generated `reference` units provide necessary configuration navigation, applicability and coverage mappings rather than transcribing long sources again.
- Each `case` unit generates one complete Case JSON. Each method has 12 substantively different Cases with explicit sources.

Intermediate drafts carry configuration choices, concrete states and execution designs without embedding complete bodies in nested JSON. A fresh Agent reads the relevant original sources, independently reviews the design and writes the unit's complete files directly. TE validates manifests, paths, unique IDs, file completeness, source sets and the Test Dataset contract, then deterministically assembles source resources and `benchmark.json` in manifest order and renders `BENCHMARK.md`. TE does not select business rules, supply facts, add Cases or call Compile again for assembly.

Source resource declarations bind exact frozen source URIs, never live directories, unfrozen files or generated artifacts. SHA validation uses the frozen manifest, not model assertions. Exact source retention makes the text available but does not prove rule coverage. The Skill must still implement configuration selection, resource lookup, branches, conflict handling and completion conditions; Cases must check observable behavior for concrete states. Unit review and successful assembly do not establish complete cross-file business validation or a True Replay pass.

Sources are frozen one file at a time, with at most 10000 files and 20 MB per file; the former aggregate 16 MB source limit no longer applies. Source directories are traversed directory by directory through the existing paginated OV file-list API, without a fixed depth or tree-node cutoff. Job details show the effective deadline for the current round.

The component protocol applies only to newly frozen requests that select it. Existing jobs retain their compiler, requests, execution protocol and historical artifacts; they are not automatically migrated or recompiled. This flow uses existing OpenViking interfaces without changing OV code. TE manages source freezing, connection recovery, stopping and deterministic assembly; knowledge mining keeps its existing flow.

The first round determines and saves the primary language of the frozen original sources. Predominantly Chinese materials use Simplified Chinese. Later rounds retain that language regardless of earlier English artifacts or the language of supplements. Human-readable prose, explanations, case Queries, Checklist text, shared analysis and review questions use that language. Field names, IDs, file paths, source URIs, code, fixed enum values and direct source quotations remain unchanged. Structural labels such as `Checklist`, `Evidence` and `Materials` remain unchanged. The console has no separate output-language selector.

## Round artifacts

| Path | Purpose |
| --- | --- |
| `analysis/evidence-map.md` | Evidence, reusable methods and source references |
| `analysis/gaps.md` | Evidence gaps, conflicts and applicability boundaries |
| `analysis/review.json` | Structured human review questions using `skill-miner.review.v1` |
| `compiled_skill/<name>/SKILL.md` | Skill Candidate |
| `compiled_skill/<name>/EVALUATION.md` | Evaluation notes |
| `compiled_skill/<name>/BENCHMARK.md` | Readable Test Dataset description |
| `compiled_skill/<name>/benchmark.json` | Test Dataset using `team-replay.dataset` |

The console supports artifact preview, scrollable Markdown and editing. Candidate submission uses the current edited artifacts. Structural validation does not prove business facts correct; failed source processing, unresolved questions and repaired formats still require review.

## Review and supplement Evidence

New jobs with human checkpoints enabled wait only when the current question list is nonempty, including optional questions. A native round with no questions ends the job without another Compile to fill the round limit; its complete output and a separate skip receipt are still archived. The legacy non-native workflow advances through the selected rounds. Skipping is not human approval, business validation, resolution of unknown source facts or new Evidence. Existing jobs retain their frozen checkpoint policy. Administrators inspect artifacts, answer structured questions, cite accessible OpenViking sources, upload documents and leave review comments. Required questions that cannot be confirmed must be explicitly marked unresolved.

Continuing a native job starts an incremental Compile into the same artifact target, using actual supplements from the immediately preceding review, the necessary frozen originals implicated by its questions, and revision context. Freeform review feedback with an unknown impact scope conservatively includes all frozen originals. The legacy non-native workflow still reprocesses the original frozen sources and cumulative supplements. The previous Skill Candidate is revision context; it cannot replace verified Evidence or be cited as a source. At the round limit, finish the final review or stop. New Evidence provided during final review is not automatically compiled into existing artifacts.

Without human checkpoints, a native job ends after its current round passes structural validation, without another Compile; the legacy non-native workflow automatically executes all selected rounds. Restarts and network retries recover existing jobs and idempotent submissions. Stopping a running job requests remote cancellation; stop before deleting it.

See [Multi-round Skill Mining and Human Evidence Supplements](./17-multi-round-skill-mining.md).

## Candidate validation and publication

After mining, submit the Skill Candidate and Test Dataset to the existing candidate workflow. True Replay compares actual Baseline and Candidate execution; Candidate Review decides whether to Publish. Mining completion and human review never automatically publish a team Skill.

## Configuration and API

`mining.pipeline.max_rounds` sets the default round limit and `mining.pipeline.runtime_timeout_seconds` the default base round deadline. `mining.prompts.ov_compile` can override the compiler Skill body. Each round freezes its compiler, inputs and Compile request and binds a separate compiler URI derived from its content hash.

The runtime adds the job's language rules to new Compile requests while preserving saved custom business instructions verbatim; a language update does not require resetting them. Accepted requests and completed artifacts are not changed retroactively, automatically translated or recompiled.

See [Skill Mining API](../api/17-skill-mining.md) for parameters, states and errors. See [Console Guide](./03-console.md) for knowledge uploads, Wiki compilation and browsing.
