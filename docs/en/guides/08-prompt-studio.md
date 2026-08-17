# Prompt Studio Guide

Prompt Studio is teamEvolver's white-box tool that transforms the skill evolution pipeline from a black box into an inspectable, editable, testable transparent system. With Prompt Studio, you can view each evolution stage's system Prompt, modify them, test effects using real sessions, and independently tune model parameters per stage.

Backend logic at `teamEvolver/evolve/prompt_studio.py`, frontend view at `web-ui/src/views/PromptStudioView.tsx`.

## Design Philosophy

Historically, system Prompts for LLM stages in evolution pipeline were constants hardcoded in Python modules, invisible black boxes to console users. Prompt Studio elevates these Prompts to first-class citizens:

- **Inspectable**: View actual System Prompt used per stage (including defaults and your saved overrides)
- **Editable**: Directly edit Prompt online; changes take effect immediately
- **Testable**: Select real historical sessions, run tests with currently edited Prompt, see System Message, User Message and model output
- **Tunable**: Each stage can independently configure temperature, max_tokens, even specify different models
- **Rollbackable**: One-click reset to built-in default Prompt
- **Version Persistence**: Prompt overrides stored in `~/.teamEvolver/prompt_overrides.json`, stage model settings stored in `~/.teamEvolver/stage_settings.json`

Override file paths customizable via environment variables:
- `TEAMEVOLVER_PROMPT_OVERRIDES_PATH`
- `TEAMEVOLVER_STAGE_SETTINGS_PATH`

## Pipeline Stages

Evolution pipeline defines 11 stage nodes total (8 are LLM call stages with editable Prompts):

### Complete Stage Graph

```
ingest → session_filter → summarize → judge → group → ┬→ evolve_skill → ┬→ merge → dataset_synthesis → validate → replay_checklist → publish
                                                        └→ create_skill ─┘
```

| Stage ID | Type | Editable Prompt | Description |
|----------|------|----------------|-------------|
| `ingest` | IO | No | Session enqueue, no LLM calls |
| `session_filter` | LLM | Yes | Value classification, determines whether session is skill evidence, user memory, ordinary task, or chitchat |
| `summarize` | LLM | Yes | Session summary, builds lossless trajectory and generates trajectory-aware summary |
| `judge` | LLM | Yes | Session scoring, supplements multi-dimensional scores for sessions lacking reliable scores |
| `group` | Logic | No | Group by skill, no LLM calls |
| `evolve_skill` | LLM | Yes | Improve skill, decides improve/optimize_description/create/skip for existing skills |
| `create_skill` | LLM | Yes | Create skill, identifies reusable patterns from no-skill bucket and generates new skills |
| `merge` | LLM | Yes | Conflict merge, merges two evolved versions of same-named skill |
| `dataset_synthesis` | LLM | Yes | Test set generation, generates progressive test datasets with Checklist |
| `validate` | Gate | No | True replay validation, non-LLM logic |
| `replay_checklist` | LLM | Yes | Checklist judge, verifies replay results satisfy Checklist item by item |
| `publish` | IO | No | Publish, writes to skill library and syncs to cloud |

### Per-LLM-Stage Details

#### session_filter (Value Classification)

- **Module**: `teamEvolver.session_filter`
- **Symbol**: `_SESSION_CLASSIFIER_SYSTEM`
- **Default Temperature**: 0.0
- **Default Max Tokens**: 512
- **Input variables**: session summary (requests, tools, interactions, metrics)
- **Injects shared blocks**: No
- **Description**: Determines whether session enters evolution queue, and distinguishes team Skill evidence from user Memory candidates. This is first gate of pipeline; high temperature may cause classification instability, recommend keeping low temperature.

#### summarize (Session Summary)

- **Module**: `teamEvolver.evolve.stages.summarize`
- **Symbol**: `_SUMMARIZE_SESSION_SYSTEM`
- **Default Temperature**: 0.2
- **Default Max Tokens**: 100000
- **Input variables**: session JSON (interactions, tool calls, scores)
- **Injects shared blocks**: No
- **Description**: Generates trajectory-aware analysis summary for individual session for subsequent scoring and evolution. Requires large token cap for long sessions.

#### judge (Session Scoring)

- **Module**: `teamEvolver.evolve.stages.judge`
- **Symbol**: `_JUDGE_SYSTEM`
- **Default Temperature**: 0.1
- **Default Max Tokens**: 32768
- **Input variables**: session payload (trajectory, summary, artifacts, prior scores)
- **Injects shared blocks**: No
- **Description**: Supplements scores for sessions lacking reliable scores, outputs JSON dimensional scores. Low temperature ensures scoring consistency.

#### evolve_skill (Skill Improvement)

- **Module**: `teamEvolver.evolve.stages.execute`
- **Symbol**: `_EVOLVE_FROM_SESSIONS_SYSTEM`
- **Default Temperature**: 0.4
- **Default Max Tokens**: 16384
- **Input variables**: `{skill_name}`, current skill block, cross-cycle evidence, evaluation cohort, session evidence, existing skill names
- **Injects shared blocks**: Yes
- **Description**: This is core evolution stage. Makes improvement decisions for existing skills based on session evidence. Note: Original Prompt template contains three sentinel placeholders (`__GENERALIZATION_RULES__`, `__USER_OVERRIDE_RULE__`, `__EVIDENCE_ROUTING_RULES__`); preserve these placeholders when saving overrides; shared rule blocks auto-injected at runtime.

#### create_skill (Skill Creation)

- **Module**: `teamEvolver.evolve.stages.execute`
- **Symbol**: `_CREATE_FROM_SESSIONS_SYSTEM`
- **Default Temperature**: 0.4
- **Default Max Tokens**: 16384
- **Input variables**: cross-cycle evidence, evaluation cohort, session evidence, existing skill names
- **Injects shared blocks**: Yes
- **Description**: Identifies reusable patterns from no-skill-match session buckets and generates brand-new skills. Also contains shared block placeholders.

#### merge (Conflict Merge)

- **Module**: `teamEvolver.evolve.stages.execute`
- **Symbol**: `_MERGE_SKILL_SYSTEM`
- **Default Temperature**: 0.3
- **Default Max Tokens**: 8192
- **Input variables**: Version A (existing skill), Version B (new evolved version)
- **Injects shared blocks**: No
- **Description**: When same-named skill produces two conflicting evolved versions, merges them into one superior version.

#### dataset_synthesis (Test Set Generation)

- **Module**: `teamEvolver.dataset_synthesizer`
- **Symbol**: `_SYNTHESIZE_SYSTEM`
- **Default Temperature**: 0.3
- **Default Max Tokens**: 16384
- **Input variables**: `{case_count}`, `{min_requirements}`, `{max_requirements}`, candidate Skill, Session trajectories, team SOP evidence, replay seeds
- **Injects shared blocks**: No
- **Description**: Simultaneously generates progressive test datasets with Checklist from session evidence and cross-cycle SOP evidence. Template variables like `{case_count}` replaced with actual values at runtime.

#### replay_checklist (Checklist Judge)

- **Module**: `teamEvolver.true_replay`
- **Symbol**: `_CHECKLIST_JUDGE_SYSTEM`
- **Default Temperature**: 0.0
- **Default Max Tokens**: 8192
- **Input variables**: checklist, interactions, tool trajectory, workspace artifacts
- **Injects shared blocks**: No
- **Description**: After true replay completes, verifies Checklist items satisfied one by one. Judge only allows judgment based on observable evidence; temperature=0 ensures adjudication consistency.

## Prompt Studio Web Interface

Select "Prompt Studio" in Web console left navigation to enter. Interface divided into three main areas:

### Left: Pipeline Visualization

- Shows complete directed graph, nodes colored by type:
  - Gray: IO nodes (ingest, publish)
  - Blue: LLM stages (editable Prompt)
  - Purple: Logic nodes (group)
  - Amber: Validation gate (validate)
- Badges displayed on LLM nodes:
  - "Override": Stage has user-saved Prompt override
  - "Params Modified": Stage model parameters differ from defaults
- Click node to select corresponding stage
- Arrows between nodes show data flow

### Middle: Prompt List + Editor

- **Stage List**: Shows all 8 LLM stages with brief descriptions
- **Prompt Editor**: After selecting stage shows:
  - Stage name and description
  - Currently effective Prompt text box (editable)
  - "Show Default" toggle: Switch between viewing code-built default Prompt vs currently effective Prompt
  - If stage injects shared blocks, three shared block contents displayed:
    - `__GENERALIZATION_RULES__`: Generalization rules
    - `__USER_OVERRIDE_RULE__`: User override rules
    - `__EVIDENCE_ROUTING_RULES__`: Evidence routing rules
  - Save button (admin only)
  - Reset button (restore defaults, admin only)

### Right: Model Parameters + Test Panel

- **Model Parameters Area**:
  - Model: Specify model used for that stage (empty uses global `llm.model_id`)
  - Temperature: 0.0-2.0 slider
  - Max Tokens: Numeric input
  - Shows default value reference
  - Reset button restores default parameters

- **Test Panel**:
  - Session selector: Select one from recent 50 historical sessions as test input
  - "Run Test" button
  - Test results displayed in three tabs:
    - **System Prompt**: Actual system message sent this test (edited Prompt + expanded shared blocks)
    - **User Message**: Real user message constructed at test runtime (using same build logic as session stage)
    - **Model Output**: Output content returned by LLM

- **Evolution Process Parameters** (bottom, collapsible):
  - Editable core parameters for evolve and validation sections
  - Saved together when saving

## How to Use Prompt Studio

### Workflow: Edit and Test Prompt

Recommended iteration flow:

1. **Select Stage**: Click stage to modify in pipeline graph or list
2. **Understand Default Prompt**: Toggle "Show Default" to read default Prompt, understand its design intent
3. **Small-step Modification**: Make incremental changes in editor; don't overhaul at once
4. **Select Test Session**: Choose representative historical session from right panel
5. **Run Test**: Click "Run Test", view three-column results
   - System Prompt as expected (shared blocks correctly expanded)
   - User Message is format real stage would receive
   - Model Output quality meets requirements
6. **Compare**: Can record default Prompt output, compare with modified
7. **Save**: Click Save when test satisfied. Next evolution round immediately uses new Prompt after save
8. **Observe**: Observe subsequent evolution round effects in Dashboard and Evolution Pipeline views

### Test Panel Authenticity

Prompt Studio tests are not "simulations"—they use identical message construction logic as real evolution pipeline:

- `session_filter`: Uses `_session_summary()` to construct input identical to real classifier
- `summarize`: Uses `_build_session_payload()` to construct real payload
- `judge`: Ensures `_trajectory` and `_summary` metadata exist before calling `_build_judge_payload()`
- `evolve_skill`/`create_skill`: Calls `_build_session_evidence()` to construct real evidence blocks
- `merge`: Provides example A/B versions (since two conflicting versions needed)
- `dataset_synthesis`: Rendered using `render_synthesis_prompt()`
- `replay_checklist`: Constructs example checklist and interaction records

This means User Message you see in test panel is exactly message LLM receives in real evolution; test results reliably reflect actual behavior.

### Model Parameter Tuning Recommendations

Different stages suit different parameter configurations:

| Stage | Temperature Recommendation | Max Tokens Recommendation | Rationale |
|-------|---------------------------|--------------------------|-----------|
| session_filter | 0.0-0.1 | 512 | Classification tasks need determinism |
| summarize | 0.1-0.3 | 100000+ | Summary needs complete information coverage |
| judge | 0.0-0.2 | 32768 | Scoring needs consistency |
| evolve_skill | 0.3-0.6 | 16384 | Creative improvement needs moderate randomness |
| create_skill | 0.3-0.6 | 16384 | New skill creation needs creativity |
| merge | 0.2-0.4 | 8192 | Merging primarily fusion; doesn't need high randomness |
| dataset_synthesis | 0.2-0.4 | 16384 | Question generation needs diversity but cannot diverge |
| replay_checklist | 0.0 | 8192 | Strict evidence adjudication must be deterministic |

If certain stage needs stronger model (like creating complex skills), can specify stronger model ID individually for that stage; other stages use default model to balance cost and speed.

## Shared Blocks Explanation

Two skill authoring stages (`evolve_skill` and `create_skill`) Prompts contain three special placeholders; these placeholders automatically replaced with shared rule blocks when Prompt takes effect. When editing these stages' Prompts, preserve these placeholders:

- `__GENERALIZATION_RULES__`: Generalization rules; controls degree to which skills abstract from specific sessions to reusable SOPs
- `__USER_OVERRIDE_RULE__`: User override rules; handles user-manually-modified skills
- `__EVIDENCE_ROUTING_RULES__`: Evidence routing rules; controls how session evidence referenced and presented

Actual shared block content defined in `teamEvolver/evolve/stages/execute.py`, injected at runtime via `_inject_shared_blocks()` function. When viewing stage details in Prompt Studio interface, can see current content of three shared blocks.

If you delete these placeholders, shared rules won't be injected, potentially causing abnormal evolution behavior. Resetting to default Prompt restores placeholders.

## Variable Placeholders

Some Prompt templates contain runtime variable placeholders using `{variable_name}` syntax. Preserve these placeholders when editing:

| Stage | Placeholder | Runtime Value |
|-------|-------------|---------------|
| evolve_skill | `{skill_name}` | Name of skill being evolved |
| dataset_synthesis | `{case_count}` | Number of test cases to generate |
| dataset_synthesis | `{min_requirements}` | Minimum checklist items |
| dataset_synthesis | `{max_requirements}` | Maximum checklist items |

## Access Control

- **Admin**: Can edit Prompts, modify model parameters, save, reset
- **User**: Can view pipeline graph, view Prompt content, run tests, but cannot save modifications

## Prompt Version Management

Current Prompt Studio version uses simple override mechanism:

- Save override writes to `prompt_overrides.json`
- Reset deletes corresponding entry, restores default
- Modifications take effect immediately on next LLM call
- Configuration file is text format (JSON), can be manually backed up or placed under version control

Recommend manually backing up override files before major modifications:

```bash
cp ~/.teamEvolver/prompt_overrides.json ~/.teamEvolver/prompt_overrides.json.bak
cp ~/.teamEvolver/stage_settings.json ~/.teamEvolver/stage_settings.json.bak
```

## API Endpoints

Prompt Studio HTTP API at `/api/prompt-studio/`:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/prompt-studio/pipeline` | GET | Get pipeline graph with override markers |
| `/api/prompt-studio/prompts` | GET | Get all Prompt summary list |
| `/api/prompt-studio/prompts/<stage_id>` | GET | Get single stage complete details |
| `/api/prompt-studio/prompts/<stage_id>` | POST | Save Prompt override and model parameters |
| `/api/prompt-studio/prompts/<stage_id>/test` | POST | Run test |
| `/api/prompt-studio/prompts/<stage_id>/reset` | POST | Reset to default Prompt |
| `/api/prompt-studio/sessions` | GET | Get sessions list for testing |

Test interface request body:

```json
{
  "session_id": "abc-123",
  "system_prompt": "(optional) temporary test Prompt, not saved"
}
```

If `system_prompt` non-empty, test uses temporary Prompt (for quick trial-and-error); empty uses currently saved effective Prompt.

## Python API

To operate Prompt Studio from scripts or Notebooks, can directly call functions in `teamEvolver/evolve/prompt_studio.py`:

```python
from teamEvolver.evolve.prompt_studio import (
    list_prompts,
    get_prompt,
    set_override,
    reset_override,
    effective_prompt,
    set_stage_settings,
    reset_stage_settings,
    stage_call_options,
    pipeline_graph,
    run_stage_test,
)

# List all editable Prompts
prompts = list_prompts()

# Get stage details
detail = get_prompt("evolve_skill")
print(detail["default_prompt"])  # Default Prompt
print(detail["effective_prompt"])  # Currently effective Prompt
print(detail["shared_blocks"])  # Shared blocks (injection stages only)

# Set override
set_override("judge", "Your custom Prompt text...")

# Reset
reset_override("judge")

# Get Prompt that should be used at runtime (used by call sites)
prompt = effective_prompt("summarize", fallback=default_in_module)

# Set model parameters
set_stage_settings("evolve_skill", {
    "temperature": 0.5,
    "max_tokens": 32768,
    "model": "doubao-seed-evolving-large",
})

# Get stage call options
options = stage_call_options("evolve_skill")
# {"temperature": 0.5, "max_tokens": 32768, "model": "doubao-seed-evolving-large"}

# Get pipeline graph
graph = pipeline_graph()
```

## Relationship with White-box Configuration

Prompt Studio is complementary to global evolution parameters (evolve section):

- **Prompt Studio** controls per-stage LLM Prompts and model sampling parameters; it's "how to think" level configuration
- **evolve section configuration** controls macro parameters of evolution flow:
  - Evolution interval (`interval_seconds`)
  - Publish mode (`publish_mode`)
  - Evidence library size (`evidence_max_entries`)
  - Test set parameters (`dataset_test_cases`, `dataset_min_requirements`, etc.)
  - Human review settings (`human_review_enabled`)
  - Validation gates (`validation_max_rejections`)
- **validation section configuration** controls validation strategy:
  - Validation mode (`mode`: true_replay/replay)
  - Concurrency (`max_concurrency`)
  - Pass gates (`required_results`, `required_approvals`)

"Evolution Process Parameters" area on right panel of Prompt Studio can directly edit core evolve and validation parameters, updated together when saving Prompt.

## Best Practices

1. **Change one stage at a time**: After modification run several evolution rounds observing effects; confirm no issues before changing next
2. **Keep defaults as reference**: Frequently compare against default Prompt, understand impact of each change
3. **Verify using test panel**: Before saving always test with at least 2-3 different session types
4. **Modify evolve_skill/create_skill cautiously**: These are core stages directly affecting skill quality; backup before modifying
5. **Do not delete shared block placeholders**: Placeholders like `__GENERALIZATION_RULES__` are critical
6. **Keep judge-type stages at low temperature**: judge and replay_checklist recommend 0.0-0.1
7. **Monitor Token usage**: If you set excessively large max_tokens for a stage and model frequently outputs verbose content, consider tightening Prompt or reducing max_tokens
8. **Review skill outputs**: After modifying Prompts, pay close attention to candidate skill quality produced in next few evolution rounds; carefully evaluate in console candidate review page
