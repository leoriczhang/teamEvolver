# Candidates and Evolution API

Skill evolution can be triggered manually by administrators or automatically by the Worker. The Worker reads valuable Sessions, plans changes per Skill group and generates Skill Candidates. Candidates may enter True Replay automatically; after validation, tenant policy either publishes an accepted Candidate automatically or waits for human review. The flow is described in [Evolution Loop](../concepts/02-evolution-loop.md).

Code entry points:

- Candidates and evolution: `team_skills/api/candidates.py:create_router`
- Prompt and evolution settings: `team_skills/api/prompts.py:create_router`
- Evaluation and publishing: `team_skills/evolution/candidate_pipeline.py:run_validation`
- Candidate state: `team_skills/candidates/repository.py:CandidateRepository`

Read endpoints require a tenant member; writes, triggers and Prompt changes require a tenant administrator. Accept endpoints return `{"status": "accepted", "job_id", "status_url"}` and support `Idempotency-Key`.

## Evolution Trigger

### POST /api/skill-evolution/trigger

```json
{"session_ids": ["session_..."], "allowed_modules": ["skill", "prompt"], "execution_profile_id": "business-agent"}
```

At most 200 `session_ids`; when omitted, `valuable` Sessions stored before acceptance and not yet consumed by evolution are read, up to `evolution_max_sessions` (cap 200). The request cannot override identity or configuration. It creates a `skill.evolve` task pinned to the current `skills` module configuration revision; the first run freezes the input Session set and resumed runs use the same set.

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

Candidates in `validating` include `replay_progress`: `total`, `completed`, `succeeded` and `failed`. Progress comes from individual Case checkpoints. `succeeded` means both branches completed the Checklist; it does not imply an efficiency verdict of `accept`.

### GET /api/skill-candidates/{candidate_id}/detail

Candidate detail: Skill name, baseline version, revision, bundle, change notes, evidence Sessions, latest evaluation result and event history.

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

Evaluate without publishing: creates a `skill.validate` task that runs static checks and True Replay, writes the verdict (`accept`, `reject`, `inconclusive`), and returns the Candidate to `open`.

Each Checklist judgment uses the `checklist-format-retry-v1` policy, with at most two provider calls for the same interaction. Only a confirmed JSON parsing, schema validation or truncated-output format failure permits one additional call. It uses the same Checklist, evidence, model and request parameters without resending the Agent request. Valid negative judgments, provider errors, timeouts, insufficient budget, invalid input and generic unavailability without evidence of a format failure do not trigger this retry.

Usage from every actual call counts toward cumulative cost; missing usage is never treated as zero. Results still undergo strict validation, without repairing JSON or relaxing Checklist standards. If the additional call fails, the judge remains unavailable and validation fails closed. Historical validation results are not rewritten.

### POST /api/skill-candidates/{candidate_id}/validate

```json
{"mode": "auto", "candidate_revision": 2}
```

| mode | Behavior |
| --- | --- |
| `auto` | Publishes when static checks pass, Replay ran normally and the verdict is `accept`; rejects on `reject`; returns to `open` on `inconclusive` |
| `force` | Publishes when static checks pass and Replay ran normally, without requiring `accept` |

Validation pins the currently bound Replay Adapter revision and the `replay` module configuration. Only Candidates in `open` can be submitted.

The table describes Skill-only Candidates. Execution-aware Candidates first `/evaluate`, then `/validate` publishes from that immutable result without new model calls. New evaluations default to `standard-v1`: existing Skill static, runtime and native primary Replay gates plus actual Prompt loading, composition and dependency checks, without mandatory locked acceptance, A/A, repeat probes or independent review. Explicit `environment.validation.strategy="enhanced-v1"` retains enhanced gates and exact-result v2 independent review; old frozen plans without a strategy marker retain enhanced rules and need a new evaluation to adopt the default. Neither `auto` nor `force` bypasses Prompt primary Replay or frozen-policy gates. Configuration/asset drift conflicts or blocks the Worker after acceptance; historical results and costs are not rewritten. See [composition evolution](../concepts/12-execution-evolution.md).

### POST /api/skill-candidates/{candidate_id}/validation-budget

```json
{"candidate_revision": 18, "max_total_tokens": null, "reason": "Administrator records explicit authority to remove the cumulative token ceiling"}
```

Administrators may update only open execution-aware Candidates; success returns the updated detail (200). All three fields are required. `candidate_revision` is the current positive integer revision, guarded by CAS; a conflict returns 409. `max_total_tokens` is a higher finite integer (at most 10000000), or explicit JSON `null` to remove this Candidate's cumulative token ceiling. Booleans, nonpositive numbers, strings, out-of-range amounts and omitted fields return 400. The nonempty `reason` is limited to 2000 characters. Once unlimited, further changes and accidental finite replacements are rejected.

A new revision and audit event (`validation_budget_unlimited` for cancellation) require fresh evaluation and exact-result review where required by the frozen strategy. All incurred costs, execution plans and model parameters remain. Unlimited disables only the cumulative token threshold; unknown historical usage becomes an accounting warning. Checklist, observed loading and frozen-policy publication requirements remain. Explicit finite limits and old command rules are unchanged. See [composition evolution](../concepts/12-execution-evolution.md).

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
| `evolution_current_case_count` | 2 | Current Replay Case generation count, integer 1–6, pinned at acceptance; recorded-anchor deduplication may reduce the effective count |
| `evolution_historical_case_count` | 3 | Historical Replay Case sampling limit, integer 1–30; existing deduplication and cross-Dataset priority apply, pinned at validation acceptance |
| `min_group_sessions` | 2 | Minimum Sessions per Skill group |
| `min_group_users` | 2 | Minimum users per Skill group |
| `evolution_trigger_mode` | manual | Trigger evolution manually or periodically |
| `evolution_interval_seconds` | 600 | Automatic trigger interval, 60–604800 seconds |
| `bundle_allow_delete` | true | Allow evolution to delete bundle files |
| `bundle_edit_max_bytes` | 262144 | Per editable file limit |
| `bundle_prompt_max_bytes` | 786432 | Bundle limit placed in the Prompt |
| `auto_evaluate` | true | Submit evaluation automatically after a Candidate is generated |
| `publish_mode` | manual | Publish automatically after accepted validation or wait for human review |

Prompt Studio usage is in the [Prompt Studio Guide](../guides/08-prompt-studio.md).

## Errors

| Status | Cause |
| --- | --- |
| 400 | Invalid field, more than 200 `session_ids`, unknown Prompt setting |
| 403 | Not a tenant administrator |
| 404 | Candidate, command or stage does not exist |
| 409 | `candidate_revision` conflict, Candidate not in `open`, `Idempotency-Key` reused for a different request, configuration revision conflict |
| 503 | Tenant `skills` module configuration missing, or master key missing when storing credentials |
