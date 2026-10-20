# Architecture

teamEvolver is a modular monolith: one release, one image, running in two roles, `api` and `worker`. The API only does authentication, authorization, validation, short accept transactions and queries; every long task runs as `202 Accepted + worker_tasks + Worker Handler`. PostgreSQL is the only authority for mutable business state; OpenViking is the authority for external knowledge artifacts and Compile Tasks.

## System Diagram

```text
                 Agent Runtime (v2: tenant key + user_id)      Browser console (Session Cookie)
                               │                                       │
                               ▼                                       ▼
        ┌──────────────── Load balancer (no session affinity) ─────────────────┐
        ▼                                                                  ▼
┌──────────────────────┐                                  ┌──────────────────────┐
│ teamEvolver api ×N   │  create_app(): auth → tenant ctx → │ teamEvolver api ×N   │
│ :52010               │  module Router → short txn → 202  │                      │
└─────────┬────────────┘                                  └─────────┬────────────┘
          │ domain record + payload + worker_tasks (one transaction)  │
          ▼                                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ PostgreSQL (RLS, isolated by tenant_id)                                     │
│ teamevolver_config · tenants · tenant_configs · secret_refs · adapters      │
│ ingestions · sessions · skills · skill_versions · skill_candidates          │
│ datasets · memory_runs · context_* · worker_tasks · resource_leases         │
│ tenant_usage · artifacts · storage_objects · audit_log · schema_versions    │
└─────────────────────────────────────────────────────────────────────────────┘
          ▲ claim with FOR UPDATE SKIP LOCKED, write back with fence check   ▲
┌─────────┴────────────┐                                  ┌─────────┴────────────┐
│ teamEvolver worker ×N│ Handlers: session.* skill.*       │ teamEvolver worker ×N│
│ health port :52011   │ replay.* memory.* miner.*        │                      │
└─────────┬────────────┘                                  └─────────┬────────────┘
          │ Compile / Content / Session / Search (Root Key + Account + User)
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ OpenViking: Skill · Memory · Wiki · Compile Task                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## Module Boundaries

```text
teamEvolver/            platform layer
├── app.py              the only FastAPI factory and business composition root (create_app, build_role_services)
├── bootstrap.py        read-only bootstrap configuration (environment + optional YAML)
├── config_runtime.py   global and tenant module configuration in PG (revision CAS)
├── database/           pools, transactions, RLS, schema_runner and versioned SQL
├── runtime/            role entrypoints, lifecycle, worker_tasks repository, Worker loop, leases
├── storage/            ObjectStore (PostgreSQL storage_objects; local implementation for development only)
├── openviking/         the only OpenViking HTTP/Compile client
├── security/           Secret encryption, AgentPrincipal
├── admin/ tenants/     console identity, RBAC, users, tenants
├── operations/         unified /api/jobs endpoints
├── observability/      health, readiness, logging status, Langfuse outbound tracing
├── platform_docs/      console entry, platform assets, OpenViking Workspace
├── context/            Agent Context gateway
└── cli/                teamEvolver command line
session_ingestion/      Session push, data source pull, mandatory analysis stages
team_skills/            Skill library, Candidate, Evolution, Experience, Transfer, Workbench
team_replay/            True Replay, Test Dataset, ReplayAdapterFactory
team_memory/            team Memory aggregation, DreamCycle maintenance, Memory Replay
team_miner/             mining module composition entrypoints
├── knowledge_miner/    knowledge base mining (OpenViking Compile tracking and Wiki indexing)
├── skill_miner/        Skill mining (sources, compilation, artifact review and Candidate submission)
└── shared/             document format conversion shared by both mining workflows
web-ui/                 React console sources, built into teamEvolver/web/dist/
```

Business modules provide `api/` (`create_router(services, **kwargs)`), `handlers`, `config.py`, `schemas.py` and `repository.py`. Exceptions: `team_replay/contracts.py` is the cross-module Replay contract; `team_miner/knowledge_miner` stores receipts, watermarks and indexes through the PostgreSQL ObjectStore without a dedicated `repository.py`. `team_miner/api` and `team_miner/handlers.py` compose both mining workflows; knowledge base mining retains its `miner` configuration and task identities.

## Dependency Rules

1. `teamEvolver/database`, `runtime`, `storage`, `openviking`, `llm.py` and `observability` do not import business modules.
2. `teamEvolver/app.py` is the only composition root; it mounts the 11 `api` packages in a fixed order and rejects duplicate `(method, path)` pairs.
3. Business modules do not import `teamEvolver.app` or another module's Handlers; cross-module access goes only through public Contracts, read-only Queries, persisted tasks or `services.extras` injected by the composition root.
4. A Repository only accesses its own domain's tables.
5. FastAPI appears only in `teamEvolver/app.py`, `teamEvolver/runtime/http_runtime.py` and each module's `api/`.
6. No hand-written production file exceeds 1000 lines.

These rules are checked statically by `tests/contracts/test_dependencies.py`, `tests/contracts/test_package_contracts.py` and `tests/contracts/test_application_contracts.py`.

## Request Path (API Role)

1. `teamEvolver/runtime/http_runtime.py` generates `request_id`/`trace_id` and calls the injected authorizer.
2. `teamEvolver/admin/authn.py:build_console_authorizer` resolves the identity: service Root Key, tenant key (`tevt_`) or console Session cookie, and binds the `TenantContext`.
3. The module Router checks permissions, parameters, idempotency keys and resource limits.
4. In one tenant transaction it writes the domain record, the immutable payload and `worker_tasks`, and returns `202`, `job_id` and `status_url` (`/api/jobs/{job_id}`).
5. Routers do not run LLM calls, Replay, Compile polling, batch file processing or external delivery directly.

## Worker Execution Model

`teamEvolver/runtime/worker.py:run_worker` polls and claims tasks across all active tenants:

- Claims use `FOR UPDATE SKIP LOCKED`; each claim increments `fence`, and result writes must match tenant, owner, attempt and fence, so late writes from an old attempt are rejected.
- The default lease is 60 seconds, renewed by heartbeat; after a failed renewal no new external side effects may start.
- Failures retry up to 3 times by default with 5/30/120 second backoff plus jitter.
- When the upstream result is unknown the task enters `reconciling` and is reconciled by idempotency key instead of being resubmitted blindly.
- `resource_leases` control tenant concurrency and exclusive resources such as Memory target trees and experience delivery; `tenant_usage` counts logical tasks.
- During short PG outages the Worker loop keeps running with 1, 2, 4… up to 30 second backoff and `/readyz` returns 503; it resumes automatically after recovery.

State machine:

```text
queued -> running -> succeeded
                  -> retry_wait -> queued
                  -> waiting_external -> reconciling -> succeeded/failed
                  -> failed
                  -> cancelled
```

The closed set of Handler kinds is `teamEvolver/runtime/tasks.py:TASK_KINDS`:

| Module | kind |
| --- | --- |
| session_ingestion | `session.pull`, `session.normalize`, `session.analyze`, `session.archive` |
| team_skills | `skill.evolve`, `skill.validate`, `skill.transfer`, `experience.sync` |
| team_replay | `replay.run`, `dataset.run` |
| team_memory | `memory.aggregate`, `memory.compile`, `memory.replay` |
| team_miner | `miner.track_compile`, `miner.rebuild_index` |

## Lifecycle

`teamEvolver/runtime/lifecycle.py:RuntimeLifecycle` fixes the shutdown order: withdraw readiness → stop claiming → wait for in-flight tasks (default 30 seconds) → close external clients and pools. The load balancer stops sending new requests once `/readyz` returns 503.

## Ports

| Role | Default port | Serves |
| --- | --- | --- |
| `api` | 52010 (`TEAMEVOLVER_API_PORT` or `--port`) | All HTTP APIs, console, `/healthz`, `/readyz`, `/status` |
| `worker` | 52011 (`--port`) | `/healthz`, `/readyz` |
| `all` | 52010 | Local development: API + Worker in one process |

All endpoints are listed in the [Endpoint Index](../api/00-endpoints.md).

## Code Entry Points

| Component | Path |
|------|---------|
| Role entrypoint | [entrypoint.py](../../../teamEvolver/runtime/entrypoint.py) |
| Composition root | [app.py](../../../teamEvolver/app.py) |
| CLI | [cli/](../../../teamEvolver/cli/) |
| Worker loop | [worker.py](../../../teamEvolver/runtime/worker.py) |
| Task repository | [repository.py](../../../teamEvolver/runtime/repository.py) |
| Schema versions | [schema/](../../../teamEvolver/database/schema/) |
| Configuration | [config_runtime.py](../../../teamEvolver/config_runtime.py), [bootstrap.py](../../../teamEvolver/bootstrap.py) |
| Frontend sources | [web-ui/src/](../../../web-ui/src/) |
