# Production Deployment Guide

Production runs the same image in two roles, `api` and `worker`, connected to external PostgreSQL and OpenViking. Both roles scale horizontally and the load balancer does not need session affinity. Installation and image builds are covered in [Installation](../getting-started/03-installation.md).

## Prerequisites

| Component | Requirement |
| --- | --- |
| PostgreSQL | 14+; database and schema created in advance, the application role owns the schema, `NOSUPERUSER NOBYPASSRLS` |
| OpenViking | Reachable service address and Root Key; knowledge base mining needs the native Compile Runtime |
| Model | OpenAI-compatible endpoint and key |
| Image | `docker build` after `cicd_scripts/build.sh` produces a release |

## Roles

| Role | Responsibility | Port | Probes |
| --- | --- | --- | --- |
| `api` | Authentication, validation, short accept transactions, queries, console static assets | 52010 | `/healthz`, `/readyz` |
| `worker` | Claims `worker_tasks`, runs long tasks, scheduled passes (tracing configuration, data source pulls, experience sync) | 52011 (health only) | `/healthz`, `/readyz` |
| `all` | API and Worker in one process, only for `TEAMEVOLVER_ENV=local` | 52010 | Same as `api` |

Workers coordinate through PostgreSQL leases and fences: a claimed task holds a lease; after a crash or timeout another Worker takes it over, and a late result is rejected because its fence no longer matches.

## Environment File

`deploy/.env` (not committed, mode 0600):

```bash
TEAMEVOLVER_PG_DSN=postgresql://te_app:<password>@db:5432/teamevolver
TEAMEVOLVER_PG_SCHEMA=teamevolver
TEAMEVOLVER_ROOT_API_KEY=<random 32+ bytes>
TEAMEVOLVER_SECRET_MASTER_KEY=<random 32+ bytes>
TEAMEVOLVER_OV_ENDPOINT=http://openviking:1933
TEAMEVOLVER_OV_API_KEY=<OpenViking Root Key>
TEAMEVOLVER_LLM_BASE_URL=https://api.openai.com/v1
TEAMEVOLVER_LLM_API_KEY=<model key>
TEAMEVOLVER_LLM_MODEL=gpt-4o-mini
TEAMEVOLVER_TENANT_TOKEN=tevt_<random>
TEAMEVOLVER_ENV=production
```

Generate random values with `python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`. Once `TEAMEVOLVER_SECRET_MASTER_KEY` is in use it cannot change, otherwise saved Secret Refs can no longer be decrypted. All fields are listed in [Configuration Reference](./01-configuration.md).

An optional read-only YAML goes to `deploy/config/config.yaml`; the container entrypoint sets `TEAMEVOLVER_CONFIG_FILE` automatically.

## Initialize the Database

Run on any instance (or an operations host with the release installed):

```bash
teamEvolver schema apply --dry-run
teamEvolver schema apply
teamEvolver schema status
```

`schema apply` is idempotent. It runs the scripts under `teamEvolver/database/schema/` in version order, records them in `schema_versions`, and writes default module configuration for each tenant. Application processes do not create tables at startup.
First installs use an empty database and the new schema baseline.

## Docker Compose

`compose.yaml` at the repository root starts `teamevolver-api` and `teamevolver-worker` from the same image and reads `deploy/.env`:

```bash
TARGET_ARCH=x86_64 sh cicd_scripts/build.sh
docker compose build
docker compose up -d
docker compose ps
```

Both services' health checks call `/readyz` on their own port.

## Container Entrypoint

The image entrypoint `container_start.sh`:

1. Loads `/app/deploy/.env` when it exists.
2. Uses `config.yaml` under `TEAMEVOLVER_CONFIG_DIR` (default `/app/deploy/config`) when present and `TEAMEVOLVER_CONFIG_FILE` is not set.
3. Defaults `TEAMEVOLVER_API_HOST` to `0.0.0.0`.
4. Requires `TEAMEVOLVER_ROLE` to be `api` or `worker`, otherwise exits with 64.
5. Passes `APP_PORT` or `TEAMEVOLVER_PORT` as `--port` when set.

On Kubernetes, deploy API and Worker as two Deployments with the same image and Secret; only `TEAMEVOLVER_ROLE` and the port differ. Point `readinessProbe` and `livenessProbe` at `/readyz` and `/healthz` respectively.

## Single-Host Processes

```bash
teamEvolver start --role api --daemon --log-file /var/log/teamevolver/api.log
teamEvolver status
teamEvolver stop
```

`--daemon` starts one background process and writes a PID file; one host can run only one daemon. To run API and Worker together, start two foreground processes with systemd or another process manager:

```ini
[Service]
EnvironmentFile=/etc/teamevolver/env
ExecStart=/opt/teamEvolver-venv/bin/teamEvolver start --role worker
Restart=on-failure
KillSignal=SIGTERM
TimeoutStopSec=60
```

## Reverse Proxy

Nginx or a cloud load balancer can proxy requests to the API:

- Forward all paths to port 52010 of the API instances; no session affinity is needed.
- `POST /ingest_session` bodies can reach 32 MiB and Replay and Skill writes 96 MiB; size `client_max_body_size` accordingly.
- Preserve the `X-Request-ID` and `X-Trace-ID` headers.
- The console cookie name includes a hash of host, port and root_path; keep the `Host` header consistent through the proxy.
- Expose the Worker port 52011 only to probes.

## Shutdown and Upgrade

After an instance receives SIGTERM:

1. Readiness is withdrawn and `/readyz` returns 503.
2. The Worker stops claiming new tasks.
3. In-flight tasks get up to 30 seconds to finish; unfinished tasks are taken over by other Workers after their lease expires.
4. The database pools are closed.

Upgrade steps:

1. Build the new release, install it offline in pre-production and run `teamEvolver schema apply --dry-run`.
2. Run `teamEvolver schema apply`. The schema only moves forward.
3. Replace API and Worker instances one at a time.

Application rollback can only go to a release compatible with the current schema. Destructive database rollbacks are not performed.

## Backup

PostgreSQL is the only authority for TeamEvolver mutable state; back it up the usual way (`pg_dump` or physical backup + WAL archiving). Knowledge artifacts in OpenViking follow OpenViking's backup procedure. Local directories (`skills_runtime_cache_dir`, logs) can be rebuilt and need no backup.

Backups contain `secret_refs` ciphertext; restores must use the same `TEAMEVOLVER_SECRET_MASTER_KEY`.

## Sizing

| Role | CPU | Memory | Notes |
| --- | --- | --- | --- |
| API | 1–2 cores | 1–2 GiB | Accept and query are short transactions |
| Worker | 2–4 cores | 2–4 GiB | Model call concurrency is controlled by `TEAMEVOLVER_LLM_CONCURRENCY`; ZIP / document conversion uses memory |

Reserve PostgreSQL connections as `(storage_pg_pool_max + storage_pg_control_pool_max) × instances`.

## Security

- Inject `TEAMEVOLVER_ROOT_API_KEY`, `TEAMEVOLVER_SECRET_MASTER_KEY`, `TEAMEVOLVER_OV_API_KEY` and model keys only through environment variables or Secrets, never YAML or the image.
- Give tenant keys only to Agent servers. Rotation is described in [Console Identity and Tenant Permissions](../api/20-console-rbac.md#tenants-and-members).
- The database application role must not be SUPERUSER or BYPASSRLS; tenant isolation relies on RLS.
- Expose only the API port externally, behind TLS termination.
