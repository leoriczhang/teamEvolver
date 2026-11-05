# Skill Mining API

These console-internal interfaces manage OpenViking Compile rounds and human Evidence supplements. They require a valid console Session Cookie and either `tenant_admin` in the selected tenant or `platform_admin`. Ordinary members and tenant machine credentials cannot call these management interfaces. The Agent data-plane protocol is unchanged.

`team_miner/api/` composes the interfaces. Skill mining lives in `team_miner/skill_miner/`, separately from knowledge base mining in `team_miner/knowledge_miner/`. Jobs, rounds, supplements and artifact content persist through the [PostgreSQL Repository](../../../team_miner/skill_miner/repository.py); temporary working directories are rebuildable caches. See [Multi-round Skill Mining](../guides/17-multi-round-skill-mining.md) for the workflow.

## Default delivery and candidate eligibility

The current default compiler is the knowledge-point Skill compiler in `knowledge_compiler_v8.py`, exposed through `team_miner/skill_miner/knowledge_compiler.py`. Its `completion_policy: te-compile-finished.v1` records mining completion after remote Compile completion and persistence of returned artifacts. Content acceptance, Test Datasets and human approval are not completion gates. This mode does not require `benchmark.json`; its inventory explicitly reports `content_validated=false` and `can_submit=false`.

Mining completion therefore does not establish Candidate Review eligibility. The capability plan,12–24 Cases and human Evidence question contracts below apply to jobs explicitly using that review contract. Delivery-only packages cannot be submitted directly, and TE does not invent evaluations or Cases to make them eligible. Within the round limit, explicit revision requirements can start another round without empty automatic continuation. Review-capable packages must pass revalidation of the selected independent candidate before submission.

## Interfaces

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/mining/jobs` | Create a job and submit its first Compile round |
| GET | `/api/mining/jobs` | List jobs and summary counts |
| GET | `/api/mining/jobs/{job_id}` | Read rounds, artifacts, logs and checkpoint state |
| POST | `/api/mining/jobs/{job_id}/supplements` | Stage attachments at the current checkpoint |
| POST | `/api/mining/jobs/{job_id}/continue` | Save supplements and start the next round |
| POST | `/api/mining/jobs/{job_id}/finish` | Save final review and finish mining |
| POST | `/api/mining/jobs/{job_id}/resume` | Recover an identified failure or explicitly authorize one format-fix test |

## Execution and configuration

### Background mutation results

Uploads, conversion, artifact saves and other long operations involving external I/O first enqueue PostgreSQL `worker_tasks` and return HTTP 202 with the receipt below. API requests do not poll Compile or convert batches inline.

```json
{"job_id":"skill-miner-command:example","status_url":"/api/mined-jobs/skill-miner-command:example"}
```

Poll the receipt's `status_url` while the status is `queued`, `running`, `retry_wait`, `waiting_external` or `reconciling`. A `succeeded` response must include `result` with the operation's original response shape, such as upload `file_count`, saved `content` or the updated `job`. A `failed` or `cancelled` response provides `error_code` and is not a successful save. Polls remain bound to the submitting tenant. The console waits for at most20 minutes without resubmitting the mutation; persisted work remains available afterward.

Skill mining jobs under `/api/mining/jobs` retain the business states documented below. A mining job and the Worker command that performs one operation have separate identities. HTTP 202 alone does not prove Compile execution or artifact persistence.

### Settings revisions

The tenant's `miner` module configuration is required. Missing configuration returns503 without YAML or local-directory fallback. It provides runtime configuration needed by both knowledge base mining and Skill mining. Skill-specific round limits, deadlines and compiler instructions live separately in PostgreSQL `skill_mining_settings` and do not rewrite knowledge base mining settings.

`GET /api/mining/settings` returns its independent `revision`. `POST /api/mining/settings` and `POST /api/mining/prompts/ov_compile/reset` require the matching `expected_revision`. Compare-and-swap rejects stale updates with409; reload and review before saving again. Accepted jobs freeze both the `miner` module revision and Skill settings revision instead of reading later defaults.

## Create a job

The main fields of `POST /api/mining/jobs` are:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Job name, at most 120 characters |
| `source_id` | string | Registered source reference tagged for Skill use |
| `input_dir` | string | Selected OpenViking Resources directory URI |
| `max_rounds` | integer | Actual round limit, 1–5, default 3 |
| `human_checkpoints` | boolean | Wait for review after each review-contract round, default true |
| `mining_mode` | string | `auto` discovery (default), `single` one complete Skill, or `target` requested count |
| `mining_direction` | string | Up to 4000 characters; optional in auto/target, required complete-method goal in single |
| `target_skill_count` | integer/null | Required integer 1–100 in target, fixed to 1 in single, null or omitted in auto |
| `runtime_timeout_seconds` | integer | Round execution budget, 60–7200 seconds, default3600; not an ETA |

Whole-round jobs use `execution_protocol: "te-native-mining.v1"`: one normal Compile per round, full initial construction, then incremental updates of the same `stable_output_uri` using current supplements, implicated originals and changed revision context. `max_rounds` is an upper bound; an empty question list finishes mining without an empty extra round.

The first round determines and saves the primary language of the frozen original sources, using Simplified Chinese when Chinese predominates. Later rounds retain that language rather than deriving it from earlier English artifacts or supplements. The runtime adds the job's language rules to new Compile requests. Human-readable prose, explanations, case Queries, Checklist text, shared analysis and review questions use that language. Field names, IDs, file paths, source URIs, code, fixed enum values, direct source quotations and structural labels such as `Checklist`, `Evidence` and `Materials` remain unchanged. Neither the current API nor the console offers a separate output-language option.

Saved custom business instructions remain unchanged and do not require a reset for a language update. Accepted requests retain their frozen version; completed artifacts are not automatically translated or recompiled.

Jobs using the multi-candidate review contract use `candidate_contract_version: 2`. One native Compile plans complete independent methods and generates their artifacts. Counts follow goals and sources rather than files, customer profiles or substeps, without the old host eight-candidate batch limit. Insufficient support remains explicit rather than forcing a count.

When a previously saved custom compiler Skill still uses the single-candidate contract, new jobs retain `candidate_contract_version: 1` with a compatibility warning. Reset to the current default compiler Skill to enable multiple candidates in subsequently created jobs. Existing jobs retain their frozen contracts.

Whole-round execution lives in [native rounds](../../../team_miner/skill_miner/native_round.py) and the [compiler Skill](../../../team_miner/skill_miner/native_compiler.py). Per-method METHOD.json receipts aggregate into the existing capability plan. Test Dataset Markdown is deterministically rendered from validated JSON. Lost acceptance is reconciled by the original key; only explicit artifact-validation failures or truncated output permit at most two whole-task repairs per round. Dependency, source, authentication, transport and unknown failures retain structured diagnostics without becoming new business instructions, preserving original requests, errors and results.

Proposed deferrals are checked against the relevant original files in bounded tasks before final supplement questions are generated. An omitted rule in a reference summary or an unspecified runtime input is not sufficient evidence of a gap. Later batches and rounds reuse candidate identities; bounded reviews of similar methods retain merge reasons and complete source lineage. Sharing a source, formula or workflow step does not automatically justify merging, and similarity retrieval does not guarantee exhaustive duplicate detection. Each stage compiler carries only its current output contract, with shared business rules kept separate from the full artifact layout. Frozen requests are never rewritten retroactively.

Newly frozen later rounds distinguish identity reuse from business-field refinement. Reuse retains existing fields; explicit refinement requires the exact candidate identity, previous metadata hash, proposals within the original method scope, and a reason. TE preserves names and existing Case IDs, regenerates prose, evaluation instructions and cases, and records before/after values with source lineage. Subsequent planning and recovery use the accepted business version. Implicit same-name changes or replacing an identity with a method from another source are rejected. Source-scope checks do not prove semantic workflow equivalence, so business review remains necessary. Older frozen requests retain their original protocol.

Custom staged compiler Skills use exactly one `te-mining-policy.v1` fenced JSON block with `schema_version`, `common` and `stages`. `common` holds all shared business rules; `stages` maps stage names to additional rules. Unclassified freeform text is never silently removed or rewritten: it produces a migration error. Existing non-staged custom compilers retain their behavior. TE explicitly recognizes its default templates and known frozen default templates.

New Evidence reviews use contiguous original-text units of at most 768 characters. The model selects exact unit identities from the current assignment; TE restores quotations from frozen source offsets and hashes. Original text appears once, with batches bounded by the complete JSON size rather than truncating material. Frozen text-quotation requests still require an exact contiguous match; formatting differences in Unicode quotes, whitespace or standalone list bullets may only locate a unique span under the exact source URI. Changed numbers, operators, business words or ambiguous matches are rejected. Raw model results and older requests remain unchanged. This validates provenance, not complete business rules or correct reasoning.

Test Dataset case generation uses the existing public Compile file Map and Finalize: `from` submits a private input directory containing at most four independent assignment files, as the public API accepts directories only. Each model task still generates one bounded case. TE checks directory membership; inputs, source scope and output paths are isolated per file. Even when OV reports completion, TE validates each exact case identity, provenance, completeness and duplicate Query. Partial success retains valid cases and repairs only missing or invalid cases within bounded attempts. The candidate Dataset is assembled only after all twelve cases pass; frozen older single-case requests recover with their original request unchanged.

An older batch with file-valued `from` sources gets an audited directory request only after an exact submission-key lookup confirms no accepted task. Original assignment files, Case IDs and repair counts remain intact. The older request is unchanged, and accepted tasks recover their existing task identity; network errors and stop requests do not trigger replacement.

## Capability plan and artifact contract

The job freezes direction and count in `mining_intent`, including subsequent-round dependency checks. Count controls final selection, never splitting or merging. Matching is assessed inside existing workflow metadata calls without an additional Compile. The complete preselection plan and exclusion reasons remain archived; `analysis/mining-intent.json` records selection and final counts. A count shortfall requires human review even when automatic continuation was requested. Structural validation does not establish business correctness.

### Resume an identified repaired failure

For Native jobs, `POST /api/mining/jobs/{job_id}/resume` without a request body lets administrators explicitly recover a confirmed request for an undeclared attachment inside the selected compiler Skill package after TE supplies a different attachment-guidance body and SHA. Changing wrapper markers alone is not a new version, and clearing an error does not permit an unchanged retry. Recovery verifies the exact terminal remote event, frozen inputs, original request and accepted-round hashes. Missing sources or declared dependencies, authentication, network, output-protocol errors, unknown semantic failures and user stops are ineligible for this attachment-recovery path. Each round permits each guidance body once, with at most two upgrades including historical resource recovery; existing repair counts remain intact. Both the first and later recoveries retain the existing round budget start and deadline; recovery cannot extend them, and an expired budget prevents recovery. The original compiler, `from` / `to` and artifacts remain unchanged; guidance and its SHA are frozen before the first POST, and lost acceptance uses the same key.

TE declares the actual frozen attachment inventory and identifies `record_fields` / `scope_fields` as inline fields. It does not invent resources or modify OV. The inventory guides the model; it is not an enforced allowlist for the OV read tool, and the current Compile interface has no option to impose one. TE separates runtime diagnostics from business instructions and does not automatically recompile a whole round for dependency errors. These measures cannot guarantee that the OV model never requests an undeclared path.

The console action “重新实测本轮” uses the same endpoint with an explicit body to authorize one separately budgeted format-fix test:

```json
{
  "action": "retry_with_new_budget",
  "request_id": "653dad9a-4bfd-47ad-9a6c-ec79779b323b",
  "expected_task_id": "<current failed Native task ID>",
  "budget_seconds": 3600
}
```

`request_id` must be a UUID and remains unchanged when retrying the same operation. `expected_task_id` must match the current failed task; `budget_seconds` must equal the original job's `runtime_timeout_seconds` (3600 seconds in this example). The server records the administrator from the signed-in console identity, not a client-supplied authorizer. Eligibility requires a confirmed `output_protocol` failure and new format guidance that this job has not used. Each TE job permits this authorization once, after verifying the exact terminal remote event, frozen request and inputs. It preserves the same TE job, original `round_started_at` and all history, and starts a new Native task for a whole-round Compile; it does not promise to rerun only the failed Map.

The separate attempt budget is stored in `native_explicit_retry.attempt_budget` with `started_at`, `seconds` and `deadline_at`; `old_budget` retains the previous budget. Lost acceptance is reconciled using the original request ID and submission key without a duplicate Compile. Public `can_retry_native` and `native_retry_context` (`expected_task_id`, `budget_seconds`, `format_guidance_sha256`) determine whether the console offers the action. The UI distinguishes the original round's elapsed time including interruption waits, this attempt's elapsed time and its execution budget. The budget is not an ETA, and a new test does not establish business correctness or True Replay success.

The test also requires a provable raw artifact package from the immediately preceding accepted round, with an unchanged reviewed archive. If the failed Compile changed the remote target, TE archives every partial file before restoring the accepted baseline to the same target and reading back every SHA. Changed file membership, a third content version or an unprovable baseline stops recovery. An accepted submission is reconciled without rolling back its target. The frozen new instruction includes record-format guidance and a review of task-role consistency; it adds no factual Evidence and leaves the old compiler Skill unchanged.

For historical staged jobs, `POST /api/mining/jobs/{job_id}/resume` is admin-only and recovers the identified host failure caused by a previous-round name being reused for changed business scope. The frozen round must be unfinished at `identity_group` with no in-flight child. Other failures and requested stops return 409; full concurrency returns 429. It preserves inputs, accepted requests, reviews and unresolved questions, records the failure in `recovery_history`, and continues the same round without starting a new job or rerunning extraction. It does not submit a Skill Candidate, True Replay or Publish.

The endpoint also handles a confirmed integrated-body capacity failure: the remote task is completed, every sibling has a durable outcome, the failed candidate has an accepted draft, and all three repairs exceeded the 12K whole-output limit. A journal retains the original state and counters. Subsequent assignments use a separately versioned compiler to generate bounded insertions into the complete draft. Originals, accepted requests, old text and valid siblings are retained; the old compiler is never overwritten. Model output remains capped at 12K, while the deterministic local projection preserves every previous byte and validates source IDs, Chinese text and its revision chain within a 60K body bound. This transition is allowed once, rather than repeatedly resetting repairs. Other format, source or business failures require separate diagnosis.

A changed workflow proposal receives a distinct machine identity with an old/new metadata and source-scope audit. The previous identity is preserved, and normal grouping must not discard the conflicting accepted metadata.

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
| `skills` | object[] | Method plans for the round; the staged flow is not limited by the per-task bound of eight |
| `deferred` | object[] | Content deferred for insufficient Evidence, with reasons; the staged flow records facts separately without inventing gaps |

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

Historical batched execution also writes `analysis/method-dispositions.md` and pages such as `analysis/dispositions/page-0001.md` with matching JSON. They record each method proposal’s retention, merge, factual-information classification or deferral, including sources and reasons. Facts do not automatically request human Evidence.

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

Attachment content and job references persist in PostgreSQL; temporary directories are rebuilt only for execution. Continuation freezes them into the next round's inputs. The completed background command's `result` includes `ok`, `saved` and the updated `job`; `saved` lists names and references. Staging does not directly modify the original OpenViking source or generated artifacts.

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
| `execution_protocol` | string | `te-native-mining.v1` for whole-round execution |
| `stable_output_uri` | string | The remote target retained across rounds of one job |
| `native_compile_history` | array | Actual admitted task IDs and rounds, not internal model calls |
| `native_repairs` | array | Bounded whole-task repair errors and previous task records |
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

Before acceptance, errors return an HTTP status and a `detail` description. Long operations already accepted with202 expose terminal `failed` status and `error_code` at their `status_url`; inspect that background result for execution-time errors below. Common cases are:

| Status | Situation | Action |
| --- | --- | --- |
| 400 | Invalid job creation or attachment request | Correct the request |
| 401 / 403 | Not authenticated or lacking mining administration permissions | Use an authorized console identity |
| 404 | Missing job or source | Query a valid reference |
| 409 | Not waiting for review, stale checkpoint, unknown or unanswered required questions, round limit reached or request conflict | Refresh details, correct answers or explicitly mark questions unresolved |
| 413 | Attachment exceeds the allowed size | Reduce upload content |
| 429 | Execution slots or the bounded queue are full | Retry after existing work finishes |
| 502 | OpenViking source reading is temporarily unavailable | Check existing jobs and retry once connectivity recovers |

Creation and continuation persist PostgreSQL state before preparing remote inputs and submitting Compile in the background. Inspect job `error`, `submission_error` or synchronization information for background failures. A successful HTTP response does not mean remote Compile execution has started.

Shared Test Dataset validation checks structure rather than business facts. Evidence gaps require review even when the format is valid. Final artifacts still require True Replay and Candidate Review.

The staged execution contract saves `round_work_units` (estimated input-shard and artifact-generation workload) and `round_timeout_seconds` (effective round deadline). Each stage task reserves one eighth of the base deadline, with a minimum of 180 seconds per task; the total remains at least the base deadline. Once planning completes, file and case generation tasks enter the estimate. Within a round, the deadline can only increase and its start time stays unchanged; the next round is estimated independently. Retrying the same accepted request does not add work units. Existing frozen requests are unchanged. Original sources are persisted one file at a time, up to 10000 files and 20 MB per file, without the former aggregate 16 MB limit.

Sources are traversed directory by directory through the existing paginated OV file-list API, without a fixed depth or tree-node cutoff. Truncation signals, permission errors, repeated pages and out-of-scope entries are never treated as a complete source. Skill sections are validated and written atomically as a stream; final identity checks and archive hashes read bounded blocks without truncating the body during assembly.

Native failures are stored as `native_failure` with their original diagnostics and fingerprint. Only explicit artifact-validation failures or truncated output permit at most two whole-round repairs. Dependency, source, authentication, transport and unknown failures are not fed back as business instructions. `native_resource_recovery` retains historical resource recovery; `native_contract_recoveries` records bounded explicit guidance upgrades for undeclared-attachment failures and their version hashes. `can_retry_native` and `native_retry_context` expose eligibility for the explicit format-fix test. `native_explicit_retry` retains its administrator authorization summary and separate `attempt_budget` alongside `old_budget`.

## Submit an independent Skill Candidate

`POST /api/mined-jobs/{job_id}/skills/{skill_name}/submit` accepts only optional `artifact_path`, which must identify that Skill's current `SKILL.md` in the specified job. Acceptance returns202 for a background command. Its `result` provides `created`, candidate `job_id`, `skill_name`, `candidate_revision` and `question_count`.

The Worker reads the complete package and capability plan from PostgreSQL and revalidates the selected independent candidate. An incomplete sibling does not block a complete candidate. The package retains `SKILL.md`, `EVALUATION.md`, `benchmark.json`, `BENCHMARK.md` and any returned `METHOD.json`, scripts and reference files. An entrypoint or completion receipt alone does not replace validation.

Resubmitting the same raw bundle and execution-binding version within one tenant returns the same candidate without revising it or resetting review state. Another live candidate with that name makes the accepted background task fail with a conflict `error_code`, preserving human edits and in-flight validation. Worker fences, snapshot revisions and PostgreSQL transaction locks guard writes. Legacy inline Case materials become real `references/mining-materials/<case_id>/<sha256>.txt` files only in the candidate copy. Its canonical Test Dataset references those files, and both Replay branches receive the same verified content. Platform bundle limits apply; collisions or missing content reject submission. Original mining bytes and source hashes remain unchanged. Submission creates an `open` candidate without starting True Replay or Publish.

Source Viking URI citations remain intact. Compiler-provided Session identities, runtimes, Adapter choices and private Context snapshots do not become execution input. TE binds trusted `document` execution to the tenant's configured Replay Adapter; the platform freezes configuration and Adapter revisions when review execution is accepted.

The tenant Replay Adapter used for these document Cases must support the `document` runtime and independent execution without a source Session. An Adapter restricted to another source runtime may reject it. Do not fabricate Sessions or private Context, or switch Adapters to bypass that restriction. Verify real Adapter compatibility separately; a fixed local test Factory establishes interface compatibility only.
