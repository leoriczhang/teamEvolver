# Configuration Reference

This guide details all configuration options for teamEvolver. Configuration file is located at `~/.teamEvolver/config.yaml`, modifiable via CLI commands or direct YAML editing.

## Configuration File Location

teamEvolver uses YAML format configuration, default path:

```
~/.teamEvolver/config.yaml
```

On first run, if configuration file doesn't exist, CLI prompts you to run `teamEvolver config` for initialization. Configuration is deep-merged from defaults in `teamEvolver/config_store/defaults.py` with user customizations.

## CLI Configuration Commands

Use `teamEvolver config` command to read or modify configuration:

```bash
# View all current configuration
teamEvolver config show

# Read single configuration item
teamEvolver config <key>

# Set single configuration item (supports dot-separated nested keys)
teamEvolver config <key> <value>
```

Examples:

```bash
teamEvolver config llm.api_key sk-xxxxxxxx
teamEvolver config service.port 52010
teamEvolver config sharing.enabled true
teamEvolver config langfuse.tracing_enabled true
```

CLI automatically converts string values to appropriate types (boolean, integer, float).

## Configuration Section Reference

### team Section

Basic team information configuration.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `display_name` | string | `"Team"` | Team display name, identifies team in console and shared skills. Override via environment variable `EVOLVE_TEAM_DISPLAY_NAME`. |

### llm Section

Large language model configuration used by evolution pipeline.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `provider` | string | `"custom"` | LLM service provider; currently supports custom OpenAI-compatible interfaces. |
| `model_id` | string | `"doubao-seed-evolving"` | Model identifier. |
| `api_base` | string | `"https://ark.cn-beijing.volces.com/api/v3"` | API base URL; must be OpenAI `/chat/completions` compatible endpoint. |
| `api_key` | string | `""` | API key for authenticating upstream model service. |
| `max_tokens` | integer | `100000` | Maximum output tokens per LLM call. |
| `temperature` | float | `0.4` | Sampling temperature, range 0.0–2.0. |

### service Section

HTTP service listening configuration.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `port` | integer | `52010` | Service listening port. |
| `host` | string | `"0.0.0.0"` | Service bind address. Production environments recommend `"127.0.0.1"` with reverse proxy exposure. |

### skills Section

Local skill library configuration.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | boolean | `true` | Whether to enable skill management. |
| `dir` | string | `"~/.hermes/skills"` | Local skills directory path. Default points to Hermes skills directory for seamless integration. |

### sharing Section

Skill sharing and OpenViking cloud sync configuration.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | boolean | `true` | Whether to enable cloud skill sharing. |
| `backend` | string | `"viking"` | Sharing backend; currently only `"viking"` (OpenViking) supported. |
| `viking_deployment` | string | `"cloud"` | OpenViking deployment mode: `"cloud"` (Volcengine hosted) or `"local"` (self-hosted openviking-server). |
| `viking_endpoint` | string | `""` | OpenViking API endpoint. Empty auto-derives based on `viking_deployment`. |
| `viking_api_key` | string | `""` | Generic API key (backward compatible; per-scope keys recommended). |
| `viking_personal_api_key` | string | `""` | Personal space API key. |
| `viking_personal_api_keys` | list | `[]` | List of multiple personal space API keys. |
| `viking_team_api_key` | string | `""` | Team space API key. |
| `viking_root_prefix` | string | `"team-skill-evolver"` | Namespace root prefix for teamEvolver resources in OpenViking; do not modify casually. |
| `viking_agent` | string | (constant) | OpenViking Agent namespace, fixed by code constant. |
| `viking_account` | string | `"default"` | Viking account identifier. |
| `viking_user` | string | `"default"` | Viking user identifier. |
| `viking_personal_user` | string | `""` | Personal space username. |
| `viking_customer_id` | string | `""` | Customer ID for DreamCycle memory space targeting. |
| `viking_group_id` | string | `""` | Group ID. |
| `viking_agent_id` | string | `""` | Agent ID. |
| `user_alias` | string | `""` | User alias for session attribution marking. |
| `auto_pull_on_start` | boolean | `true` | Auto-pull latest skills from cloud on startup. |
| `push_min_injections` | integer | `5` | Minimum injection count threshold before pushing skills to cloud. |
| `push_min_effectiveness` | float | `0.3` | Minimum effectiveness threshold before pushing skills to cloud. |
| `session_upload_interval` | integer | `0` | Session auto-upload interval in seconds; 0 disables upload. |
| `skill_reload_mode` | string | `"poll"` | Skill reload mode: `"off"` (disabled), `"poll"` (polling), `"callback"` (webhook). |
| `skill_reload_interval_seconds` | integer | `30` | Skill check interval in polling mode; minimum 5. |
| `endpoint` | string | `""` | Generic endpoint (uses viking_endpoint when empty). |
| `skill_backend` | string | `""` | Skill-dedicated backend (uses backend when empty). |
| `session_backend` | string | `""` | Session-dedicated backend (uses backend when empty). |

### evolve Section

Evolution pipeline core parameter configuration.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `interval_seconds` | integer | `600` | Evolution round interval in seconds; how often evolution cycle executes. |
| `publish_mode` | string | `"validated"` | Candidate skill publish mode: `"validated"` (auto-publish after validation passes), `"manual"` (all human review), `"off"` (no auto-publish). |
| `human_review_enabled` | boolean | `true` | Whether to enable human review workflow. |
| `human_review_timeout_seconds` | integer | `86400` | Human review timeout in seconds; default 24 hours. |
| `evidence_enabled` | boolean | `true` | Whether to enable evidence collection mechanism. |
| `evidence_max_entries` | integer | `400` | Maximum evidence library entries. |
| `evidence_recent_limit` | integer | `20` | Recent evidence window size. |
| `evidence_historical_limit` | integer | `20` | Historical evidence window size. |
| `evidence_replay_cases_per_window` | integer | `1` | Replay cases per evidence window. |
| `evidence_change_debt_threshold` | integer | `3` | Change debt threshold; exceeding triggers forced evolution. |
| `dataset_synthesis_enabled` | boolean | `true` | Whether to enable automatic test dataset synthesis. |
| `dataset_test_cases` | integer | `2` | Test cases generated per synthesis. |
| `dataset_min_requirements` | integer | `12` | Minimum checklist items per test case. |
| `dataset_max_requirements` | integer | `24` | Maximum checklist items per test case. |
| `dataset_disclosure_batch_size` | integer | `4` | Progressive disclosure batch size. |
| `validation_max_rejections` | integer | `1` | Pause evolution for skill after consecutive rejections count. |
| `use_session_judge` | boolean | `true` | Whether to use session value classifier. |
| `candidate_coalesce_enabled` | boolean | `true` | Whether to enable candidate coalescing. |
| `bundle_text_extensions` | list | `[".py", ".sh"]` | Extensions treated as text files in skill bundles. |
| `bundle_max_file_bytes` | integer | `262144` | Maximum single file bytes in skill bundles (256KB). |
| `bundle_max_prompt_bytes` | integer | `786432` | Maximum prompt bytes in skill bundles (768KB). |
| `bundle_allow_delete` | boolean | `true` | Whether to allow file deletion during evolution. |
| `bundle_static_checks_enabled` | boolean | `true` | Whether to enable skill bundle static checks. |
| `server_url` | string | `"http://127.0.0.1:52010"` | Evolution service self-referencing URL. |

### dreamcycle Section

DreamCycle memory maintenance engine configuration. DreamCycle is teamEvolver's automated memory maintenance subsystem running during inactive hours.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | boolean | `false` | Whether to enable DreamCycle memory maintenance. |
| `auto_start` | boolean | `false` | Whether to auto-start with main service. |
| `active_start_hour` | integer | `0` | Active window start hour (0-23, 24-hour). Default 0:00 AM. |
| `active_end_hour` | integer | `6` | Active window end hour (0-23). Default 6:00 AM. |
| `rounds_per_window` | integer | `3` | Number of rounds executed per active window. |
| `round_interval_minutes` | integer | `90` | Round interval in minutes. |
| `max_turns_per_job` | integer | `25` | Maximum conversation turns per Job. |
| `max_consecutive_errors` | integer | `3` | Consecutive error threshold; exceeds triggers backoff retry. |
| `retry_delay_seconds` | integer | `300` | Backoff wait time after errors in seconds. |
| `enabled_jobs` | list | `["team_overview","deduplication","cleanup","onboarding_check","consolidate"]` | Enabled Jobs list. Available Jobs: `team_overview`, `deduplication`, `cleanup`, `onboarding_check`, `consolidate`. |
| `llm_model` | string | `""` | Model used by DreamCycle; empty reuses global LLM config. |
| `llm_base_url` | string | `""` | DreamCycle-dedicated API Base URL. |
| `llm_api_key` | string | `""` | DreamCycle-dedicated API Key. |
| `llm_max_tokens` | integer | `4096` | DreamCycle LLM max output tokens. |
| `temperature` | float | `0.3` | DreamCycle LLM sampling temperature. |
| `embed_model` | string | `""` | Embedding model name; configures enables semantic deduplication. |
| `embed_base_url` | string | `""` | Embedding model API Base URL. |
| `embed_api_key` | string | `""` | Embedding model API Key. |
| `dedup_merge_threshold` | float | `0.86` | Semantic similarity merge threshold (0-1). |
| `dedup_warn_threshold` | float | `0.72` | Semantic similarity warning threshold (0-1). |
| `customer_id` | string | `""` | Target customer ID. |
| `state_dir` | string | `""` | State files directory. |
| `log_level` | string | `"INFO"` | Log level. |
| `daemon_command` | string | `"dreamcycle --daemon"` | DreamCycle daemon startup command. |
| `trigger_command` | string | `"dreamcycle --once"` | DreamCycle single-trigger command. |
| `viking_agent` | string | `"dreamcycle"` | DreamCycle Agent namespace in OpenViking. |
| `job_prompts` | dict | `{}` | Per-Job Prompt override configuration. |
| `job_settings` | dict | `{}` | Per-Job runtime parameter override configuration. |

### validation Section

Candidate skill validation configuration.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | boolean | `true` | Whether to enable background validation. |
| `mode` | string | `"true_replay"` | Validation mode: `"true_replay"` (full workspace isolation) or `"replay"` (lightweight replay). |
| `max_concurrency` | integer | `1` | Maximum concurrent validation tasks. |
| `required_results` | integer | `3` | Valid validation results required for publishing. |
| `required_approvals` | integer | `2` | Approval passes required for publishing. |
| `agentshub_url` | string | `""` | Pi Agent service URL (distributed replay HTTP endpoint). Config key name retained for historical reasons. |
| `agentshub_api_key` | string | `""` | Pi Agent Replay/Sync API Key. Config key name retained for historical reasons. |
| `idle_after_seconds` | integer | `300` | Idle wait time in seconds; Worker enters sleep after exceeding. |
| `poll_interval_seconds` | integer | `60` | Poll interval in seconds. |
| `max_jobs_per_day` | integer | `5` | Maximum validation jobs per day. |

### langfuse Section

Langfuse observability and session pull configuration. Langfuse integration has two independent modes: inbound session pull (pulling sessions from Langfuse into evolution pipeline) and outbound tracing (sending LLM calls during evolution to Langfuse).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | boolean | `false` | Whether to enable inbound session pull mode. |
| `host` | string | `"https://cloud.langfuse.com"` | Langfuse service address. Self-hosted instances change to your address. |
| `public_key` | string | `""` | Langfuse Public Key for API access. |
| `secret_key` | string | `""` | Langfuse Secret Key. |
| `tracing_enabled` | boolean | `false` | Whether to enable outbound LLM call tracing. |
| `tracing_environment` | string | `"local"` | Tracing environment tag; distinguishes deployment environments in Langfuse UI (e.g., production, staging, local). |
| `tracing_release` | string | `""` | Tracing release tag. |
| `tracing_sample_rate` | float | `1.0` | Tracing sample rate (0.0-1.0); 1.0 means full sampling. |
| `tracing_capture_content` | boolean | `true` | Whether to capture LLM input/output content. Disabled records metadata only. |
| `tracing_flush_at` | integer | `1` | Batch flush after accumulating traces. |
| `tracing_flush_interval_seconds` | float | `1.0` | Periodic flush interval in seconds. |
| `timeout_seconds` | integer | `30` | Langfuse API request timeout in seconds. |
| `page_limit` | integer | `50` | Page size for paginated pulls. |
| `max_sessions` | integer | `100` | Maximum sessions per pull. |
| `default_environment` | list | `[]` | Default environment tag filter list. |
| `default_user_id` | string | `""` | Default user ID filter. |
| `default_tags` | list | `[]` | Default tags filter. |
| `default_release` | string | `""` | Default release filter. |
| `default_version` | string | `""` | Default version filter. |
| `default_trace_name` | string | `""` | Default trace name filter. |
| `mapper_enabled` | boolean | `false` | Enable the custom trace mapper. When on, a user-authored `map_trace` runs for every trace during a pull. |
| `mapper_code` | string | `""` | Source of the user-authored `map_trace(trace, observations)` function returning a (possibly partial) standard evolution turn. |

#### Custom Trace Mapping (Standard Evolution Format)

Langfuse traces and observations share one shape; observations only add nesting through `parentObservationId`. Mapping them into the standard evolution turn is otherwise mechanical, so beyond the built-in mapping teamEvolver lets an admin own that step with a small function:

```python
def map_trace(trace, observations):
    # trace:        dict — one Langfuse trace (input/output/metadata/...)
    # observations: list[dict] — its observations (flat, nested via parentObservationId)
    # return:       dict — a standard-format evolution turn. A partial dict is
    #               deep-merged over the built-in mapping; return None to accept
    #               the built-in mapping as-is.
    usage = (trace.get("metadata") or {}).get("usage") or {}
    return {
        "prompt_text": str(trace.get("input") or ""),
        "response_text": str(trace.get("output") or ""),
        "metrics": {"total_tokens": int(usage.get("total") or 0)},
    }
```

- The function runs in a restricted namespace: `json / re / math / datetime` are available, while `import` and filesystem access are disabled. Only admins may edit it (it is executable configuration).
- The return value is **deep-merged** over the built-in mapping — override only the fields you care about; the rest fall back to the built-in logic.
- The console's Langfuse page has a "Custom Trace Mapping" panel to edit the code, insert the reference template, and dry-run it against a bundled sample or a pasted trace, comparing the mapped turn to the built-in mapping side by side. A "Standard Format" button in the panel header opens a dialog documenting the evolution turn format (every field's meaning plus a worked example).
- If the code raises during a pull, that session falls back to the built-in mapping so one bad trace never fails the whole batch.

### mining Section

Skill Miner (document-to-skill mining) configuration.

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `model.provider` | string | inherits global llm | Mining-dedicated model provider. |
| `model.model_id` | string | inherits global llm | Mining-dedicated model ID. |
| `model.base_url` | string | inherits global llm | Mining-dedicated API Base URL. |
| `model.api_key` | string | inherits global llm | Mining-dedicated API Key. |
| `model.max_tokens` | integer | inherits global llm | Mining model max output tokens. |
| `model.context_length` | integer | `240000` | Model context window size. |
| `model.temperature` | float | `0.2` | Mining model sampling temperature. |
| `pipeline.max_rounds` | integer | `3` | Reflection loop maximum rounds. |
| `pipeline.max_retries` | integer | `2` | Single-step maximum retries. |
| `pipeline.retry_backoff_seconds` | float | `0.8` | Retry backoff time in seconds. |
| `pipeline.oneshot_timeout_seconds` | integer | `1800` | Single mining timeout in seconds; default 30 minutes. |
| `pipeline.step1_validation_retries` | integer | `1` | Step1 sample package validation failure retries. |
| `pipeline.strict_step1` | boolean | `true` | Whether Step1 validation failure aborts current round. |
| `pipeline.benchmark_target_total` | integer | `16` | Benchmark target total questions. |
| `pipeline.benchmark_difficulty_dist` | string | `"easy:4,medium:7,hard:5"` | Benchmark difficulty distribution. |
| `pipeline.benchmark_max_turns` | integer | `5` | Multi-turn Benchmark maximum conversation turns. |
| `prompts` | dict | `{}` | Per-mining-stage Prompt overrides. |

### openrouter Section

OpenRouter fallback routing configuration (optional).

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `app_name` | string | `"teamEvolver"` | OpenRouter application name. |
| `app_url` | string | `""` | Application URL. |
| `route` | string | `"fallback"` | Routing strategy. |
| `fallback_models` | string | `""` | Fallback model list. |
| `data_policy` | string | `""` | Data policy. |

## Environment Variable Overrides

Besides YAML configuration file, following environment variables can override corresponding configuration items (highest priority):

| Environment Variable | Corresponding Config |
|---------------------|---------------------|
| `EVOLVE_TEAM_DISPLAY_NAME` | `team.display_name` |
| `EVOLVE_MODEL` | `llm.model_id` |
| `EVOLVE_LLM_MAX_TOKENS` | `llm.max_tokens` |
| `EVOLVE_LLM_TEMPERATURE` | `llm.temperature` |
| `EVOLVE_USE_SESSION_JUDGE` | `evolve.use_session_judge` |
| `EVOLVE_PUBLISH_MODE` | `evolve.publish_mode` |
| `EVOLVE_VALIDATION_MAX_REJECTIONS` | `evolve.validation_max_rejections` |
| `EVOLVE_HUMAN_REVIEW_ENABLED` | `evolve.human_review_enabled` |
| `EVOLVE_HUMAN_REVIEW_TIMEOUT_SECONDS` | `evolve.human_review_timeout_seconds` |
| `EVOLVE_INTERVAL` | `evolve.interval_seconds` |
| `EVOLVE_EVIDENCE_ENABLED` | `evolve.evidence_enabled` |
| `EVOLVE_EVIDENCE_MAX_ENTRIES` | `evolve.evidence_max_entries` |
| `EVOLVE_INGEST_API_KEY` | Global ingest endpoint API Key |
| `TEAMEVOLVER_PROXY_API_KEY` | Model proxy API Key |
| `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` | `langfuse.host` |
| `LANGFUSE_PUBLIC_KEY` | `langfuse.public_key` |
| `LANGFUSE_SECRET_KEY` | `langfuse.secret_key` |
| `LANGFUSE_TRACING_ENABLED` | `langfuse.tracing_enabled` |
| `LANGFUSE_TRACING_ENVIRONMENT` | `langfuse.tracing_environment` |
| `LANGFUSE_SAMPLE_RATE` | `langfuse.tracing_sample_rate` |
| `ARK_API_KEY` | Volcengine Ark API Key (used by Skill Miner) |

## Configuration File Example

Following is complete example of `~/.teamEvolver/config.yaml`:

```yaml
team:
  display_name: "My Team"

llm:
  provider: "custom"
  model_id: "doubao-seed-evolving"
  api_base: "https://ark.cn-beijing.volces.com/api/v3"
  api_key: "sk-xxxxxxxx"
  max_tokens: 100000
  temperature: 0.4

service:
  port: 52010
  host: "127.0.0.1"

skills:
  enabled: true
  dir: "~/.hermes/skills"

sharing:
  enabled: true
  backend: "viking"
  viking_deployment: "cloud"
  viking_personal_api_key: "vk-xxxxxxxx"
  viking_team_api_key: "vk-yyyyyyyy"
  skill_reload_mode: "poll"
  skill_reload_interval_seconds: 30

evolve:
  interval_seconds: 600
  publish_mode: "validated"
  human_review_enabled: true
  human_review_timeout_seconds: 86400
  evidence_max_entries: 400
  dataset_test_cases: 2
  dataset_min_requirements: 12
  validation_max_rejections: 1

dreamcycle:
  enabled: true
  auto_start: false
  active_start_hour: 0
  active_end_hour: 6
  rounds_per_window: 3
  enabled_jobs:
    - team_overview
    - deduplication
    - cleanup
    - onboarding_check
    - consolidate

validation:
  enabled: true
  mode: "true_replay"
  max_concurrency: 1
  required_results: 3
  required_approvals: 2

langfuse:
  enabled: false
  host: "https://cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_environment: "production"
  tracing_sample_rate: 0.1
```

## Configuration Hot Reload

Most configuration items require service restart after modification. Following configuration items support dynamic modification via Web console without restart:

- LLM model parameters (`llm.*`)
- Evolution pipeline parameters (`evolve.*`)
- Validation parameters (`validation.*`)
- DreamCycle parameters (`dreamcycle.*`)
- Langfuse parameters (`langfuse.*`)
- Skill Miner parameters (`mining.*`)
- Prompt overrides (via Prompt Studio)

Configurations modified via CLI `teamEvolver config set` auto-load at next evolution round start.
