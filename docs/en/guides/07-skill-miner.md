# Skill Mining

Skill Mining turns domain documents and human Evidence supplements into a reviewable Skill Candidate and a Test Dataset derived from the same sources. teamEvolver manages jobs, rounds and candidate submission; OpenViking Compile reads the Evidence and generates the artifacts.

## Prepare and create a job

Start teamEvolver and configure a reachable OpenViking endpoint, account and server credentials in the console. Mining uses the model and execution environment configured in OpenViking.

1. Register or upload a source and give its directory the Skill purpose tag. Knowledge compilation can use the Knowledge purpose tag on the same source.
2. Choose 1–5 rounds (default 3), human Evidence checkpoints and a per-round deadline of 60–7200 seconds (default 3600).
3. Inspect progress, execution events, remote task IDs and errors. At most eight jobs execute concurrently; waiting for human review does not occupy an execution slot.

The source is frozen when the job starts. Later changes to the original directory do not change its Evidence. Every round has separate inputs and outputs and a local archive.

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

`mining.pipeline.max_rounds` sets the default round limit and `mining.pipeline.runtime_timeout_seconds` the default per-round deadline. `mining.prompts.ov_compile` can override the compiler Skill body. Each round freezes its compiler, inputs and Compile request and binds a separate compiler URI derived from its content hash.

See [Skill Mining API](../api/17-skill-mining.md) for parameters, states and errors. See [Console Guide](./03-console.md) for knowledge uploads, Wiki compilation and browsing.
