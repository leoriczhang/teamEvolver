# Skill Miner Guide

Skill Miner is teamEvolver's built-in document-to-skill mining subsystem that can automatically mine a set of same-topic domain documents into executable Agent Skills, while simultaneously generating reproducible evaluation benchmarks and stability reports.

Skill Miner code at `teamEvolver/skillminer/`, bridge layer with main service at `teamEvolver/proxy/skillminer_bridge.py`, white-box configuration managed by `teamEvolver/mining_settings.py` and `teamEvolver/mining_lifecycle.py`. Independent Web console at `teamEvolver/skillminer/web_console/`.

## Core Capabilities

1. **Sample Package Construction**: Organizes input documents into sample packages by evidence perspective and context capacity. Artifacts undergo programmatic segmentation quality validation (hard metrics like slice depth, cross-package deduplication, coverage, common/ consistency); automatically reruns once with violation details when hard flaws exist; aborts round if still failing.

2. **Semantic Discovery**: Inducts reusable decision units, processes, and boundaries from each sample package, annotating evidence gaps (GAP).

3. **Skill Compilation**: Generates `SKILL.md` and accompanying `EVALUATION.md`, provides confidence level and pending gap list.

4. **Reflection Loop**: When confidence level not converged and supplementary material exists, carries previous round gaps for targeted evidence supplementation (default max 3 rounds).

5. **Benchmark Generation**: Builds question bank per `EVALUATION.md`, supports multi-turn dialogue (simulated contextual participants) and single-turn answer scoring modes, plus difficulty distribution quotas.

6. **Trajectory Benchmark Independent Mining**: Directly receives teamEvolver/SkillGen or OpenAI messages-style trajectories, generates held-out Benchmark; does not enter sample package, semantic discovery, Skill compilation or LIFT flow.

7. **Multiple Builds & Stability Reruns**: Saves question banks from multiple builds as snapshots, computes intersection, then runs multiple sessions on intersection items to observe skill behavior stability under multi-turn dialogue.

8. **Coverage Report**: Statistics on semantic unit adoption rate, GAP resolution rate, and dimensional evidence coverage.

9. **Web Console**: Provides entry points for live runs, knowledge supplementation, scoring runs, and coverage reports.

10. **LIFT Adaptation**: Converts SkillMiner question banks to LIFT Suite v1 and Markdown scenarios; published to external LIFT workspace after manual editing, validation, and approval.

## Mining Pipeline

Skill Miner main pipeline contains three core Agent Skill-driven steps, plus reflection loop and evaluation stages:

### Step 1: Sample Package Constructor

- **Agent Skill**: `sample-package-constructor-agent-skill/`
- **Prompt**: Defined by `SAMPLE_PACKAGE_CONSTRUCTOR_AGENT_PROMPT` in `teamEvolver/skillminer/sample_package_constructor_agent_prompt.py`
- **Input**: Raw Markdown documents under `data/input/` directory
- **Output**: Structured sample packages under `sample_packages/` directory
- **Validation**: `validate_sample_packages.py` performs hard metric validation:
  - Slice depth strategy
  - Cross-package deduplication
  - Source document coverage
  - `common/` shared area consistency

### Step 2: Semantic Discovery

- **Agent Skill**: `semantic-discovery-agent-skill/`
- **Prompt**: Defined by `SEMANTIC_DISCOVERY_AGENT_PROMPT` in `teamEvolver/skillminer/semantic_discovery_agent_prompt.py`
- **Input**: Sample packages produced by Step 1
- **Output**: Semantic analysis reports per sample package under `semantic_reports/`
- **Produces**: Reusable decision units, process definitions, boundary conditions, evidence gap (GAP) lists

### Step 3: Skill & Evaluation Compiler

- **Agent Skill**: `evaluation-compiler-agent-skill/`
- **Prompt**: Defined by `EVALUATION_COMPILER_AGENT_PROMPT` in `teamEvolver/skillminer/evaluation_compiler_agent_prompt.py`
- **Input**: Semantic analysis reports from Step 2
- **Output**: `compiled_skill/<skill-name>/` directory
  - `SKILL.md`: Executable Agent skill file
  - `EVALUATION.md`: Evaluation dimensions and scoring criteria
  - Initial Benchmark questions

### Reflection Loop

If Skill confidence level doesn't reach threshold and supplementary material exists, pipeline re-enters Steps 1-3 with previous round GAP list for targeted evidence supplementation; default max 3 rounds. Configurable via `mining.pipeline.max_rounds`.

### Benchmark Construction & Execution

After Skill compilation completes, `run_benchmark.py` builds baseline question bank per `EVALUATION.md` and executes evaluation:

- **Multi-turn dialogue mode** (default): Simulates contextual participants (customer_sim) disclosing facts progressively, tested Skill responds multi-turn, finally scored by judge
- **Single-turn answer mode**: Provides complete context in one shot, tested Skill answers single-turn then judge scores
- **Difficulty distribution**: Configurable easy/medium/hard ratio, default `easy:4,medium:7,hard:5` (16 questions total)

## Prerequisites

### Runtime Environment

- Python 3.11～3.13
- Hermes runtime installed (via project install script):
  ```bash
  bash scripts/install_teamEvolver.sh
  scripts/project_hermes.sh --version
  ```
- Available model provider; defaults to Volcengine Ark

### Model Configuration

API Key injected via environment variable `ARK_API_KEY`, do not commit to repository:

```bash
export ARK_API_KEY="your-api-key"
```

First run automatically creates project-local Hermes config `.hermes_home/config.yaml` from `teamEvolver/skillminer/hermes/config.yaml.example`.

Project-specific Hermes config isolated from global `~/.hermes/config.yaml`, can independently modify model:

```bash
# Use Hermes model selector (changes only written to project HERMES_HOME)
scripts/project_hermes.sh model

# Or edit directly
${EDITOR:-vi} teamEvolver/skillminer/.hermes_home/config.yaml
```

## Quick Start

### 1. Prepare Input Documents

Place same-topic Markdown documents into `teamEvolver/skillminer/data/input/` directory.

### 2. Static Self-Check (no model calls)

```bash
cd teamEvolver/skillminer
python3 test_pipeline_static.py
```

### 3. Run Mining Pipeline

```bash
# Run one round mining (Steps 1-3, no reflection loop)
python3 run_pipeline.py --input data/input --max-rounds 1

# Full run (max 3 reflection rounds)
python3 run_pipeline.py --input data/input --max-rounds 3

# Step1 hard flaws only warn without abort
python3 run_pipeline.py --input data/input --no-strict-step1
```

### 4. View Artifacts

```
compiled_skill/<skill-name>/
├── SKILL.md          # Generated executable skill
└── EVALUATION.md     # Evaluation criteria
```

### 5. Build & Run Benchmark

```bash
# Build and run with default difficulty distribution
python3 run_benchmark.py

# Specify difficulty distribution and question count
python3 run_benchmark.py --difficulty-dist "easy:3,medium:8,hard:7" --target-total 16

# Build question bank only without running
python3 run_benchmark.py --build-only

# Reuse existing question bank for scoring
python3 run_benchmark.py --skip-build

# Single-turn answer mode
python3 run_benchmark.py --mode single

# Quick smoke (run only 3 questions)
python3 run_benchmark.py --limit 3
```

### 6. Generate Coverage Report

```bash
python3 run_coverage_report.py
```

## Trajectory Benchmark Independent Mining

Besides document-to-Skill mining, Skill Miner also supports mining held-out Benchmark directly from Agent session trajectories; this path does not generate SKILL.md, only produces evaluation questions.

### HTTP API (teamEvolver unified service)

```bash
POST /api/mining/trajectory-benchmarks
```

Request example:

```json
{
  "dataset_name": "skillgen-evolution",
  "target_total": 18,
  "difficulty_dist": "easy:3,medium:10,hard:5",
  "trajectories": [
    {
      "session_id": "session-001",
      "turns": [
        {
          "turn_num": 1,
          "prompt_text": "User task",
          "response_text": "Agent answer",
          "tool_calls": [],
          "tool_results": [],
          "success": true
        }
      ]
    }
  ]
}
```

Supports three trajectory formats:
- teamEvolver `turns` format
- OpenAI `messages` format
- `steps`/`events` format containing `action`/`observation`

Service automatically deduplicates, limits scale, and redacts keys, phone numbers, emails, and local user paths. Returns HTTP 202 and run_id; query progress via polling status endpoint:

```bash
GET /api/mining/trajectory-benchmarks/<run_id>
```

Completed artifacts at `trajectory_benchmarks/<run_id>/`:
- `benchmark.jsonl`: teamEvolver-benchmark-v1 format question bank
- `BENCHMARK.md`: Human-readable version
- `manifest.json`: Source summary, difficulty and dimension statistics

### Python API

teamEvolver internal components can call directly:

```python
from teamEvolver import amine_benchmark_from_trajectories

result = await amine_benchmark_from_trajectories({
    "dataset_name": "skillgen-evolution",
    "target_total": 18,
    "trajectories": trajectories,
})
```

Query historical artifacts:

```python
from teamEvolver.skillminer.trajectory_benchmark import (
    list_trajectory_benchmark_runs,
    get_trajectory_benchmark_run,
)
```

## Multiple Builds & Stability Reruns

Single question generation has randomness; recommend building question banks multiple times and verifying stability:

```bash
cd teamEvolver/skillminer

# 1) Build question banks multiple times and save snapshots
python3 run_benchmark.py --build-only
python3 run_multi_session.py snapshot

# 2) View snapshots and intersection overview
python3 run_multi_session.py status

# 3) Compute intersection (by scenario text similarity, fallback to dimension)
python3 run_multi_session.py intersect

# Force intersection by evaluation dimension
python3 run_multi_session.py intersect --by-dimension

# 4) Run N sessions each on intersection items
python3 run_multi_session.py run --sessions 3

# Rerun specific dimension only (reuse remaining results)
python3 run_multi_session.py run --sessions 3 --only EVAL-01
```

Artifacts at `benchmark_sessions/`:
- `snapshots/build-N.jsonl`: Snapshots from each build
- `intersection.md`: Intersection list
- `DIM_*/session-N.md`: Complete dialogue and grading per session
- `SESSIONS_REPORT.md`: Per-dimension stability summary

## Human Checkpoints

Skill Miner sets multiple human review nodes during execution to ensure mining quality:

1. **Step 1 Sample Package Review** (optional, controlled by `strict_step1`): When sample package segmentation quality below standard, abort waiting for manual correction of input documents or adjustment of segmentation strategy
2. **Skill Output Review**: Manual inspection of SKILL.md and EVALUATION.md accuracy after compilation
3. **Benchmark Question Review**: Auto-generated questions require manual verification of scenario rationality and gold answer accuracy
4. **LIFT Pre-Publish Review**: Must pass manual review approval before publishing to external LIFT workspace

Human checkpoint logic at `teamEvolver/skillminer/human_checkpoints.py`, test file `test_skillminer_human_checkpoints.py`.

## Web Console

### Independent Console (Skill Miner Native)

Start independent Skill Miner Web console:

```bash
cd teamEvolver/skillminer
python3 web_console/server.py
```

Open `http://127.0.0.1:8765`. Console implements SSE real-time logging using standard library; only provides live pipeline runs, no simulation mode.

Features include:
- Upload input documents
- Start mining tasks, view log output in real-time
- View sample packages and semantic reports
- Download compiled SKILL.md
- Start Benchmark scoring runs
- View coverage reports
- Knowledge supplementation entry

Frontend code at `teamEvolver/skillminer/web_console/static/` (`index.html`, `app.js`, `styles.css`).

### Unified Console (teamEvolver Main Service)

After starting teamEvolver main service, unified console also provides Skill Miner entry:

- Mining task submission and status monitoring
- Model configuration (independent of global LLM configuration)
- Pipeline parameter configuration
- Prompt white-box editing (10 editable Prompts)
- Benchmark runs and result viewing
- LIFT integration review and publishing
- Coverage report viewing
- Trajectory Benchmark submission

## LIFT Integration

After Skill Miner generates `benchmark.jsonl`, auto-generates LIFT pending-review drafts written to `lift_datasets/drafts/`. To disable:

```bash
export SKILLMINER_LIFT_AUTO_DRAFT=0
```

### Prepare LIFT Environment

```bash
# At teamEvolver repository root
bash scripts/setup_lift.sh

# Optional: also create Python 3.12 virtual environment
bash scripts/setup_lift.sh --install-deps
export TEAMEVOLVER_LIFT_PYTHON="$PWD/external/LIFT/.venv-teamEvolver/bin/python"
```

Or reuse existing LIFT checkout:

```bash
export TEAMEVOLVER_LIFT_ROOT=/absolute/path/to/LIFT
export TEAMEVOLVER_LIFT_PYTHON=/absolute/path/to/python
```

### Publishing Flow

1. Enter evaluation center in unified console
2. Create LIFT draft from Skill with generated Benchmark
3. Check questions one by one and edit `query`, content requirements, trajectory requirements; confirm warmup/holdout split
4. Save and pass structural validation, click "Human Review Passed"
5. Click "Publish to LIFT"
6. Select runtime and start; view run logs in real-time

Post-publish data structure:

```
<LIFT>/assets/benchmarks/teamEvolver/<suite>.json
<LIFT>/assets/benchmark_mds/teamEvolver/<suite>/
├── train/q*/q*.md
├── test/q*/q*.md
└── skills/<skill>/SKILL.md
```

## Available Miner Agent Skills

Skill Miner includes three built-in mining Agent Skills under `teamEvolver/skillminer/`:

### evaluation-compiler

- **Directory**: `evaluation-compiler-agent-skill/`
- **Purpose**: Compiles semantic discovery reports into SKILL.md and EVALUATION.md
- **Assets**:
  - `assets/evaluation_template.md`: EVALUATION.md template
  - `assets/skill_template.md`: SKILL.md template

### sample-package-constructor

- **Directory**: `sample-package-constructor-agent-skill/`
- **Purpose**: Segments raw documents into quality-compliant sample packages
- **Assets**:
  - `assets/coverage_report_template.md`: Coverage report template
  - `assets/mirror-config.example.yaml`: Mirror config example
  - `assets/package_index_template.md`: Package index template
  - `assets/package_note_template.md`: Package note template
- **References**:
  - `references/global-coverage-policy.md`: Global coverage policy
  - `references/output-folder-spec.md`: Output directory spec
  - `references/package-count-decision.md`: Package count decision
  - `references/selection-priority.md`: Selection priority
  - `references/single-file-subsetting-policy.md`: Single-file segmentation policy
  - `references/slice-depth-policy.md`: Slice depth policy
  - `references/source-coverage-policy.md`: Source document coverage policy
- **Scripts**: `scripts/README.md`

### semantic-discovery

- **Directory**: `semantic-discovery-agent-skill/`
- **Purpose**: Mines semantic units, decision flows, and knowledge gaps from sample packages
- **Assets**:
  - `assets/judgment_structure_template.md`: Judgment structure template
  - `assets/rebuttal_template.md`: Rebuttal template
  - `assets/report_filename_examples.md`: Report naming examples
  - `assets/report_template.md`: Report template
  - `assets/semantic_role_template.md`: Semantic role template
- **References**:
  - `references/common-false-structures.md`: Common false structures
  - `references/evidence-priority.md`: Evidence priority
  - `references/input-output-spec.md`: Input/output spec
  - `references/judgment-structure-screening.md`: Judgment structure screening
  - `references/local-vs-global-evidence-policy.md`: Local vs global evidence policy
  - `references/output-contract.md`: Output contract
  - `references/semantic-role-screening.md`: Semantic role screening
- **Scripts**: `scripts/README.md`

## Configuration (mining Section)

Skill Miner configuration in config.yaml `mining` section, dynamically modifiable via console white-box panel.

### Model Configuration (mining.model)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `provider` | string | inherits global | Model provider |
| `model_id` | string | inherits global | Model ID |
| `base_url` | string | inherits global | API Base URL |
| `api_key` | string | inherits global | API Key |
| `max_tokens` | integer | `100000` | Maximum output tokens |
| `context_length` | integer | `240000` | Context window size |
| `temperature` | float | `0.2` | Sampling temperature |

Set to empty object `{}` to inherit global `llm` configuration.

### Pipeline Configuration (mining.pipeline)

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `max_rounds` | integer | `3` | Reflection loop maximum rounds (1-20) |
| `max_retries` | integer | `2` | Single-step maximum retries (0-20) |
| `retry_backoff_seconds` | float | `0.8` | Retry backoff time (0-300 seconds) |
| `oneshot_timeout_seconds` | integer | `1800` | Single mining timeout (30-86400 seconds, default 30 minutes) |
| `step1_validation_retries` | integer | `1` | Step1 validation failure retries (0-10) |
| `strict_step1` | boolean | `true` | Whether Step1 validation failure aborts current round |
| `benchmark_target_total` | integer | `16` | Benchmark target total questions (1-500) |
| `benchmark_difficulty_dist` | string | `"easy:4,medium:7,hard:5"` | Difficulty distribution |
| `benchmark_max_turns` | integer | `5` | Multi-turn Benchmark maximum conversation turns (1-50) |

### Prompts Configuration (mining.prompts)

10 editable Prompts total:

| Prompt ID | Description |
|-----------|-------------|
| `sample_package` | Sample package construction |
| `semantic_discovery` | Semantic discovery |
| `evaluation_compiler` | Skill & evaluation compilation |
| `benchmark_generation` | Benchmark question generation |
| `benchmark_usage` | Benchmark single-turn answer |
| `benchmark_participant` | Benchmark simulated participant |
| `benchmark_skill_reply` | Benchmark tested Skill reply |
| `benchmark_judge_single` | Benchmark single-turn judge |
| `benchmark_judge_dialogue` | Benchmark multi-turn judge |
| `trajectory_benchmark_generation` | Trajectory Benchmark mining |

First three Prompts load defaults from corresponding `*_agent_prompt.py` files; last seven are dynamically generated default Prompts. Modified in console and saved to `mining.prompts` configuration section.

## Artifact Directory Structure

Runtime-generated artifact directories Git-ignored by default:

```
teamEvolver/skillminer/
├── data/input/              # Domain documents to mine (user-provided)
├── sample_packages/         # Step 1 artifacts
├── semantic_reports/        # Step 2 artifacts
├── compiled_skill/          # Step 3 artifacts: SKILL.md, EVALUATION.md
├── reflection_rounds/       # Reflection round intermediate artifacts
├── run_history/             # Previous batch outputs saved when new task starts
├── benchmark_sessions/      # Multiple build snapshots, intersections, stability reruns
├── trajectory_benchmarks/   # Trajectory Benchmark artifacts
├── lift_datasets/           # LIFT pending-review drafts and published snapshots
├── logs/                    # Runtime logs
└── .hermes_home/            # Project Hermes independent config (should not commit)
```

Clean runtime artifacts (preserves data/input/ and .hermes_home/):

```bash
bash clean_artifacts.sh
```

After deleting `.hermes_home/`, next run reinitializes from `hermes/config.yaml.example`.

## Bridge Layer

`teamEvolver/proxy/skillminer_bridge.py` provides HTTP API bridge between main service and Skill Miner, including:

- Mining task submission and status queries
- Trajectory Benchmark interface
- LIFT integration interface
- Artifact listing and download
- White-box configuration read/write

Main service `/api/mining/*` routes call lifecycle management functions in `teamEvolver/mining_lifecycle.py` via this bridge layer.

## Data Security

- Only use domain documents you have right to process and distribute
- Do not commit `.env`, `.hermes_home/`, model responses, runtime logs, or generated artifacts to version control
- Generated Skills and Benchmarks should be reviewed by domain experts before production use
- Trajectory Benchmark mining automatically redacts keys, phone numbers, emails, and local user paths; source trajectory original text not persisted, `manifest.json` retains only IDs, counts, and SHA-256 digests
