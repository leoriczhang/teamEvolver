# Skill Mining API

These console-internal interfaces manage real OpenViking Compile rounds and human Evidence supplements. They require a valid console Session Cookie and existing mining administration permissions. Tenant machine credentials cannot call these management interfaces. The Agent data-plane protocol is unchanged.

Execution and synchronization live in the [main-service bridge](../../../team_miner/bridge.py); local jobs, rounds and supplements live in the [job repository](../../../team_miner/ov_compile_mining.py). See [Multi-round Skill Mining](../guides/17-multi-round-skill-mining.md) for the workflow.

## Interfaces

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/mining/jobs` | Create a job and submit its first Compile round |
| GET | `/api/mining/jobs` | List jobs and summary counts |
| GET | `/api/mining/jobs/{job_id}` | Read rounds, artifacts, logs and checkpoint state |
| POST | `/api/mining/jobs/{job_id}/supplements` | Stage attachments at the current checkpoint |
| POST | `/api/mining/jobs/{job_id}/continue` | Save supplements and start the next round |
| POST | `/api/mining/jobs/{job_id}/finish` | Save final review and finish mining |

## Create a job

The main fields of `POST /api/mining/jobs` are:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Job name, at most 120 characters |
| `source_id` | string | Registered source reference tagged for Skill use |
| `input_dir` | string | Selected OpenViking Resources directory URI |
| `max_rounds` | integer | Actual round limit, 1–5, default 3 |
| `human_checkpoints` | boolean | Wait for human review after each round, default true |
| `runtime_timeout_seconds` | integer | Runtime limit for each round, 60–7200 seconds, default 3600 |

The first round freezes the original source. Subsequent rounds use that snapshot, accumulated human supplements and previous revision context. Jobs return `rounds_mode: "orchestrated"`; `current_round` represents an actual round.

The first round determines and saves the primary language of the frozen original sources, using Simplified Chinese when Chinese predominates. Later rounds retain that language rather than deriving it from earlier English artifacts or supplements. The runtime adds the job's language rules to new Compile requests. Human-readable prose, explanations, case Queries, Checklist text, shared analysis and review questions use that language. Field names, IDs, file paths, source URIs, code, fixed enum values, direct source quotations and structural labels such as `Checklist`, `Evidence` and `Materials` remain unchanged. Neither the current API nor the console offers a separate output-language option.

Saved custom business instructions remain unchanged and do not require a reset for a language update. Accepted requests retain their frozen version; completed artifacts are not automatically translated or recompiled.

New jobs using the default compiler Skill use `candidate_contract_version: 2`: the model plans independently executable task methods before compiling a separate Skill Candidate for each method. Candidate count follows triggers, inputs, outputs, task boundaries, and the ability to execute and validate each method through True Replay independently. It does not follow document or chapter count. Each round allows at most 8 candidates as an execution bound, not a target to fill. Zero candidates is valid when no executable method is supported; pure facts and insufficient Evidence are recorded in `deferred` with reasons.

When a previously saved custom compiler Skill still uses the single-candidate contract, new jobs retain `candidate_contract_version: 1` with a compatibility warning. Reset to the current default compiler Skill to enable multiple candidates in subsequently created jobs. Existing jobs retain their frozen contracts.

The remote Compile request's `from` contains only this round's Evidence source directory. Revision context for the previous candidate collection is delivered directly through `instruction` rather than extracted as another source. Its JSON is limited to 30000 characters and contains each previous Skill body, case identifiers and current review information, with explicit excerpt or omission flags. The full previous artifacts remain in the round archive.

## Capability plan and artifact contract

Each round produces four shared analysis files and four separate files for each candidate:

```text
analysis/evidence-map.md
analysis/gaps.md
analysis/review.json
analysis/capability-plan.json
compiled_skill/<skill-name>/SKILL.md
compiled_skill/<skill-name>/EVALUATION.md
compiled_skill/<skill-name>/benchmark.json
compiled_skill/<skill-name>/BENCHMARK.md
```

`analysis/capability-plan.json` fixes candidate names, task boundaries and source references before writing. Artifacts cannot contain unplanned candidates. Names must be unique and match both the directory and the `name` in `SKILL.md` frontmatter. A planned candidate missing `SKILL.md` or another mandatory file is marked `incomplete` without blocking other candidates that pass structural validation.

| Field | Type | Meaning |
| --- | --- | --- |
| `schema_version` | string | Fixed value `skill-miner.plan.v1` |
| `skills` | object[] | Method plans for 0–8 candidates |
| `deferred` | object[] | Content that does not yet form a Skill, with reasons; required to be nonempty when there are zero candidates |

Each entry in `skills` contains:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Unique lowercase kebab-case name |
| `task` | string | Independently completable task |
| `trigger` | string | Applicable scenario |
| `inputs` / `outputs` | string[] | Task inputs and expected outputs; both must be nonempty |
| `boundary` | string | Method scope and constraints |
| `rationale` | string | Reason for splitting or merging this method |
| `source_refs` | string[] | Original-source or human-supplement URIs supporting this method; must be nonempty |

Each `deferred` entry contains nonempty `reason` and `source_refs`. References must belong to this job's source or supplement scope; previous model output cannot serve as Evidence. Candidates may share Evidence, but overlapping methods should be merged. Steps of a complete workflow should stay together when they cannot be used independently.

Each candidate's `benchmark.json` uses `team-replay.dataset`, contains 12–24 cases and references only that candidate. During initial artifact synchronization, the system deterministically normalizes the generated `BENCHMARK.md` from the structurally validated `benchmark.json`, preserving matching cases, Checklists and sources without adding cases or business facts. Missing mandatory files still prevent that candidate from being submitted; normalization does not supply missing artifacts.

Every candidate has its own Test Dataset, True Replay, Candidate Review and Publish result. A compiled collection does not directly become a published Skill library. A zero-candidate result retains analysis and deferral reasons without a candidate eligible for submission. Existing jobs retain their single-candidate contract with three analysis files and four package files; recovery or continuation does not automatically switch contracts.

On candidate submission, the service binds document cases with `replay.execution`: `kind: "document"`, `runtime_type: "document"`, and `binding: "tenant_replay_adapter"`. The current tenant's configured ReplayAdapterFactory selects the executor; the model does not select it. Original document URIs remain Evidence references, not recoverable Sessions. Baseline and Candidate open independent Agent sessions. A new Skill has no candidate in Baseline; an updated Skill uses the published version in Baseline. Assessment files and the full Checklist are excluded from the Agent's execution bundle, and requirements are disclosed through progressive feedback.

Candidate evaluation responses expose `completed`, `skipped`, `not_run`, or `failed` in both `status` and `replay.status`, along with `replay.reason` and the observed paired-case count `replay.case_count`. A missing adapter produces a skip with a reason. Unobserved metrics are not treated as zero or ties. Replay-based Publish requires a genuinely completed result that passes its release gates. Automatic publication of an unfinished evaluation returns HTTP 409 `replay_incomplete`, leaving the candidate open for reevaluation after configuration. Explicit administrator force-publish retains its existing permission and confirmation flow.

## Stage attachments

`POST /api/mining/jobs/{job_id}/supplements`:

```json
{
  "checkpoint_id": "<current pending_checkpoint.id>",
  "files": [
    {"name": "clarification.md", "content_base64": "IyBDbGFyaWZpY2F0aW9uCg=="}
  ]
}
```

Attachments are saved locally within the job and frozen into the next round's inputs on continuation. The response includes `ok`, `saved` and the updated `job`; `saved` lists names and references. Staging does not directly modify the original OpenViking source or generated artifacts.

Each upload accepts 1–10 files, with at most 20 staged attachments per round. Original files are limited to 10 MB each and 20 MB per upload. The round's converted Markdown attachments are limited to 8 MB in total.

## Continue or finish

`POST /api/mining/jobs/{job_id}/continue` and `/finish` accept the same review fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `checkpoint_id` | string | Must match the current `pending_checkpoint.id` |
| `answers` | object | Mapping of question `qid` to answer text |
| `unresolved` | string[] | Question identifiers explicitly left unresolved |
| `references` | object | Mapping of question `qid` to arrays of OpenViking source file URIs, at most 20 per question |
| `feedback` | string | Review feedback for this round's artifacts, at most 20000 characters |
| `request_id` | string | Client request identifier; retain it when retrying the same operation |

Required questions need an answer or an explicit entry in `unresolved`; the same question cannot be answered and marked unresolved. Answers and feedback together are limited to 100000 characters. Source references must be readable Resources text files, at most 2 MB each. Previous artifacts and revision context cannot serve as Evidence sources. Unknown questions, stale checkpoints and conflicting parameters in duplicate requests are rejected.

Example using a Cookie file from an authenticated console session:

```bash
curl -X POST 'http://localhost:52010/api/mining/jobs/<job_id>/continue' \
  -b '<console-cookie-file>' \
  -H 'Content-Type: application/json' \
  -d '{
    "checkpoint_id": "<checkpoint-id>",
    "answers": {"q1": "Use the billing rule stated in the source."},
    "unresolved": [],
    "references": {"q1": ["viking://resources/team/source/rules.md"]},
    "feedback": "Correct this rule in the Skill Candidate and Test Dataset.",
    "request_id": "review-round-1-continue"
  }'
```

`continue` freezes supplements and submits another round; continuation is rejected after `max_rounds`. `finish` ends mining and retains review notes and unresolved questions. It does not automatically run Compile again or Publish.

## Response state and recovery

Job details include status, `current_round`, a `rounds` list, the current `pending_checkpoint`, artifacts and quality information. New jobs also record `candidate_contract_version`. After synchronization, multiple-candidate results expose these fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `capability_plan` | object | Validated capability plan |
| `candidate_count` | integer | Number of candidates in this round |
| `valid_candidate_count` | integer | Candidates passing their own package and Test Dataset structural validation |
| `candidate_quality` | object | Quality results keyed by candidate name, including `level`, `warnings` and `can_submit` |

The `ready` / `incomplete` levels and `can_submit` in `candidate_quality` reflect structural validity. The job must still finish its current human review or execution rounds before eligible candidates can be submitted. Structural validity does not mean True Replay or Candidate Review has passed.

A checkpoint contains `id`, `round`, `questions` and `attachments`. Questions contain `qid`, `question`, `reason`, `source_refs` and `required`.

`remote_warnings` preserves OpenViking partial-output and source-processing warnings, which are also included in `artifact_quality.warnings`. Warning-bearing results have quality `caution`. Each round retains its warnings and `compiler_sha256`. Remote `completed` and structurally valid required artifacts do not prove complete Evidence coverage.

`compiler_source_sha256` identifies the frozen compiler source body. `compiler_sha256` identifies the actual installed body after package-name substitution and native formatting normalization. `compiler_uri` and `skill_uri` point to its separate package, and round archives retain these fields. An existing package with different contents is rejected rather than overwritten with another version.

| Status | Meaning |
| --- | --- |
| `preparing` | Preparing or recovering this round's inputs and submission |
| `queued` | Compile was accepted and awaits execution |
| `running` | Executing, synchronizing or materializing this round |
| `waiting_human` | Round completed; awaiting human supplements or final review |
| `stopping` | Cancellation requested; awaiting the remote terminal state |
| `succeeded` | Mining finished; final artifacts can enter subsequent validation |
| `failed` / `stopped` / `interrupted` | Failed, stopped or interrupted; inspect job details |

Every round has its own deadline. `waiting_human` consumes neither runtime allowance nor an execution slot. Unknown acceptance is recovered using the same round's original idempotency key and request. A new round uses a new key. Restarting the service does not increment the round count or resubmit accepted tasks.

## Errors

Errors return an HTTP status and a `detail` description. Common cases are:

| Status | Situation | Action |
| --- | --- | --- |
| 400 | Invalid job creation or attachment request | Correct the request |
| 401 / 403 | Not authenticated or lacking mining administration permissions | Use an authorized console identity |
| 404 | Missing job or source | Query a valid reference |
| 409 | Not waiting for review, stale checkpoint, unknown or unanswered required questions, round limit reached or request conflict | Refresh details, correct answers or explicitly mark questions unresolved |
| 413 | Attachment exceeds the allowed size | Reduce upload content |
| 429 | All 8 execution slots are occupied | Retry after existing work finishes |
| 502 | OpenViking source reading is temporarily unavailable | Check existing jobs and retry once connectivity recovers |

Creation and continuation persist local state before preparing remote inputs and submitting Compile in the background. Inspect job `error`, `submission_error` or synchronization information for background failures. A successful HTTP response does not mean remote Compile execution has started.

Shared Test Dataset validation checks structure rather than business facts. Evidence gaps require review even when the format is valid. Final artifacts still require True Replay and Candidate Review.
