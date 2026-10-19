# Storage and Directory Layout

teamEvolver data falls into four categories with fixed ownership:

| Store | Holds | Does not hold |
| --- | --- | --- |
| PostgreSQL (default schema `teamevolver`) | Global configuration, tenants and members, Secret Refs, Adapters, Sessions, Skill version chains, Candidates, Evidence, Datasets, Replay, Memory runs and checkpoints, Context records, Worker tasks, leases, usage, audit | External knowledge artifacts |
| PostgreSQL `storage_objects` (ObjectStore) | Immutable payloads, stage results, reports, Team Miner watermarks and receipts, and other objects not suited to column queries | Domain master data |
| OpenViking | Personal and team Memory, personal Skills, team Resources, Wiki, Compile Tasks | TeamEvolver mutable business state |
| Local directories | Temporary files, logs, PIDs, rebuildable Skill runtime cache | Any unique copy |

Replicas coordinate only through PostgreSQL. In production, PG or OpenViking failures never fall back to local directories: the API returns an error, the Worker backs off and retries, and `/readyz` returns 503.

## PostgreSQL

Every business table has a `tenant_id` column with `FORCE ROW LEVEL SECURITY` and the policy `tenant_id = current_setting('app.tenant_id')`. The tenant transaction in `teamEvolver/database/transaction.py` sets that variable inside the transaction; the runtime role must be NOSUPERUSER and NOBYPASSRLS, otherwise RLS has no effect.

| Version | Content |
| --- | --- |
| `001_init.sql` | `tenants`, `teamevolver_config`, `tenant_configs`, `secret_refs`, `adapters`/`adapter_versions`/`adapter_bindings`, `worker_tasks`, `resource_leases`, `tenant_usage`, `artifacts`, `audit_log` |
| `002_session_ingestion.sql` | `ingestions`, `session_stage_results`, `sessions` |
| `003_skills.sql` | `skills`, `skill_versions`, `skill_blobs`, `skill_version_files`, `skill_mutations`, `skill_candidates*` |
| `004_skill_evolution.sql` | `skill_commands`, `skill_evolution_branches`, `skill_evolution_sessions`, `skill_evidence` |
| `006_experiences.sql` | `skill_experiences`, `experience_*` |
| `007_skill_transfer.sql` | `skill_transfers` |
| `009_replay.sql` | `datasets`, `dataset_revisions`, `dataset_cases`, `dataset_materials` |
| `010_memory_context.sql` | `context_*`, `memory_runs`, `memory_stages`, `memory_checkpoints`, `memory_changes` |
| `011_storage.sql` | `storage_objects` |
| `012_objects.sql` | `objects` and tenant RLS |
| `013_console_identity.sql` | `console_users`, `tenant_memberships`, `console_*` |
| `014_tenant_module_defaults.sql` | Backfills the 5 default module configuration rows for existing tenants |

`schema_versions` is maintained by `teamEvolver/database/schema_runner.py`.
The open-source schema is a fresh-install baseline with gaps in version numbers; initialize it in an empty database.

### Configuration

- `teamevolver_config`: single-row global configuration (OpenViking deployment, team display name, tracing and so on) with `revision`.
- `tenant_configs`: primary key `(tenant_id, module)`, where `module` is `session`, `skills`, `replay`, `memory` or `miner`. All 5 rows are written when a tenant is created; a missing row at runtime returns 503 directly, with no YAML fallback.
- Writes use `expected_revision` CAS; long tasks pin configuration in `worker_tasks.config_revision` and the task payload.
- Credentials appear in configuration only as Secret Refs (`sref_...`); ciphertext lives in `secret_refs`, encrypted with `TEAMEVOLVER_SECRET_MASTER_KEY`.

## ObjectStore

`teamEvolver/storage/postgres.py:PostgresObjectStore` stores bytes and SHA-256 by `(tenant_id, key)` and verifies the hash on read. Common keys:

| Prefix | Content | Writer |
| --- | --- | --- |
| `ingest/<tenant>/<ingestion>.json` | Raw Session payload | Session intake |
| `session/<hash>/<stage>/<digest>.json` | Result of each analysis stage | Session Worker |
| Team Miner prefixes | Compile watermarks, receipts, link index | Team Miner |

The console "Platform assets" page shows these objects read-only as `platform://` URIs; see [Workspace and Platform Assets API](../api/23-workspace-assets.md).

## Tenant ↔ OpenViking Space Mapping

Each tenant is bound to one OpenViking Account (`sharing_viking_account`, defaulting to `tenant_id` when the tenant is created). TeamEvolver uses a single Root Key for all Accounts, and the space is chosen by request headers:

| Header | Value |
| --- | --- |
| `X-OpenViking-Account` | The tenant's bound Account |
| `X-OpenViking-User` | User ID for personal spaces; `sharing_viking_user` (default `team`) for team resources |
| `X-API-Key`, `Authorization: Bearer` | Root Key (`TEAMEVOLVER_OV_API_KEY` or the Secret Ref saved in the console) |
| `X-OpenViking-Role: admin` | Shared administrative operations only (such as publishing Skills to `viking://agent/skills`) |

A client-supplied Account cannot override the tenant configuration. The Root Key is used only on the server and is never returned to browsers or Agents.

### Scopes

Scopes are defined in `teamEvolver/platform_docs/workspace_core.py:_scope_map` and shared by the Context endpoints and "Personal and team assets":

| Scope | OpenViking root URI | Writable by members |
|--------|-------------------|--------------|
| `personal_memory` | `viking://user/<user>/memories` | Yes |
| `personal_skills` | `viking://resources/<root_prefix>/peers/<user>/skills` | Yes |
| `personal_resources` | `viking://user/<user>/resources` | Yes |
| `personal_workspace` | `viking://user` | Yes (own subtree only) |
| `team_memory` | `viking://resources/<shared_knowledge_prefix>` | No, tenant administrators only |
| `team_skills` | `viking://resources/<root_prefix>/skills` | No, tenant administrators only |
| `team_resources` | `viking://resources/team` | No, tenant administrators only |
| `team_workspace` | `viking://resources` | No, tenant administrators only |

| Variable | Source | Default |
|------|------|------|
| `root_prefix` | `sharing_viking_root_prefix` | `team-skill-evolver` (data contract constant) |
| `shared_knowledge_prefix` | `memory` module `aggregation.shared_knowledge_prefix` | `shared-knowledge` |
| `user` | `user_id` of the Agent request or the console user ID | — |

The authoritative team Skill versions are in PostgreSQL; the `team_skills` scope is the team Skill directory on the OpenViking side used for Context retrieval.

### Directory Skeleton

When a tenant is created or `POST /api/sharing-config/bootstrap-dirs` is called, `teamEvolver/admin/openviking_dirs.py:ensure_openviking_dirs` idempotently creates missing directories without overwriting existing content:

```text
viking://resources/team
viking://resources/<root_prefix>/{skills,peers}
viking://resources/<root_prefix>/peers/<user>/skills
viking://resources/<shared_knowledge_prefix>
viking://user/<user>/{memories,resources,skills}
```

### Other OpenViking Paths

| Path | Purpose |
| --- | --- |
| `viking://agent/skills/team-memory-okf`, `team-memory-maintenance` | Memory aggregation and maintenance Skills |
| `viking://user/<merge-user>/resources/teamEvolver/staging/<run>/` | Memory run work root (private) |
| `viking://resources/agent_knowledge_workspace/input/raw_knowledge_base` | Team Miner default knowledge source |
| `viking://resources/agent_knowledge_workspace/output/processed_knowledge` | Team Miner default Wiki output |
| `viking://resources/agent_knowledge_workspace/input/proven_experiences` | Default successful experience sync directory |
| `viking://agent/skills/llm-wiki` | Team Miner default Compile Skill |

## Local Directories

| Path | Content | Deletable |
| --- | --- | --- |
| `~/.teamEvolver/teamEvolver.log` (`TEAMEVOLVER_LOG_DIR`) | Service log | Yes |
| `~/.teamEvolver/runtime/skills` (`skills_runtime_cache_dir`) | Skill runtime cache | Yes, rebuilt on next read |
| `/tmp/te_runtime.log` | Default `--daemon` log | Yes |
| PID file | Process recorded by `teamEvolver start --daemon` | After the process stops |

## Console Views

| Page | Data source |
| --- | --- |
| Personal and team assets | OpenViking, by the scopes above |
| Platform assets | PostgreSQL ObjectStore, read-only |
| Run status | `GET /api/storage/status`: pools and backend (always `postgres`) |

## Code Entry Points

| Module | Path |
|------|------|
| Pools and transactions | [pool.py](../../../teamEvolver/database/pool.py), [transaction.py](../../../teamEvolver/database/transaction.py) |
| RLS | [rls.py](../../../teamEvolver/database/rls.py) |
| Schema | [schema/](../../../teamEvolver/database/schema/) |
| ObjectStore | [postgres.py](../../../teamEvolver/storage/postgres.py) |
| OpenViking client | [client.py](../../../teamEvolver/openviking/client.py) |
| Scopes | [workspace_core.py](../../../teamEvolver/platform_docs/workspace_core.py) |
| Directory skeleton | [openviking_dirs.py](../../../teamEvolver/admin/openviking_dirs.py) |

## Related Docs

- [Architecture](./01-architecture.md)
- [Memory](./04-memory.md)
- [Configuration Reference](../guides/01-configuration.md)
