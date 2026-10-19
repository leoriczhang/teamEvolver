# Prompt Studio Guide

Prompt Studio is the console page "Evolution Pipeline → Skills Self-Evolution". It shows the Skill evolution pipeline, edits the system prompt and model parameters of each LLM stage, and tests stage output against real Sessions. All settings are stored per tenant in the `skills` module configuration; the Worker uses the configuration revision pinned when a task was accepted.

Code entry points: `team_skills/api/prompts.py:create_router` (API), `team_skills/evolution/prompt_studio.py` (stage catalog and prompt resolution), `team_skills/evolution/prompt_test.py` (stage tests), `web-ui/src/features/skills/PromptStudioView.tsx` (page).

## Pipeline

```text
ingest → analyze_session → group
                           ├─ evolve_skill / create_skill ─ (optional merge) ─┐
                           └─ dataset_synthesis ──────────────────────────────┤
                                                                             ▼
                                              validate → replay_checklist → publish
analyze_session → analyze_trace_badcase
```

Skill evolution and Dataset synthesis start concurrently from the same frozen Sessions and cross-cycle Evidence. Dataset synthesis does not read this round's proposal; validation starts only when both the Candidate and its Cases are ready. The console marks improve/create as conditional paths and validation as a join.

| Node | Kind | Description |
|------|------|------|
| `ingest` | Input | Sessions arrive through the ingest API or a data source pull |
| `analyze_session` | LLM | One call performs value classification, trajectory summary and session scoring |
| `analyze_trace_badcase` | LLM | Analyzes problems across all Traces of a Session |
| `group` | Logic | Groups by Skill used; Sessions without a Skill go to the no-skill group |
| `evolve_skill` | LLM | For an existing Skill, decides improve, optimize_description, create or skip |
| `create_skill` | LLM | Decides whether the no-skill group contains a reusable pattern and creates a new Skill |
| `merge` | LLM | Merges two evolved versions of the same Skill when they conflict |
| `dataset_synthesis` | LLM | Runs alongside Skill evolution and independently generates Test Datasets from shared Evidence |
| `validate` | Gate | Waits for the Candidate and Dataset, then runs True Replay to compare turns, tool calls and tokens |
| `replay_checklist` | LLM | Judges each Checklist item from replies, tool trajectory and workspace artifacts |
| `publish` | Output | Candidates passing the gate are published to SkillHub |

The evolution flow is in [Evolution Loop](../concepts/02-evolution-loop.md), and gate rules in [Checklist](../concepts/07-checklist.md).

## Editable Stages

| Stage ID | Default temperature | Default max_tokens |
|---------|------------------|-----------------|
| `analyze_session` | 0.1 | 32768 |
| `analyze_trace_badcase` | 0.1 | 8192 |
| `evolve_skill` | 0.4 | 16384 |
| `create_skill` | 0.4 | 16384 |
| `merge` | 0.3 | 8192 |
| `dataset_synthesis` | 0.3 | 16384 |
| `replay_checklist` | 0.0 | 8192 |

`evolve_skill`, `create_skill` and `merge` run as an Agent loop: the model plans, calls tools to load evidence as needed, and finally submits a result.

### Shared Blocks

The `evolve_skill` and `create_skill` templates contain shared-block placeholders that expand into common rules at run time:

| Placeholder | Content |
|--------|------|
| `__GENERALIZATION_RULES__` | Generalization rules, so details of a single Session are not written into the Skill |
| `__USER_OVERRIDE_RULE__` | Explicit user requests take precedence |
| `__EVIDENCE_ROUTING_RULES__` | Evidence loading rules |
| `__OUTPUT_LANGUAGE_RULE__` | Output language rule |

The editor shows the unexpanded template; saved overrides are expanded the same way at run time. Removing a placeholder removes the corresponding rules from that stage.

## Settings

Each stage can override:

| Field | Description |
|------|------|
| `prompt` | System prompt override; must not be empty |
| `model` / `base_url` | Stage-specific model; empty inherits the tenant default model |
| `temperature` / `max_tokens` | Sampling parameters |
| `api_key` | Stage-specific key, encrypted as a Secret Ref and never echoed; `clear_api_key=true` clears it |

Saving `api_key` requires `TEAMEVOLVER_SECRET_MASTER_KEY` (or `TEAMEVOLVER_ROOT_API_KEY`) on the service, otherwise 503 is returned. Reset removes both the prompt override and the model settings of the stage.

Process parameters are edited under "Process Parameters" on the same page:

| Field | Default | Description |
|------|------|------|
| `evolution_max_sessions` | 100 | Maximum Sessions consumed per evolution round |
| `min_group_sessions` | 2 | Minimum Sessions for a Skill group to enter evolution |
| `min_group_users` | 2 | Minimum users for a Skill group to enter evolution |
| `evolution_trigger_mode` | manual | Manual trigger or periodic automatic trigger |
| `evolution_interval_seconds` | 600 | Automatic trigger interval |
| `bundle_allow_delete` | true | Whether evolution may delete bundle files |
| `bundle_prompt_max_bytes` | 786432 | Total budget of bundle content injected during evolution |
| `bundle_edit_max_bytes` | 262144 | Per-file edit limit |
| `auto_evaluate` | true | Whether a new Candidate is submitted for validation automatically |
| `publish_mode` | manual | Publish after accepted validation or wait for human review |

## Testing a Stage

1. Choose a stage on the left and edit the prompt (saving is optional).
2. Choose a real Session from the sample list.
3. Click test. The request is accepted as a `skill.evolve` task (operation `prompt_test`) and returns a `job_id`.
4. The Worker runs it with the edited prompt and the stage model settings; the result includes the system prompt, user message and model output. Agent-loop stages run at most 6 rounds and return a per-round transcript.

Tests use the real model, write no Candidate and change no Skill. With trace observability enabled, test calls are reported as `team_skills.evolution.prompt_test.<stage_id>`.

## API

| Method | Path | Description |
|------|------|------|
| GET | `/api/skill-evolution/pipeline` | Pipeline nodes and edges |
| GET | `/api/skill-evolution/prompts` | Summary of every stage, including override flags and current model parameters |
| GET | `/api/skill-evolution/prompts/{stage_id}` | Stage details: default template, current template, expanded text, variables |
| PUT | `/api/skill-evolution/prompts/{stage_id}` | Save stage settings, optionally with `expected_revision` |
| POST | `/api/skill-evolution/prompts/{stage_id}/reset` | Restore defaults |
| GET | `/api/skill-evolution/settings` | Process parameters and `revision` |
| PUT | `/api/skill-evolution/settings` | `{"evolve": {...}, "revision"}` save process parameters |
| GET | `/api/skill-evolution/session-samples?limit=` | Sessions available for tests (1–200) |
| POST | `/api/skill-evolution/prompts/{stage_id}/test` | `{"session_id", "prompt"?}` accept a test; returns 202 |

Write operations require a tenant administrator. Configuration writes compare revisions; concurrent edits return a conflict and must be retried after refresh. Unknown stages return 404 and unsupported fields return 400.

## Recommendations

- Compare outputs before and after an edit in the test panel before saving
- Change one stage at a time and watch the Candidates and True Replay results of the next evolution round
- `replay_checklist` is the validation judge; raising its temperature reduces judgment stability
- Before changing the output structure of `analyze_session`, make sure downstream parsing fields stay the same

## Related Documents

- [Evolution Loop](../concepts/02-evolution-loop.md)
- [Trace Observability Guide](./04-observability.md)
- [Web Console User Guide](./03-console.md)
