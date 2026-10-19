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
{"session_ids": ["session_..."]}
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

### POST /api/skill-candidates/{candidate_id}/validate

```json
{"mode": "auto", "candidate_revision": 2}
```

| mode | Behavior |
| --- | --- |
| `auto` | Publishes when static checks pass, Replay ran normally and the verdict is `accept`; rejects on `reject`; returns to `open` on `inconclusive` |
| `force` | Publishes when static checks pass and Replay ran normally, without requiring `accept` |

Validation pins the currently bound Replay Adapter revision and the `replay` module configuration. Only Candidates in `open` can be submitted.

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
