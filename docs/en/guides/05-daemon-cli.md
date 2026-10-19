# Operations CLI

The `teamEvolver` command is defined in `teamEvolver/cli/__init__.py` and is available after installing the wheel or `pip install -e .`. Commands fall into two groups:

- **Local commands** read the bootstrap configuration or connect to PostgreSQL directly: `start`, `stop`, `status`, `config`, `schema`
- **API commands** call a running `api` role over HTTP: `datasource`, `skills`, `validation`

All commands read the bootstrap configuration from environment variables and the YAML pointed to by `TEAMEVOLVER_CONFIG_FILE`; fields are listed in the [Configuration Reference](./01-configuration.md).

## Process Management

### start

```bash
teamEvolver start [--role api|worker|all] [--port PORT] [-d|--daemon] [--log-file PATH]
```

| Option | Description |
|------|------|
| `--role` | Runtime role; defaults to `TEAMEVOLVER_ROLE`, then `api` |
| `--port` | HTTP port for `api` / `all` (default `proxy_port`, i.e. 52010); health port for `worker` (default 52011) |
| `-d, --daemon` | Start in the background; the PID is written to `~/.teamEvolver/teamEvolver.pid` |
| `--log-file` | Log file; in background mode without it, process output goes to `/tmp/te_runtime.log` |

Startup constraints:

- PostgreSQL must be configured (`TEAMEVOLVER_PG_DSN` or `TEAMEVOLVER_PG_*`), otherwise the command exits
- `all` is allowed only with `TEAMEVOLVER_ENV=local`; production runs `api` and `worker` separately
- The database schema is never created at startup; run `teamEvolver schema apply` on first deployment
- Background mode refuses to start while the recorded PID is alive; with `TEAMEVOLVER_MULTI_REPLICA=true` each process uses its own PID file

The `api` role serves the console and all HTTP APIs and runs no tasks; the `worker` role claims `worker_tasks` and exposes only `/healthz` and `/readyz`.

### stop and status

```bash
teamEvolver status
teamEvolver stop
```

`status` reads the PID file and reports whether the process is running. `stop` sends SIGTERM to the recorded PID; the process revokes readiness first, then waits for in-flight tasks before exiting.

## Configuration and Schema

### config

```bash
teamEvolver config
```

Prints the merged bootstrap configuration with secrets redacted. The command is read-only; tenant and module online configuration is changed through the console or API.

### schema

```bash
teamEvolver schema status
teamEvolver schema apply --dry-run
teamEvolver schema apply
```

`status` lists the versions and checksums in `teamEvolver/database/schema/`. `apply` runs pending SQL in version order and records them in `schema_versions`; it is idempotent. `--dry-run` only lists pending versions.

## API Commands

API commands target `http://127.0.0.1:<proxy_port>` by default; `--url` selects another address and `--tenant` selects the tenant.

### datasource

```bash
teamEvolver datasource --tenant demo status
teamEvolver datasource --tenant demo bind example-doris.py
teamEvolver datasource --tenant demo test
teamEvolver datasource --tenant demo list --from-timestamp ... --to-timestamp ... --max-sessions 20
teamEvolver datasource --tenant demo pull --from-timestamp ... --to-timestamp ...
```

Uses `TEAMEVOLVER_ROOT_API_KEY`. `list` and `pull` accept `--from-timestamp`, `--to-timestamp`, `--session-id` and `--user-id`. `--tenant` defaults to `default`. See the [Data Source Onboarding Guide](./10-datasource-onboarding.md).

### skills

```bash
teamEvolver skills --tenant demo list
teamEvolver skills --tenant demo pull --user-id alice
```

- `list` calls `GET /api/skills` with `TEAMEVOLVER_ROOT_API_KEY`
- `pull` calls `GET /sync/skills` with the tenant Key `TEAMEVOLVER_TENANT_TOKEN` and writes the visible Skills to the local cache `skills_runtime_cache_dir` (default `~/.teamEvolver/runtime/skills`)

The local cache can be deleted and rebuilt at any time; the authoritative versions always live in the server-side SkillHub.

### validation

```bash
teamEvolver validation --tenant demo status
```

Calls `GET /api/skill-candidates?scope=open` with `TEAMEVOLVER_ROOT_API_KEY` and lists Candidates waiting for validation or review.

## Exit Codes

Commands return 0 on success. Invalid arguments, missing required environment variables or non-2xx HTTP responses return non-zero with the reason on standard error; database driver errors never echo SQL content.

## Related Documents

- [Deployment Guide](./02-deployment.md)
- [Configuration Reference](./01-configuration.md)
- [Troubleshooting](./06-troubleshooting.md)
