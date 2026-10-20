# Configuration Reference

TeamEvolver configuration has two layers:

| Layer | Source | How to change | Takes effect |
| --- | --- | --- | --- |
| Bootstrap configuration | Environment variables + optional read-only YAML | Edit and restart the process | Per process |
| Online configuration | PostgreSQL: `teamevolver_config` (global), `tenant_configs` (5 modules per tenant), `tenants.config` (tenant overrides) | Console or API, with revision optimistic locking | Newly accepted tasks pin the revision current at acceptance |

Bootstrap configuration is read-only; the CLI has no write command. Write endpoints for online configuration are listed on each API page.

## Bootstrap Configuration

### Load Order

`teamEvolver/bootstrap.py:load_bootstrap`:

1. The YAML named by `TEAMEVOLVER_CONFIG_FILE` (optional). Top-level keys must be flat field names of `teamEvolver/config.py:TeamEvolverConfig`; unknown keys or the old sectioned / dotted-path layout make startup fail.
2. The environment variables below override their fields.
3. When `TEAMEVOLVER_PG_DSN` is not set, the DSN is assembled from `TEAMEVOLVER_PG_HOST` and related variables.
4. When a DSN is present, `storage_pg_enabled` becomes true automatically.

Show the effective result (credentials redacted):

```bash
teamEvolver config show
```

### Environment Variables

| Variable | Field | Description |
| --- | --- | --- |
| `TEAMEVOLVER_PG_DSN` | `storage_pg_dsn` | PostgreSQL DSN, required |
| `TEAMEVOLVER_PG_HOST`, `TEAMEVOLVER_PG_PORT`, `TEAMEVOLVER_PG_DATABASE`, `TEAMEVOLVER_PG_USERNAME`, `TEAMEVOLVER_PG_PASSWORD` | — | Used to assemble the DSN when it is not set; defaults: port 5432, database and user `teamevolver` |
| `TEAMEVOLVER_PG_SCHEMA` | `storage_pg_schema` | Default `teamevolver` |
| `TEAMEVOLVER_OV_ENDPOINT` | `sharing_viking_endpoint` | OpenViking address |
| `TEAMEVOLVER_OV_API_KEY` | `sharing_viking_api_key` | OpenViking Root Key |
| `TEAMEVOLVER_LLM_BASE_URL` | `llm_api_base` | OpenAI-compatible endpoint |
| `TEAMEVOLVER_LLM_API_KEY` | `llm_api_key` | Model key |
| `TEAMEVOLVER_LLM_MODEL` | `llm_model_id` | Model ID |
| `TEAMEVOLVER_LLM_CONCURRENCY` | `llm_max_concurrency` | Per-process model concurrency, default 8 |
| `TEAMEVOLVER_LLM_QUEUE_CAPACITY` | `llm_queue_capacity` | Model call queue limit, default 64 |
| `TEAMEVOLVER_TENANT_TOKEN` | `tenant_machine_token` | Tenant Key of the `default` tenant; must start with `tevt_` |
| `TEAMEVOLVER_SELF_REGISTRATION_ENABLED` | `self_registration_enabled` | Whether anonymous registration is allowed, default true |
| `TEAMEVOLVER_API_HOST` / `TEAMEVOLVER_API_PORT` | `proxy_host` / `proxy_port` | API listen address, default `0.0.0.0:52010` |

Variables read directly by code without a configuration field:

| Variable | Description |
| --- | --- |
| `TEAMEVOLVER_CONFIG_FILE` | Bootstrap YAML path |
| `TEAMEVOLVER_ROLE` | `api`, `worker` or `all`; used by `teamEvolver start` when `--role` is not passed, default `api` |
| `TEAMEVOLVER_ENV` | Environment label; the `all` role may only run under `local` (the default) |
| `TEAMEVOLVER_ROOT_API_KEY` | Service Root Key, see [API Overview](../api/01-overview.md#service-root-key) |
| `TEAMEVOLVER_SECRET_MASTER_KEY` | Master key for Secret Ref encryption; falls back to the Root Key when unset |
| `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` | Session upload body limit, default 32 MiB |
| `TEAMEVOLVER_LOG_*` | Logging parameters, see [Service Logging Status](../api/15-logging-status.md#logging-parameters) |
| `TEAMEVOLVER_MULTI_REPLICA`, `TEAMEVOLVER_INSTANCE_ID` | Separate log directories per instance in multi-replica deployments |
| `TEAMEVOLVER_CUSTOMER_MODE` | When `1`, `/api/auth/status` returns `customer_mode=true` and the console renders customer mode |
| `TEAMEVOLVER_CONFIG_DIR` | YAML directory in the container (default `/app/deploy/config`), also the default directory of the local identity store |
| `TEAMEVOLVER_AGENT_<PROFILE>_REPLAY_API_KEY` | Authentication key for the Replay transport |
| `OPENVIKING_CLI_BIN` | OpenViking CLI path, used by the Workspace CLI |
| `LANGFUSE_PULL_HOST`, `LANGFUSE_PULL_PUBLIC_KEY`, `LANGFUSE_PULL_SECRET_KEY` | Read by the Langfuse data source Adapter |
| `APP_PORT`, `TEAMEVOLVER_PORT` | Passed as `--port` by the container entrypoint `container_start.sh` |

### Common Fields

The YAML example is `config/config.yaml` in the repository. Set PostgreSQL TLS through the YAML field `storage_pg_ssl`.

| Field | Default | Description |
| --- | --- | --- |
| `team_display_name` | `Team` | Initial console display name (the online value is changed via `/api/team-settings`) |
| `llm_api_base`, `llm_model_id`, `llm_max_tokens`, `llm_temperature` | `https://api.openai.com/v1`, `gpt-4o-mini`, 16384, 0.4 | Model defaults; tenants can override via `/api/model-settings` |
| `sharing_viking_deployment` | `cloud` | `cloud` or `local` (`local` shows the OpenViking Studio link in the console) |
| `sharing_viking_account` / `sharing_viking_user` | `default` / `team` | Account and team User of the `default` tenant |
| `sharing_viking_root_prefix` | `team-skill-evolver` | Directory prefix for team Skills and personal Skills |
| `storage_pg_schema` | `teamevolver` | Schema name |
| `storage_pg_pool_min` / `storage_pg_pool_max` | 2 / 20 | Business connection pool |
| `storage_pg_control_pool_max` | 5 | Control-plane connection pool |
| `storage_pg_command_timeout_seconds` | 30 | Per-statement timeout |
| `storage_pg_ssl` | `prefer` | PostgreSQL TLS mode |
| `skills_runtime_cache_dir` | `~/.teamEvolver/runtime/skills` | Local cache of team Skills; can be deleted and rebuilt at any time |
| `self_registration_enabled` | true | Anonymous registration switch |
| `langfuse_tracing_*` | Off | Outbound tracing credentials; switch, sampling and other online values are changed via `/api/langfuse-tracing-config` |
| `logging` | See logging parameters | Logging settings object |

## Online Configuration

### Global Configuration

`teamevolver_config` stores global settings, changed by platform administrators:

| Key | Endpoint |
| --- | --- |
| `sharing` (OpenViking address, Account, team User, Secret Ref of the key) | `PUT /api/sharing-config` |
| `display_name` | `PUT /api/team-settings` |
| `tracing` | `PUT /api/langfuse-tracing-config` |

Values in the global `sharing` key override the bootstrap `sharing_viking_*` fields.

### Tenant Module Configuration

Each tenant has 5 module rows in `tenant_configs`; default rows are written when the tenant is created or when `teamEvolver schema apply` runs. When a module row is missing the related endpoints return 503 and do not fall back to YAML.

| Module | Content | Write endpoint |
| --- | --- | --- |
| `session` | `datasource_schedule` and related | `PUT /api/datasource/schedule` |
| `skills` | Bundle limits, evolution parameters, `model`, `prompt_overrides`, `stage_settings`, `experience_sync` | `/api/skill-evolution/settings`, `/api/skill-evolution/prompts/*`, `/api/model-settings`; the experience sync target has no dedicated endpoint |
| `replay` | `concurrency` (default 2), `timeout_seconds` (600), `max_interactions` (4) | No dedicated endpoint |
| `memory` | `aggregation` object | `POST /api/aggregation/settings` |
| `miner` | Knowledge base mining roots and Skill | No dedicated endpoint |

For modules without a dedicated endpoint, operators update the `config` of the corresponding `tenant_configs` row and increment `revision`.

### Tenant Overrides

`tenants.config` stores a tenant's overrides of bootstrap fields (flat field names). It is changed via `PUT /api/tenants/{tenant_id}/config` and merged with the bootstrap configuration per request. `sharing_viking_account` is written automatically when the tenant is created. Writable fields are listed in [Console Identity and Tenant Permissions](../api/20-console-rbac.md#tenants-and-members).

### Credentials

Credentials in online configuration (model key, OpenViking key, Langfuse secret, Git / marketplace token) are stored as Secret Refs (`sref_<32 hex>`); the plaintext is encrypted with `TEAMEVOLVER_SECRET_MASTER_KEY` and kept in `secret_refs`. Endpoints only return whether a value is configured. After the master key changes, old Secret Refs can no longer be decrypted and the credentials must be entered again.

## Console Self-Registration

`self_registration_enabled` (`TEAMEVOLVER_SELF_REGISTRATION_ENABLED`) is bootstrap configuration and takes effect after restart. When it is off, only the Root Key can call `POST /api/auth/register`; see [Self-Registration](../api/20-console-rbac.md#self-registration).
