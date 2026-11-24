# Candidates and Evolution API

Skill evolution can be triggered manually by administrators or automatically by the Worker. The Worker reads valuable Sessions, plans changes per Skill group and generates Skill Candidates. Candidates may enter True Replay automatically; after validation, tenant policy either publishes an accepted Candidate automatically or waits for human review. The flow is described in [Evolution Loop](../concepts/02-evolution-loop.md).

Planning also reads the same tenant's unpublished Candidate catalog: names, revisions, source Agent scopes and validation feedback. The `read_candidate` tool loads a selected proposal's complete body. New Evidence can revise a Candidate that has no published version. For overlapping proposals, `merge_skill` selects `target_candidate_id` and `source_candidate_ids`; a dedicated Merge stage produces the consolidated Skill. Renaming an existing capability is not a reason to create another Skill.

Before creating a new capability, the planner must successfully read existing Candidates in the same source Agent scope. A read failure requires correcting the identifier or skipping; it cannot justify another alias. Context compaction retains exact Candidate identifiers and original system contracts. Planning rounds, Candidate read results and decision validation errors are recorded in `agent_audit`.

Consolidation preserves the target name and Candidate ID, increments its revision and clears stale validation. Sources are archived with their files, evaluations and `decision.merged_into` retained. Target revision and source archival commit in one transaction, guarded by every input's status, revision, tree hash and baseline. Candidates from unrelated Agent scopes, those undergoing validation, and updates to other published Skills cannot be retired as merge sources. Nonconflicting auxiliary files survive; conflicting file contents require explicit resolution.

Analysis and planning include original Agent system contracts and input modality visibility. Text-only analysis cannot treat unverified visual/OCR claims as fabricated merely because video, image or audio bytes are unavailable. Test Dataset synthesis remains independent, using Session Evidence without unpublished Candidate bodies or the current Merge output. Manual `reprocess` reuses existing ingest analysis; checking historical summaries against the original contract does not rerun the Session Judge.

MA connection, execution fingerprint, source snapshot and target-member reads during validation acceptance reuse the caller's transaction connection. This avoids exhausting the pool by borrowing another connection while holding a transaction. Standalone calls still use their own short transactions.

Code entry points:

- Candidates and evolution: `team_skills/api/candidates.py:create_router`
- Prompt and evolution settings: `team_skills/api/prompts.py:create_router`
- Evaluation and publishing: `team_skills/evolution/candidate_pipeline.py:run_validation`
- Candidate state: `team_skills/candidates/repository.py:CandidateRepository`

Read endpoints require a tenant member; writes, triggers and Prompt changes require a tenant administrator. Accept endpoints return `{"status": "accepted", "job_id", "status_url"}` and support `Idempotency-Key`.

## Evolution Trigger

### POST /api/skill-evolution/trigger

```json
{"session_ids": ["session_..."]}
```

At most 200 `session_ids`; when omitted, `valuable` Sessions stored before acceptance and not yet consumed by evolution are read, up to `evolution_max_sessions` (cap 200). The request cannot override identity or configuration. It creates a `skill.evolve` task pinned to the current `skills` module configuration revision; the first run freezes the input Session set and resumed runs use the same set.

For explicit historical reprocessing, submit `{"session_ids":["session_..."],"reprocess":true}`.
A nonempty ID list, mandatory analysis, and matching content hashes are required. Only the selected
Evidence and derived Cases participate in this candidate; archived candidates, Evidence, and automatic
datasets remain archived. Planning uses the current effective bundle and retains version, reset-boundary,
and candidate-revision concurrency checks.

### GET /api/skill-evolution/jobs/{job_id}/result

```json
{"job_id": "evolve:...", "result": {}}
```

### GET /history

Evolution cycle history; see [Session Queries](./08-sessions-api.md#evolution-history).

## Skill Candidates

States: `open` (awaiting review) → `validating` (evaluation or validation running) → `published` / `rejected`; an inconclusive validation returns to `open`.

### GET /api/skill-candidates

| Parameter | Description |
| --- | --- |
| `scope` | `open` (default, includes `validating`), `processed`, `all` |
| `limit` | 1–200, default 20 |
| `offset` | ≥0 |
| `compact` | `true` omits large fields |

Returns `{"candidates", "total", "limit", "offset", "has_more"}`.

Console lists use `compact=true`. Completed evaluations carry `evaluation.summary_only=true`, retaining verdicts, efficiency metrics, Checklist counts and the first Replay error while omitting Case bodies, Session snapshots, artifacts and repeated Dataset results. Full evidence is loaded through the detail endpoint. Summary polling preserves a loaded full evaluation for the same revision.

Candidates in `validating` include `replay_progress`: `total`, `completed`, `succeeded` and `failed`. Progress comes from individual Case checkpoints. `succeeded` means both branches completed the Checklist; it does not imply an efficiency verdict of `accept`.

### GET /api/skill-candidates/{candidate_id}/detail

Candidate detail: Skill name, baseline version, revision, bundle, change notes, evidence Sessions, latest evaluation result and event history.

`include_replay` defaults to `true`, returning full Replay evidence. The console initially uses `include_replay=false`, keeping Skill content, diffs, item-level Checklist judgements and the decision while omitting full Replay trajectories. `evaluation.replay_details_omitted=true` indicates that full Replay evidence can be requested separately. Stored detail evaluations carry `cached=true`. Polling reuses detail only when both revision and `updated_at` match; loading and request failures have explicit states.

### PUT /api/skill-candidates/{candidate_id}/content

```json
{"description": "...", "category": "general", "content": "...", "candidate_revision": 2}
```

Changes the Candidate's `SKILL.md` and creates a new `candidate_revision`. Renaming is not allowed; a `candidate_revision` different from the current one returns 409.

### PATCH /api/skill-candidates/{candidate_id}/feedback

```json
{"reviewed": true}
```

Marks whether the Candidate has been reviewed; only `reviewed` is accepted.

### POST /api/skill-candidates/{candidate_id}/evaluate

Evaluate without publishing: creates a `skill.validate` task that runs static checks and Candidate
Validation, writes the verdict (`accept`, `reject`, `inconclusive`), and returns the Candidate to `open`.

The request can be empty or contain `{"validation_method":"auto"}`. Methods are `auto` (default,
selected by the Worker LLM from actual differences and current Evidence), `experience_review`,
`candidate_execution` and `full_ab`. Review cannot pass unsupported behavioral changes. Experience
review reports Evidence references and scope without asserting Runtime execution. Candidate execution
runs one relevant Candidate Case with an explicit stage or complete-delivery scope. Full A/B retains
isolated branches and efficiency comparison. All model and execution work stays in the Worker; acceptance
still returns 202.

`evaluation.replay` records `validation_method`, `validation_scope`, `basic_validation`,
`candidate_completed`, `ab_completed`, `ab_verdict` and `assessment`. Candidate completion is independent
of the legacy `succeeded` count of completed pairs. Basic acceptance never rewrites a historical A/B verdict.

### POST /api/skill-candidates/{candidate_id}/validate

```json
{"mode": "auto", "candidate_revision": 2, "validation_method": "auto"}
```

| mode | Behavior |
| --- | --- |
| `auto` | Publishes when static checks and the chosen method's gate pass with verdict `accept`; rejects on `reject`; returns to `open` on `inconclusive` |
| `force` | An administrator directly adopts and publishes the current Candidate without rerunning assessment, creating Candidate Execution or A/B Sessions, or requiring execution gates to pass. Authorization, Candidate revision, static content, baseline version, and cloud publication readback still apply. The original validation remains unchanged; overriding failed or absent validation records `validation_overridden=true`, with `review_overridden=true` additionally recorded for Experience Review. The console shows publication progress instead of Replay Case progress |

Validation pins current Evidence, Candidate bytes, Replay Adapter revision and the `replay` module
configuration. Missing execution configuration does not prevent accepting experience review; only
execution methods consume MA resource gates. Publication retains the evaluated method by default and
reuses completion with matching Candidate and execution fingerprints. Only `open` Candidates can be submitted.

Forced experience-review adoption reuses the saved review for the current Candidate revision without another model call. Publication runs in the background and reports success only after the version is written and read back. A blocked publication keeps its details open and shows the reason.

### POST /api/skill-candidates/{candidate_id}/reject

Manual rejection; the Candidate becomes `rejected` immediately. Related successful experience Patterns record the outcome.

## Prompt and Evolution Settings

Prompt overrides and per-stage model settings are stored in `prompt_overrides` and `stage_settings` of the tenant `skills` module configuration; every write uses revision optimistic locking.

| Stage ID | Purpose |
| --- | --- |
| `analyze_session` | Session Analyze (value classification, summary, scoring) |
| `analyze_trace_badcase` | Trace Analyze |
| `evolve_skill` | Revise an existing Skill |
| `create_skill` | Create a new Skill |
| `merge` | Merge changes |
| `dataset_synthesis` | Synthesize Replay datasets |
| `replay_checklist` | Replay Checklist generation and judging |

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/skill-evolution/pipeline` | Evolution pipeline graph |
| GET | `/api/skill-evolution/prompts` | Current Prompts of all stages |
| GET | `/api/skill-evolution/prompts/{stage_id}` | Single stage; unknown stages return 404 |
| PUT | `/api/skill-evolution/prompts/{stage_id}` | Change `prompt`, `model`, `base_url`, `temperature`, `max_tokens`, `api_key`, `clear_api_key`, optionally with `expected_revision` |
| POST | `/api/skill-evolution/prompts/{stage_id}/reset` | Remove the stage's override |
| POST | `/api/skill-evolution/prompts/{stage_id}/test` | `{"session_id", "prompt"?}` tests the Prompt with one Session, returns 202 |
| GET | `/api/skill-evolution/session-samples?limit=50` | Valuable Sessions usable for tests (max 200) |
| GET | `/api/skill-evolution/settings` | Evolution parameters and `revision` |
| PUT | `/api/skill-evolution/settings` | `{"evolve": {...}, "revision": N}` |

A stage `api_key` is stored as a Secret Ref and needs `TEAMEVOLVER_SECRET_MASTER_KEY` (falling back to `TEAMEVOLVER_ROOT_API_KEY` as master key when unset); when neither is set the response is 503.

Fields of `settings.evolve` and `settings.validation`:

| Field | Default | Description |
| --- | --- | --- |
| `evolution_max_sessions` | 100 | Max Sessions read per evolution |
| `min_group_sessions` | 2 | Minimum Sessions per Skill group |
| `min_group_users` | 2 | Minimum users per Skill group |
| `evolution_trigger_mode` | manual | Trigger evolution manually or periodically |
| `evolution_interval_seconds` | 600 | Automatic trigger interval, 60–604800 seconds |
| `bundle_allow_delete` | true | Allow evolution to delete bundle files |
| `bundle_edit_max_bytes` | 262144 | Per editable file limit |
| `bundle_prompt_max_bytes` | 786432 | Bundle limit placed in the Prompt |
| `auto_evaluate` | true | Submit evaluation automatically after a Candidate is generated |
| `assessment_timeout_seconds` | 600 | Total model assessment budget under `validation`, integer 30–1800 seconds |
| `publish_mode` | manual | Publish automatically after accepted validation or wait for human review |

`assessment_timeout_seconds` is tenant scoped and pinned with the validation command's configuration revision;
updates affect newly accepted tasks only. The budget includes model queueing, HTTP requests and retries.
Each HTTP request uses the remaining budget after queueing and previous attempts, reserving up to 5 seconds for cleanup.
Coroutine cancellation cannot forcibly stop a synchronous HTTP thread; its concurrency slot stays occupied until the request ends.
This setting controls strategy selection and Experience Review only, not Candidate Execution or True Replay execution timeouts.
Identical source contracts appear once in the assessment input's `source_contracts` table, referenced by each Evidence's
`source_contract_ref`. Evidence IDs, summaries, distinct contracts and material visibility remain separate.

Prompt Studio usage is in the [Prompt Studio Guide](../guides/08-prompt-studio.md).

## Errors

| Status | Cause |
| --- | --- |
| 400 | Invalid field, more than 200 `session_ids`, unknown Prompt setting |
| 403 | Not a tenant administrator |
| 404 | Candidate, command or stage does not exist |
| 409 | `candidate_revision` conflict, Candidate not in `open`, `Idempotency-Key` reused for a different request, configuration revision conflict |
| 503 | Tenant `skills` module configuration missing, or master key missing when storing credentials |
