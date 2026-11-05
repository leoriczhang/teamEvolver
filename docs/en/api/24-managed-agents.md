# Managed Agents API

Entry point: `team_ma/api/__init__.py:create_router`. Console requests require login or a service Root Key and `X-Tenant-Id`. Tenant administrators can write; members read redacted status. Release snapshots accept the current tenant key + `user_id`; connection IDs are not credentials.

## Console Endpoints

| Method | Path | Request and response |
| --- | --- | --- |
| GET | `/api/ma/connections` | `after`, `limit`; returns `connections`, `next_after` |
| GET | `/api/ma/connections/{connection_id}` | Redacted configuration, revision, status, business/pull timestamps and Session cursors |
| POST | `/api/ma/connections` | Configuration and optional `connection_id`; queues discover, 202 + `job_id` / `status_url` |
| PUT | `/api/ma/connections/{connection_id}` | Patch + `expected_revision`; `enabled` pauses / resumes |
| POST | `/api/ma/connections/{connection_id}/discover` | `{}`, 202 |
| POST | `/api/ma/connections/{connection_id}/collect` | `{session_ids? , from_timestamp? , to_timestamp?}`, 202; timestamps require timezone |
| POST | `/api/ma/connections/{connection_id}/reconcile` | `{}`; queues pull, external effect reconciliation and owned resource cleanup, 202 |
| GET | `/api/ma/connections/{connection_id}/resources` | `kind=skill|agent`, `after`, `limit`; members, managed versions and tree hashes |
| PUT | `/api/ma/connections/{connection_id}/resources/{skill_name}/bindings` | `{expected_revision, agent_ids}`; new bindings use revision zero |
| GET | `/api/ma/connections/{connection_id}/releases` | `limit`; complete / partial releases, readback and error facts |

Creation and operations support `Idempotency-Key`; reuse with different requests returns 409. External HTTP, downloads, imports, analysis and synchronization run in Workers. Follow the returned unified Jobs URL.

## Connection Fields

| Field | Meaning |
| --- | --- |
| `provider` | `volcengine-managed-agents` |
| `display_name`, `entry_agent_id`, `environment_id`, `default_user_id` | Required, 1–200 characters |
| `ma_base_url` | Default `https://ark.cn-beijing.volces.com/api/v3`; HTTPS in production |
| `ma_api_key`, `tenant_token` | One-time plaintext input, encrypted storage, presence-only responses |
| `ma_secret_ref`, `tenant_token_ref` | Existing refs owned by this tenant; omitted credentials remain unchanged |
| `team_evolver_url` | Worker pull address, default `http://127.0.0.1:52010` |
| `vault_ids`, `replay_vault_ids` | Explicit Vault ID lists |
| `user_tag_prefix` | Default `te-user:` |
| `poll_interval_seconds`, `sync_interval_seconds` | Defaults 60 / 30; range 15–86400 |
| `replay_enabled`, `replay_environment_id` | Disabled by default; enabling requires a separate Replay Environment |
| `replay_isolation` | Explicit `{vaults?, mcp?, custom_tools?}` capabilities |
| `runtime_binding` | Optional `{skill_name, paths, working_directory, source?, pre_setup?, replace_setup_script?, bootstrap, ready_file, ready_probe_message?}`; `source` defaults to `embedded`; `skill_mount` verifies every file's SHA-256 in the pinned Skill mount before copying, keeping setup scripts small; prerequisites up to 32 KiB; replacing source setup defaults to false; optional read-only probe precedes the business task |

One MA origin + entry Agent can be managed by only one tenant. Revision conflicts and target ownership conflicts return 409. Download, isolation and frozen snapshot gaps fail explicitly; publishing gates are not weakened.

## Data-Plane Release Snapshot

`GET /sync/ma/{connection_id}?user_id=employee` uses `Authorization: Bearer <tenant-key>`. Responses contain `release_id`, `manifest_hash`, `connection_revision`, entry ID / fixed version, Environment ID / configuration hash, `agents`, `skills` and optional `runtime_tree_sha256`. Skill `tree_sha256` hashes the canonical file tree and differs from ZIP byte hashes.

Supports `ETag`, `If-None-Match` / 304 and `Cache-Control: private, no-cache`. Only fully applied releases are exposed. Partial synchronization returns the previous complete release; no complete release returns 503. Missing and cross-tenant connections return 404 without leaking resources.

## Replay Configuration

`GET /api/replay/settings` returns `revision` and `config`. `PUT /api/replay/settings` accepts `{expected_revision, config}` with `concurrency` 1–2, `timeout_seconds` 30–1800 and `max_interactions` 1–20. Tenant administrator permission is required. Entry point: `team_replay/api/settings.py:create_router`.

`GET /api/skill-evolution/settings` and PUT `evolve` support `bundle_text_extensions` to explicitly allow `.mjs/.js/.json` and other text files; static checks remain required. See [MA Integration](../agent-integrations/07-managed-agents.md) for workflows and examples.
