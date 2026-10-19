# Trace Observability Guide

Trace observability reports teamEvolver's own model calls to Langfuse, to investigate prompts, outputs, token usage and latency of each evolution stage. It is a service-level setting; all tenants report to the same Langfuse project. Pulling Agent sessions from Langfuse belongs to data source onboarding and uses separate credentials; see the [Data Source Onboarding Guide](./10-datasource-onboarding.md).

Code entry points: `teamEvolver/observability/langfuse.py` (runtime and settings), `teamEvolver/llm.py` (model call instrumentation), `teamEvolver/admin/api/settings.py` (configuration API).

## Configuration Sources

Settings are merged in the following order, higher priority overriding lower:

1. `LANGFUSE_*` environment variables
2. Online settings saved in the console "Trace Observability" page (`teamevolver_config.tracing`)
3. Bootstrap configuration fields `langfuse_tracing_*`

On each tenant refresh round (at least 10 seconds apart) the Worker re-reads the online settings and applies them; when they change, the Langfuse client is rebuilt without a process restart.

| Bootstrap field | Environment variable | Default | Description |
|--------------|----------|------|------|
| `langfuse_tracing_enabled` | `LANGFUSE_TRACING_ENABLED` | false | Whether to report |
| `langfuse_tracing_host` | `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` | empty | Langfuse address |
| `langfuse_tracing_public_key` | `LANGFUSE_PUBLIC_KEY` | empty | Project public key |
| `langfuse_tracing_secret_key` | `LANGFUSE_SECRET_KEY` | empty | Project secret key |
| `langfuse_tracing_environment` | `LANGFUSE_TRACING_ENVIRONMENT` | `local` | Environment tag; letters, digits, `-`, `_` only |
| `langfuse_tracing_release` | `LANGFUSE_RELEASE` | empty | Release tag |
| `langfuse_tracing_sample_rate` | `LANGFUSE_SAMPLE_RATE` | 1.0 | Sample rate 0–1 |
| `langfuse_tracing_capture_content` | `LANGFUSE_CAPTURE_CONTENT` | true | Record model input and output; when off, only metadata and usage are recorded |
| `langfuse_tracing_flush_at` | `LANGFUSE_FLUSH_AT` | 1 | Batch flush size |
| `langfuse_tracing_flush_interval_seconds` | `LANGFUSE_FLUSH_INTERVAL` | 1.0 | Flush interval (seconds) |
| `langfuse_tracing_timeout_seconds` | `LANGFUSE_TIMEOUT` | 30 | Request timeout (seconds) |

A client is created only when both the public and secret keys are present.

## Console and API

Platform administrators configure the switch, address, sample rate and content capture in "Review & Publish → Trace Observability", and can test the connection.

| Method | Path | Description |
|------|------|------|
| GET | `/api/langfuse-tracing-config` | Online settings, and whether the bootstrap configuration has public/secret keys |
| PUT | `/api/langfuse-tracing-config` | Save online settings; fields limited to `enabled`, `host`, `environment`, `release`, `sample_rate`, `capture_content`, `flush_at`, `flush_interval_seconds`, `public_key`, `secret_key` |
| POST | `/api/langfuse-tracing-config/test` | `{"host", "public_key", "secret_key"}` connection test; returns 202 and `job_id` |

A submitted `secret_key` is encrypted and stored as a Secret Ref; the API never echoes the plaintext. Encryption requires `TEAMEVOLVER_SECRET_MASTER_KEY` (falling back to `TEAMEVOLVER_ROOT_API_KEY`). The connection test is run by the Worker against `<host>/api/public/projects`; read the result with `GET /api/jobs/{job_id}`.

## Reported Content

Each model call produces one `generation` observation containing:

- Input messages and tool definitions (when `capture_content=true`)
- Model output and `finish_reason`
- Model name, `temperature`, `max_completion_tokens`
- Token usage
- Metadata: component, operation, retry count and so on
- Tags: `teamEvolver` and `llm` on every call, plus stage tags

Each retry attempt is recorded separately, with `metadata.attempt` as its sequence number. Reporting failures never affect the model call; the error is logged as `[Langfuse] tracing unavailable` and kept as `last_error` in status.

### Trace Names

| Stage | Trace name |
|------|------------|
| Session analysis | `team_skills.evolution.analyze_session` |
| Trace bad-case analysis | `team_skills.evolution.analyze_trace_badcase` |
| Skill improvement | `team-skill-evolver:evolve_skill:<skill>` |
| New Skill | `team-skill-evolver:create_skill` |
| Skill merge | `team-skill-evolver:merge_skill:<skill>` |
| Dataset synthesis | `team-skill-evolver:dataset_synthesis:<skill>` |
| Prompt test | `team_skills.evolution.prompt_test.<stage_id>` |
| Session split | `session_ingestion.split_session` |
| Experience pattern compile | `skill_experience_pattern.compile` |
| Calls without a name | `teamEvolver.llm.chat` |

Tags of Skill improvement, creation and merge include `team-skill-evolver`, `team_skills.evolution`, the operation name, `skill:<skill>` and up to 20 source Session IDs; metadata keeps up to 50 source Session IDs. When the `EVOBENCH_RUN_ID` environment variable is set, the run id is added as a tag and `team-skill-evolver:<run_id>:<operation>:<skill>` is used as the Langfuse Session ID.

## Viewing in Langfuse

1. Filter by Environment on the Tracing page.
2. Use the tag `team-skill-evolver` for Skill evolution calls and `skill:<name>` for one Skill.
3. Use source Session ID tags to find which Sessions an evolution consumed.
4. Aggregate token usage and latency by model and tag on the Metrics page.

## Troubleshooting

| Symptom | Check |
|------|------|
| No traces at all | Switch enabled; both keys configured; `LANGFUSE_TRACING_ENABLED` not overriding to false |
| Console changes have no effect | Worker running; `LANGFUSE_*` environment variables not overriding the same field |
| Connection test fails | Reach `<host>/api/public/projects` from the Worker host; check network and keys |
| Metadata only, no content | `capture_content` is false |

For sensitive data, turn off `capture_content` or use a self-hosted Langfuse. A sample rate of 0.05–0.2 is recommended in production; raise it to 1.0 temporarily while investigating.

## Related Documents

- [Configuration Reference](./01-configuration.md)
- [Service Logging](./15-service-logging.md)
- [Prompt Studio Guide](./08-prompt-studio.md)
