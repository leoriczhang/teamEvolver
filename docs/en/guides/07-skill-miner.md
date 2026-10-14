# Skill Mining

Skill Mining turns domain documents and human Evidence supplements into a reviewable Skill Candidate and a Test Dataset derived from the same sources. teamEvolver manages jobs, rounds and candidate submission; OpenViking Compile reads the Evidence and generates the artifacts.

## Prepare and create a job

Start teamEvolver and configure a reachable OpenViking endpoint, account and server credentials in the console. Mining uses the model and execution environment configured in OpenViking.

1. Register or upload a source and give its directory the Skill purpose tag. Knowledge compilation can use the Knowledge purpose tag on the same source.
2. Choose 1–5 rounds (default 3), human Evidence checkpoints and a base round deadline of 60–7200 seconds (default 3600, extended with input shards and artifact-generation workload).
3. Inspect progress, execution events, remote task IDs and errors. At most eight jobs execute concurrently; waiting for human review does not occupy an execution slot.

The source is frozen when the job starts. Later changes to the original directory do not change its Evidence. Every round has separate inputs and outputs and a local archive.

The default compiler uses bounded batches: long files are split into source ranges, method proposals remain in a local ledger and planning proceeds in bounded batches with a disposition for each item, and candidate files are generated independently. Each model call generates one Test Dataset case; up to four independent case files share one Compile before validated cases are assembled. Each file has its own inputs, output path and validation result. When a batch partially succeeds, valid cases are retained and only missing or invalid cases are repaired; an OV completed status does not establish batch validity. More input increases the number of batches and elapsed time rather than individual model input or output. Sources are frozen one file at a time, with at most 10000 files and 20 MB per file; the former aggregate 16 MB source limit no longer applies. Job details show the effective deadline for the current round.

Source directories are traversed directory by directory through the existing paginated OV file-list API, without a fixed depth or tree-node cutoff. Skill sections are validated and written atomically as a stream. Final identity checks and archive hashes read bounded blocks, avoiding repeated full-body reads without truncating the artifact.

The default flow uses the existing public OpenViking Compile API without changing OV code. TE submits bounded batches for extraction, method planning, file generation and case generation, validating small stage outputs before advancing. Business rules belong to the compiler Skill; TE handles source mappings, recovery, cancellation and Test Dataset assembly. Knowledge mining keeps its existing Compile flow.

Methods proposed for deferral are checked against the relevant original text before requesting human supplements. A rule omitted from a summary or a runtime input not yet supplied does not directly become an Evidence gap. The same method reuses its candidate identity across batches; similar proposals receive bounded reviews that retain all sources. Sharing a formula or source does not justify merging by itself. Finite similarity retrieval can still miss duplicates, so administrators should review the actual methods.

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

With human checkpoints enabled, every completed round waits for review, including the final round. Administrators inspect artifacts, answer structured questions, cite accessible OpenViking sources, upload documents and leave review comments. Required questions that cannot be confirmed must be explicitly marked unresolved.

Continuing starts a new round using the original frozen sources and cumulative supplements. The previous Skill Candidate is revision context; it cannot replace verified Evidence or be cited as a source. At the round limit, finish the final review or stop. New Evidence provided during final review is not automatically compiled into existing artifacts.

Without human checkpoints, all selected rounds execute automatically. Restarts and network retries recover existing jobs and idempotent submissions. Stopping a running job requests remote cancellation; stop before deleting it.

See [Multi-round Skill Mining and Human Evidence Supplements](./17-multi-round-skill-mining.md).

## Candidate validation and publication

After mining, submit the Skill Candidate and Test Dataset to the existing candidate workflow. True Replay compares actual Baseline and Candidate execution; Candidate Review decides whether to Publish. Mining completion and human review never automatically publish a team Skill.

## Configuration and API

`mining.pipeline.max_rounds` sets the default round limit and `mining.pipeline.runtime_timeout_seconds` the default base round deadline. `mining.prompts.ov_compile` can override the compiler Skill body. Each round freezes its compiler, inputs and Compile request and binds a separate compiler URI derived from its content hash.

The runtime adds the job's language rules to new Compile requests while preserving saved custom business instructions verbatim; a language update does not require resetting them. Accepted requests and completed artifacts are not changed retroactively, automatically translated or recompiled.

See [Skill Mining API](../api/17-skill-mining.md) for parameters, states and errors. See [Console Guide](./03-console.md) for knowledge uploads, Wiki compilation and browsing.
