# Langfuse Observability Guide

teamEvolver deeply integrates Langfuse to provide LLM application observability. Langfuse integration includes two independent working modes, can be enabled separately:

1. **Inbound Session Pull**: Pull existing Agent session data from Langfuse into teamEvolver evolution pipeline
2. **Outbound Tracing**: Send teamEvolver's own evolution process LLM calls to Langfuse for tracing analysis

Related code at:
- `teamEvolver/observability/langfuse.py`: Tracing runtime and configuration management
- `teamEvolver/integrations/langfuse_client.py`: Langfuse v3 public API HTTP client
- `teamEvolver/integrations/langfuse_pull.py`: Session pull orchestration layer
- `teamEvolver/integrations/langfuse_convert.py`: Langfuse session format to teamEvolver format conversion

## Two Mode Comparison

| Dimension | Inbound Session Pull | Outbound Tracing |
|-----------|---------------------|-----------------|
| Direction | Langfuse → teamEvolver | teamEvolver → Langfuse |
| Config toggle | `langfuse.enabled` | `langfuse.tracing_enabled` |
| Purpose | Use external Agent sessions as evolution input | Debug and monitor evolution pipeline's own LLM calls |
| Data content | Complete session trajectories, tool calls, scores | System Prompts, User Messages, model outputs, Token usage across evolution stages |
| Execution | Scheduled/manual pull | Auto-captured per LLM call |
| Impact on evolution | Provides raw material for evolution | No side effects, fail-open design |

Both modes share same Langfuse credentials (`public_key`/`secret_key`) and `host` config, but can be toggled independently.

## Configuration Reference

All Langfuse-related configuration in config.yaml `langfuse` section:

### Basic Connection Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `enabled` | boolean | `false` | Whether to enable inbound session pull |
| `host` | string | `"https://cloud.langfuse.com"` | Langfuse service address. Self-hosted instances change to your address, e.g., `"http://127.0.0.1:3000"` |
| `public_key` | string | `""` | Langfuse Project Public Key, obtained from Langfuse project settings |
| `secret_key` | string | `""` | Langfuse Project Secret Key |

### Outbound Tracing Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `tracing_enabled` | boolean | `false` | Whether to enable outbound LLM call tracing |
| `tracing_environment` | string | `"local"` | Environment tag, distinguishes deployment environments in Langfuse UI. Recommended values: `production`, `staging`, `local`. Only letters, numbers, `-`, `_` allowed |
| `tracing_release` | string | `""` | Release tag, marks current deployment version (e.g., git commit hash, version number) |
| `tracing_sample_rate` | float | `1.0` | Sample rate, range 0.0-1.0. Production recommend `0.1` (10% sampling) to reduce cost |
| `tracing_capture_content` | boolean | `true` | Whether to capture complete LLM input/output content. Set to `false` to record only metadata and Token usage |
| `tracing_flush_at` | integer | `1` | Batch flush to Langfuse after accumulating traces |
| `tracing_flush_interval_seconds` | float | `1.0` | Periodic flush interval in seconds |
| `timeout_seconds` | integer | `30` | Langfuse API request timeout in seconds |

### Inbound Pull Default Filter Configuration

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `page_limit` | integer | `50` | Page size for paginated pulls |
| `max_sessions` | integer | `100` | Maximum sessions per pull |
| `default_environment` | list | `[]` | Default filter by environment; empty list means no filter |
| `default_user_id` | string | `""` | Default filter by userId |
| `default_tags` | list | `[]` | Default filter by tags (all specified tags must match) |
| `default_release` | string | `""` | Default filter by release |
| `default_version` | string | `""` | Default filter by version |
| `default_trace_name` | string | `""` | Default filter by trace name |

### Obtaining Credentials

1. Login to Langfuse (cloud.langfuse.com or your self-hosted instance)
2. Go to Settings → Projects
3. Create or select a Project
4. In API Keys section click "Create new API keys"
5. Copy Public Key and Secret Key

Note: Public Key starts with `pk-lf-`, Secret Key starts with `sk-lf-`.

## Configuration Examples

### Outbound Tracing Only (recommended for production monitoring)

```yaml
langfuse:
  enabled: false
  host: "https://cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_environment: "production"
  tracing_release: "v1.2.3"
  tracing_sample_rate: 0.1
  tracing_capture_content: true
```

### Both Inbound Pull and Outbound Tracing Enabled

```yaml
langfuse:
  enabled: true
  host: "https://cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_environment: "production"
  tracing_sample_rate: 0.5
  default_environment:
    - "production"
  default_tags:
    - "agent"
    - "coding"
  max_sessions: 50
```

### Self-Hosted Langfuse

```yaml
langfuse:
  enabled: true
  host: "http://langfuse.internal.example.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_environment: "local"
```

Also configurable via environment variables (higher priority than config.yaml):

```bash
export LANGFUSE_HOST="https://cloud.langfuse.com"
export LANGFUSE_PUBLIC_KEY="pk-lf-xxxxxxxx"
export LANGFUSE_SECRET_KEY="sk-lf-xxxxxxxx"
export LANGFUSE_TRACING_ENABLED="true"
export LANGFUSE_TRACING_ENVIRONMENT="production"
export LANGFUSE_SAMPLE_RATE="0.1"
```

## Outbound Tracing: What Gets Traced

When outbound tracing enabled, each LLM call in evolution pipeline automatically captured and sent to Langfuse. Tracing uses fail-open design: if Langfuse unavailable, evolution pipeline continues normally, only warning logged.

### Traced LLM Calls

Following stages' LLM calls in evolution pipeline are traced:

| Stage | Trace Name | Tags |
|-------|-----------|------|
| Value Classification (Session Filter) | `teamEvolver.evolve.session_filter` | `["teamEvolver", "evolve", "session_filter"]` |
| Summarize | `teamEvolver.evolve.summarize` | `["teamEvolver", "evolve", "summarize"]` |
| Judge (Session scoring) | `teamEvolver.evolve.judge` | `["teamEvolver", "evolve", "judge"]` |
| Skill Improvement (Evolve Skill) | `teamEvolver.evolve.evolve_skill` | `["teamEvolver", "evolve", "evolve_skill"]` |
| New Skill Creation (Create Skill) | `teamEvolver.evolve.create_skill` | `["teamEvolver", "evolve", "create_skill"]` |
| Conflict Merge (Merge) | `teamEvolver.evolve.merge` | `["teamEvolver", "evolve", "merge"]` |
| Dataset Synthesis | `teamEvolver.evolve.dataset_synthesis` | `["teamEvolver", "evolve", "dataset_synthesis"]` |
| Checklist Judge (Replay Checklist) | `teamEvolver.evolve.replay_checklist` | `["teamEvolver", "evolve", "replay_checklist"]` |
| Prompt Studio Test | `teamEvolver.evolve.prompt_test.<stage_id>` | `["teamEvolver", "evolve", "prompt-studio", <stage_id>]` |

DreamCycle Job LLM calls also traced when enabled.

### Data Contained in Each Trace

For each traced LLM call, following information recorded:

- **Input**: Complete System Prompt and User Message (when `tracing_capture_content: true`)
- **Output**: Complete text returned by model
- **Model**: Model name used
- **Model Parameters**: temperature, max_tokens, etc.
- **Usage Details**: prompt tokens, completion tokens, total tokens
- **Metadata**: Structured metadata like component name, operation name, stage ID, source session ID
- **Session ID**: Associated teamEvolver session ID (for grouping by session in Langfuse)
- **Tags**: Automatically adds `teamEvolver` tag and stage-specific tags
- **Environment**: Configured by `tracing_environment`
- **Release**: Configured by `tracing_release`

### Trace Hierarchy

Tracing uses nested Span structure:

- Each evolution round corresponds to one Trace
- Each LLM stage corresponds to one Span under Trace
- Sub-calls within same stage correspond to nested Spans

This clearly shows call relationships and timing across stages in one complete evolution cycle in Langfuse UI.

## Inbound Session Pull: How It Works

Inbound session pull mode pulls external Agent session data from Langfuse, converts format, then feeds into teamEvolver session queue triggering subsequent value classification and skill evolution.

### Supported Filter Dimensions

Pull supports rich filter conditions; default values can be set in config file, can also be temporarily overridden via CLI parameters per pull:

- **Time range**: `from_timestamp` / `to_timestamp` (ISO 8601 format)
- **Environment**: `environment` (can specify multiple, matches any)
- **User ID**: `user_id`
- **Tags**: `tags` (all must match)
- **Version**: `release` / `version`
- **Trace Name**: `trace_name`
- **Specific Session**: `session_id` (pull single session)
- **Metadata**: `metadata` (filter by metadata key-value pairs)

### Pull Flow

1. Query matching Trace list per filter conditions (via `/api/public/traces` endpoint)
2. Parse unique Session ID set from Trace results
3. Pull Session details one by one (via `/api/public/sessions/{id}` endpoint), including all Traces and Observations
4. Call `langfuse_convert.py` to convert Langfuse format to teamEvolver session format
5. Feed into session queue, undergo Session Filter value classification
6. High-value sessions enter evolution pipeline

### Authentication

Langfuse API uses HTTP Basic Auth:
- Username: Public Key
- Password: Secret Key

`langfuse_client.py` uses `httpx` to directly call public REST API, does not depend on Langfuse Python SDK for data pull.

## CLI Commands

teamEvolver provides `teamEvolver langfuse` command group for managing Langfuse integration:

### Check Connection Status

```bash
teamEvolver langfuse status
```

Output includes:

- host address
- session_pull_enabled / tracing_enabled status
- tracing_environment
- public_key/secret_key presence
- max_sessions config
- Default filter conditions
- Langfuse SDK availability
- Whether can successfully connect to Langfuse service
- Total session count (if accessible)

### Preview Matching Sessions

```bash
# List sessions matching default filters
teamEvolver langfuse list

# List sessions for specified environment
teamEvolver langfuse list --environment production --environment staging

# List sessions for specified user
teamEvolver langfuse list --user-id user-123

# List sessions with specified tags
teamEvolver langfuse list --tag agent --tag coding

# Filter by time range
teamEvolver langfuse list --from 2026-01-01T00:00:00Z --to 2026-01-31T23:59:59Z

# Pull single session
teamEvolver langfuse list --session-id abc-123-def

# Limit return count
teamEvolver langfuse list --max-sessions 20
```

`list` command only queries and displays; does not perform ingestion.

### Pull Sessions into Evolution Pipeline

```bash
# Pull using default config
teamEvolver langfuse pull

# Pull with filter conditions
teamEvolver langfuse pull --environment production --max-sessions 50

# Pull and specify user alias
teamEvolver langfuse pull --user-alias "langfuse-import"

# Force reprocess previously processed sessions
teamEvolver langfuse pull --force

# Pull but don't trigger evolution (enqueue only)
teamEvolver langfuse pull --defer-trigger

# Local processing (no dependency on running teamEvolver service)
teamEvolver langfuse pull --in-process
```

After pull completes, statistics displayed: queued, skipped (low-value), duplicate (skipped duplicate), empty (empty session skipped), error (processing error).

## Viewing Traces in Langfuse UI

### Viewing teamEvolver Outbound Traces

1. Login to Langfuse
2. Go to Tracing page
3. In left filter bar:
   - Select Environment (e.g., `production`)
   - Add Tag filter: `teamEvolver`
4. Can see all Traces emitted by teamEvolver

### Filtering by Stage

To view calls for specific stage, add corresponding Tag filter, e.g., `summarize`, `judge`, `evolve_skill`, etc.

### Viewing Token Usage and Cost

In Langfuse Metrics page can view by Environment, Tags, Model dimensions:
- Total Token usage trends
- Per-stage latency distribution
- Cost estimation (requires configuring model prices in Langfuse)

### Session Replay

Click individual Trace to see complete:
- Input/output content
- Nested call relationships
- Per-Span latency
- Token usage details
- Metadata

## Langfuse vs teamEvolver Console

| Feature | teamEvolver Console | Langfuse UI |
|---------|-------------------|-------------|
| Evolution status monitoring | Supported | Not supported |
| Skill management | Supported | Not supported |
| Candidate review | Supported | Not supported |
| Prompt editing/testing | Supported | Not supported |
| LLM call full-chain tracing | Basic logging | Full support |
| Token usage statistics | Basic | Detailed, multi-dimensional aggregation |
| Cost analysis | Not supported | Supported (with price config) |
| Trace history search | Limited | Powerful filtering and search |
| External Agent session browsing | Not supported | Natively supported |
| Session scoring and annotation | Not supported | Natively supported |

Recommended to use both: teamEvolver console for operations and management, Langfuse for deep debugging and LLM-call-level issue troubleshooting.

## Troubleshooting

### Traces not appearing in Langfuse

1. Check `tracing_enabled` is `true`
2. Check `public_key` and `secret_key` are correct
3. Check `host` is accessible from server (self-hosted instances verify network connectivity)
4. Check teamEvolver logs for `[Langfuse] tracing unavailable` warnings
5. Run `teamEvolver langfuse status` to verify `reachable: True`

### Inbound pull not finding sessions

1. Confirm sessions recorded in environment matching `default_environment`
2. Check Tags on Traces include your filtered tags
3. First run `teamEvolver langfuse list` to confirm match count
4. Confirm sessions created after `from_timestamp`
5. Langfuse Sessions list endpoint natively supports environment + time filtering only; other dimensions matched indirectly via /traces endpoint, ensure your Traces set correct userId/tags/release attributes

### Sample Rate Recommendations

- **Development**: `tracing_sample_rate: 1.0` (full sampling for debugging)
- **Staging**: `tracing_sample_rate: 1.0` (full sampling to verify integration)
- **Production**: `tracing_sample_rate: 0.05-0.2` (5%-20% sampling, balancing cost and observability)
- **Troubleshooting issues**: Temporarily set to `1.0`, revert after resolution

### Sensitive Data

If evolution pipeline processes sensitive data:

1. Set `tracing_capture_content: false` to record only metadata, Token usage and latency
2. Or run on self-hosted Langfuse instance where data doesn't leave your network
3. Note: Inbound pull pulls complete session content from Langfuse; ensure Langfuse's own access control properly configured
