# Managed Agents API

After discovery, collection and synchronization are scheduled independently.
`detail.operation_errors` records each operation's error, Job ID and `retryable`
flag. Success clears only that operation's error. Transient network and upstream
service failures resume at the configured interval even after task retries are
exhausted. Validation failures, authentication failures and version drift pause
automatic scheduling for the affected operation; trigger it manually after repair.
Synchronization failures do not stop Session collection. An unpublished candidate's
target binding is not a deleted Skill; unattached cloud resources retain their
identity. Cloud 404 responses never revive an old TE import.

Entry point: `team_ma/api/__init__.py:create_router`. Console requests require login or a service Root Key and `X-Tenant-Id`. Tenant administrators can write; members read redacted status. Release snapshots accept the current tenant key + `user_id`; connection IDs are not credentials.

## Console Endpoints

| Method | Path | Request and response |
| --- | --- | --- |
| GET | `/api/ma/connections` | `after`, `limit`; returns `connections`, `next_after` |
| GET | `/api/ma/connections/{connection_id}` | Redacted configuration, revision, status, business/pull timestamps and Session cursors |
| POST | `/api/ma/connections` | Configuration and optional `connection_id`; queues discover, 202 + `job_id` / `status_url` |
| PUT | `/api/ma/connections/{connection_id}` | Patch + `expected_revision`; `enabled` pauses / resumes; enabling Replay or changing its dependencies queues discover and returns 202 + Job |
| POST | `/api/ma/connections/{connection_id}/discover` | `{}`, 202 |
| POST | `/api/ma/connections/{connection_id}/collect` | `{session_ids? , from_timestamp? , to_timestamp?}`, 202; timestamps require timezone |
| POST | `/api/ma/connections/{connection_id}/reconcile` | `{}`; observes current cloud Skills, Agents and Environment, reconciles effects and cleans owned resources, 202 |
| GET | `/api/ma/connections/{connection_id}/resources` | `kind=skill|agent`, `after`, `limit`; members, managed versions and tree hashes |
| PUT | `/api/ma/connections/{connection_id}/resources/{skill_name}/bindings` | `{expected_revision, agent_ids}`; new bindings use revision zero |
| GET | `/api/ma/connections/{connection_id}/releases` | `limit`; complete / partial releases, readback and error facts |
| GET | `/api/ma/skill-jobs/{job_id}` | Actual `ma.skill` status, native write result and error |

Creation and operations support `Idempotency-Key`; reuse with different requests returns 409. Cloud writes, imports, analysis and cleanup run in Workers. Skill queries resolve native metadata and verified snapshots before short business transactions.

MA Skills use the existing [Skill management endpoints](./09-skills-admin.md) for current contents, native history, edits, creation, files, rollback and deletion. Responses carry `skill_backend: "managed-agents"` and native version numbers. Select a connection with the `connection_id` query parameter; same-name Skills across connections require explicit selection. Writes return `202`, `job_id`, `status_url` and `result_url`; publication requires upload and member-reference readback. Rollback creates a new native version. Unknown uploads reconcile first, and concurrent uploads pause publication. Discovery/reconcile do not pull or push TE library copies.

## Connection Fields

| Field | Meaning |
| --- | --- |
| `provider` | `volcengine-managed-agents` |
| `display_name`, `entry_agent_id`, `environment_id` | Required, 1–200 characters |
| `default_user_id` | Optional, at most 200 characters; an explicit default attribution. Empty defaults show an unknown submitter when tags/channel identity are absent |
| `ma_base_url` | Default `https://ark.cn-beijing.volces.com/api/v3`; HTTPS in production |
| `ma_api_key`, `tenant_token` | One-time plaintext input, encrypted storage, presence-only responses |
| `ma_secret_ref`, `tenant_token_ref` | Existing refs owned by this tenant; omitted credentials remain unchanged |
| `team_evolver_url` | Compatibility field; native MA Skill management no longer uses TE pull |
| `vault_ids`, `replay_vault_ids` | Business Vaults; omitted Replay Vaults are created by the Worker; existing separate IDs remain compatible |
| `user_tag_prefix` | Default `te-user:` |
| `poll_interval_seconds`, `sync_interval_seconds` | Defaults 60 / 30; range 15–86400 |
| `replay_enabled`, `replay_environment_id` | Disabled by default; omit Replay Environment ID to let the Worker find or create it |
| `replay_isolation` | Explicit `{vaults?, mcp?, custom_tools?}` capabilities |
| `runtime_binding` | Optional `{skill_name, paths, working_directory, source?, artifact?, packages?, pre_setup?, replace_setup_script?, bootstrap, ready_file, ready_probe_message?}`; `source` defaults to `embedded`; `skill_mount` verifies pinned Skill files; `openviking` downloads an independent tar.xz pinned by `artifact` (`base_url`, `uri`, `sha256`, `bytes`, `files[{path,sha256,bytes}]`), verifies and caches it before bootstrap; optional `packages.apt/pip` lists merge with source packages for MA provisioning before setup; prerequisites up to 32 KiB; replacing source setup defaults to false; optional read-only probe precedes the business task |

One MA origin + entry Agent can be managed by only one tenant. Revision conflicts and target ownership conflicts return 409. Download, isolation and frozen snapshot gaps fail explicitly; publishing gates are not weakened.

`detail.replay_resources` reports `status` (`preparing/ready/credentials_missing/unsupported/disabled`), `environment_id`, `vault_ids` and `missing_credentials`, without secrets. Automatic resources carry ownership for the tenant, connection and MA origin; the external effect ledger reconciles ambiguous outcomes before retrying POST. The Environment copies business dependencies and setup without the production TOS output location. An automatic Vault can inject this tenant's existing MA Key as an MA-host-limited `ARK_API_KEY`; other services' write-only secrets cannot be read or inferred. Missing credentials leave collection available and Replay unsupported. Existing separate resources remain usable; production Environment/Vault IDs are rejected. Replay acceptance freezes actual discovered IDs without changing the connection revision.

## Data-Plane Release Snapshot

`GET /sync/ma/{connection_id}?user_id=employee` uses `Authorization: Bearer <tenant-key>`. Responses contain `release_id`, `manifest_hash`, `connection_revision`, entry ID / fixed version, Environment ID / configuration hash, `agents`, `skills` and optional `runtime_tree_sha256`. `environment_alias` is the stable business alias used for lookup, while `environment_name` is the immutable actual MA name. Historical releases can derive the alias from `base_environment` without rewriting existing Environments. Skill `tree_sha256` hashes the canonical file tree and differs from ZIP byte hashes.

Supports `ETag`, `If-None-Match` / 304 and `Cache-Control: private, no-cache`. Only fully applied releases are exposed. Partial synchronization returns the previous complete release; no complete release returns 503. Missing and cross-tenant connections return 404 without leaking resources.

## Replay Configuration

`GET /api/replay/settings` returns `revision` and `config`. `PUT /api/replay/settings` accepts `{expected_revision, config}` with `concurrency` 1–2, `timeout_seconds` 30–1800 and `max_interactions` 1–20. Tenant administrator permission is required. Entry point: `team_replay/api/settings.py:create_router`.

`GET /api/skill-evolution/settings` and PUT `evolve` support `bundle_text_extensions` to explicitly allow `.mjs/.js/.json` and other text files; static checks remain required. See [MA Integration](../agent-integrations/07-managed-agents.md) for workflows and examples.
