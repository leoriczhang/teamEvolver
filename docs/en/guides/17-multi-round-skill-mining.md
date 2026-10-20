# Multi-round Skill Mining and Human Supplements

Skill mining compiles registered OpenViking documents into deliverable Skill packages and lives in `team_miner/skill_miner/`. The current default compiler constructs or locally revises instructor Skills by knowledge point from course source tables. It does not generate validation Cases or publish automatically. Knowledge base mining remains separate in `team_miner/knowledge_miner/` and compiles documents into Wiki pages.

The console retains Mining Overview, Sources and Skill Mining entries. Each accepted round uses frozen inputs and a compiler Skill; later edits to original directories or default instructions do not rewrite accepted rounds.

## Prerequisites and persistence

- API and Worker roles are deployed and the selected tenant has a `miner` module configuration. Missing configuration returns503 without local YAML fallback.
- The operator is a tenant `tenant_admin` or `platform_admin` and the tenant's OpenViking Account is configured.
- PostgreSQL persists jobs, rounds, attachments and artifact content. OpenViking stores knowledge artifacts and Compile Tasks; working directories are rebuildable caches.
- Skill mining defaults and compiler instructions persist separately in PostgreSQL. Saves and resets use independent `revision` / `expected_revision` values; stale versions return409 instead of overwriting another operator's changes.

Long operations such as uploads and artifact saves return202 with a background job receipt. The console waits for a completed `result` before reporting success. Failed or cancelled work and missing results are errors. See the [Skill Mining API](../api/17-skill-mining.md).

## Create a job

Register a directory under Sources, tag it for Skill use, then create a task from Skill Mining.

| Setting | Behavior |
| --- | --- |
| Maximum rounds | 1–5, default3; an upper bound, without mandatory empty rounds |
| Human supplements | Jobs using the review-question contract wait for real questions; default delivery completion does not depend on human approval |
| Round deadline | 60–7200 seconds, default3600; a budget protection, not an ETA |
| Free discovery | Optional direction; the selected compiler organizes source-supported artifacts |
| Single Skill | Required direction and a fixed target count of1 |
| Target count | 1–100, optional direction; report insufficient support without forced splits or merges |

Compile uses OpenViking's models and runtime. One external Compile may contain multiple model calls and internal tasks; external request counts alone do not establish workload.

## Default delivery and revisions

The default compiler is `team_miner/skill_miner/knowledge_compiler_v8.py`, using execution protocol `te-native-mining.v1` and completion policy `te-compile-finished.v1`. After remote completion, TE preserves returned files and records successful mining without adding content acceptance gates or requiring `EVALUATION.md`, `METHOD.json`, `benchmark.json` or validation Cases.

The job reports `content_validated=false` and its candidate inventory reports `can_submit=false`. Mining delivery is complete; this does not establish business correctness, successful True Replay or permission to publish.

Administrators can inspect and edit current artifacts. Within the round limit, explicit supplementary revision requirements can start another round. Continuation freezes those requirements, attachments and necessary original and prior-round context, then incrementally updates the same remote target. Empty automatic continuation is not allowed. Existing files and round archives remain available.

## Review and supplements for complete candidates

Jobs explicitly using the candidate-review contract still support independent methods, capability plans and Test Datasets:

- Complete packages contain `SKILL.md`, `EVALUATION.md`, `benchmark.json`, `BENCHMARK.md` and actual returned references or scripts.
- Each `team-replay.dataset` has12–24 Cases per independent candidate, with12–24 output conditions and at least one trajectory condition per Case.
- Capability plans follow independently executable task boundaries. Facts and insufficient Evidence retain explanations instead of invented methods.
- Human review collects concrete answers and sources, explicitly unresolved questions, attachments, feedback and permitted artifact edits.
- Only a valid, real empty-question receipt can skip supplements. A missing or malformed receipt is not an empty question list.

Delivery-only packages cannot enter this flow through invented evaluation Cases.

## Submission and publishing boundaries

A completed task can submit a selected independent candidate only after its complete artifacts and Test Dataset pass revalidation. An incomplete sibling does not block complete candidates. Submission preserves the full package, rather than one Markdown entrypoint.

Repeated submission of the same raw package returns the same candidate without resetting validation or human review. A different live candidate with the same name makes the accepted background task fail with a conflict `error_code`; resolve the existing candidate first. Legacy inline materials become verified files in the candidate bundle so that both Replay branches receive the same content; the original mining package stays unchanged. Submission creates only an `open` candidate. True Replay, Candidate Review and Publish retain the main platform's gates; submission never publishes automatically.

Document source URIs remain Evidence rather than recorded Sessions. Compiler-provided Session identities, private Context, runtime and Adapter choices do not determine execution identity. Execution uses the tenant's configured and frozen Replay Adapter.

## Failure and recovery

- Synchronization failures retain the last known state and retry with backoff without creating a replacement Compile.
- Lost acceptance uses the original idempotency key to reconcile the same request instead of blindly resubmitting.
- A stop request waits for upstream cancellation or terminal confirmation; local intent does not establish remote cancellation.
- Jobs using acceptance contracts apply bounded repairs only to recognized format, incomplete-artifact or truncation failures, preserving original requests, errors and results.
- Missing configuration and unavailable PostgreSQL or OpenViking fail explicitly. Local caches cannot replace authoritative state or remote results.

Rounds, Compile receipts, timings, supplements and archives each report their own evidence. Completion receipts, format checks and local tests cannot replace real business use or True Replay.

The tenant Replay Adapter used for these document Cases must support the `document` runtime and independent execution without a source Session. An Adapter restricted to another source runtime may reject it. Do not fabricate Sessions or private Context, or switch Adapters to bypass that restriction. Verify real Adapter compatibility separately; a fixed local test Factory establishes interface compatibility only.
