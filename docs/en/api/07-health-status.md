# Health, Status and Jobs API

Code entry points:

- Health and status: `teamEvolver/observability/api/__init__.py:create_router`
- Worker health port: `teamEvolver/runtime/http_runtime.py:build_health_app`
- Unified job endpoints: `teamEvolver/operations/api/__init__.py:create_router`
- Task state machine: `teamEvolver/runtime/repository.py:WorkerTaskRepository`

## Health Checks

| Role | Port | Routes |
| --- | --- | --- |
| `api` | `TEAMEVOLVER_API_PORT`, default 52010 | `/healthz`, `/readyz`, `/status` and all business endpoints |
| `worker` | Default 52011 (changeable with `teamEvolver start --role worker --port`) | Only `/healthz`, `/readyz` |
| `all` | Same as `api` | Same as `api`, with the Worker running in the same process |

None of the three routes require authentication.

### GET /healthz

Liveness probe; returns 200 while the process is alive:

```json
{"ok": true, "role": "api"}
```

### GET /readyz

Readiness probe, reading `teamEvolver/runtime/lifecycle.py:RuntimeLifecycle.ready`. Returns 200 once lifecycle startup completes, and 503 when: startup has not finished, a graceful shutdown is in progress, the Worker loop reports PostgreSQL unavailable, or the Worker loop exited unexpectedly outside shutdown.

```json
{"ready": false, "role": "worker"}
```

During shutdown readiness is withdrawn first; in-flight tasks are awaited after the load balancer has removed the replica.

### GET /status

```json
{"role": "api", "ready": true}
```

`/status` also accepts the tenant key so Agent runtimes can probe the service.

## Unified Job Endpoints

Every `job_id` returned by a `202 Accepted` response is a `worker_tasks.task_id` and can be used with the endpoints below. The tenant comes from the request identity and RLS limits visibility to the tenant's own tasks; other tenants' tasks return 404. Console login or the Root Key is required.

### GET /api/jobs/{job_id}

```json
{
  "job_id": "3f0c...:session.normalize",
  "kind": "session.normalize",
  "status": "running",
  "attempt": 1,
  "cancel_requested": false,
  "upstream_task_id": "",
  "result_ref": "",
  "error_code": "",
  "created_at": "2026-10-14T08:00:00Z",
  "updated_at": "2026-10-14T08:00:03Z"
}
```

| Status | Meaning |
| --- | --- |
| `queued` | Waiting for a Worker to claim it |
| `running` | A Worker holds the lease and is executing |
| `retry_wait` | Failed and waiting for retry with backoff |
| `waiting_external` | An external task was submitted (such as OpenViking Compile) and is awaited; `upstream_task_id` is the upstream task ID |
| `reconciling` | Result unknown, reconciling with the upstream |
| `succeeded` | Succeeded; `result_ref` points to the result |
| `failed` | Terminal failure; `error_code` holds the reason |
| `cancelled` | Cancelled |

Business results are read from each module's `.../jobs/{job_id}/result`, for example `/api/datasource/jobs/{job_id}/result`, `/api/skill-evolution/jobs/{job_id}/result`.

### POST /api/jobs/{job_id}/cancel

- `queued`, `retry_wait`: becomes `cancelled` immediately.
- `running`, `waiting_external`, `reconciling`: sets `cancel_requested=true`; the Worker stops at a safe point and does not interrupt non-idempotent external writes.
- Already terminal: returns the current task view.

```json
{"job_id": "...", "status": "running", "cancel_requested": true}
```

### POST /api/jobs/{job_id}/retry

Only affects `failed` tasks, putting them back to `queued`; `attempt` increments on the next claim. Other states return `409 only a failed job can be retried (status=<status>)`.

## Worker Kinds

| kind | Module | Source |
| --- | --- | --- |
| `session.pull`, `session.normalize`, `session.analyze`, `session.archive` | Session intake | Push, data source pull, scheduled pull |
| `skill.evolve` | Skill evolution | Evolution trigger, Prompt tests, model / tracing tests, OpenViking directory initialization |
| `skill.validate` | Skill validation | Candidate evaluation and validation |
| `skill.transfer` | Skill transfer | Skill import and export |
| `experience.sync` | Successful experiences | Experience sync, full import, Pattern compile |
| `memory.aggregate`, `memory.compile`, `memory.replay` | Team Memory | Memory runs, maintenance, Memory Skill publishing, Memory Replay |
| `miner.track_compile`, `miner.rebuild_index` | Knowledge base mining | Mining jobs, uploads, edits, deletes |
| `replay.run`, `dataset.run` | Replay | Replay runs, Adapter tests, dataset runs and synthesis |

On each tenant refresh the Worker applies the tracing configuration and dispatches data source pulls, Skill evolution, Memory maintenance and successful experience sync. Task dispatch uses leases and dedupe keys, so multiple Worker replicas can run concurrently.

## Service Logging Status

`GET /api/logging/status` is described in [Service Logging Status](./15-logging-status.md).
