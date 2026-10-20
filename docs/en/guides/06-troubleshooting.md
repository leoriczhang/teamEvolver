# Troubleshooting

Work through "process → database → task → business module". Most problems can be located with `/readyz`, `GET /api/jobs/{job_id}` and the service log.

## Step 1: Check Processes and Logs

```bash
teamEvolver status
curl -s http://127.0.0.1:52010/readyz
curl -s http://127.0.0.1:52011/readyz
```

| Port | Role | Description |
|------|------|------|
| 52010 | `api` | Console and HTTP API; `/readyz` 503 means not ready or shutting down |
| 52011 | `worker` | Health checks only |

The log defaults to `~/.teamEvolver/teamEvolver.log`; containers can set the directory with `TEAMEVOLVER_LOG_DIR`. It is also written to standard error. In background mode without `--log-file`, process output goes to `/tmp/te_runtime.log`. Log fields and degradation behavior are in [Service Logging](./15-service-logging.md).

## Startup Failures

| Output | Cause and fix |
|------|------------|
| `runtime requires PostgreSQL; configure TEAMEVOLVER_PG_DSN` | No database connection; set `TEAMEVOLVER_PG_DSN` or the `TEAMEVOLVER_PG_HOST` family |
| `all runtime is restricted to local development` | `--role all` requires `TEAMEVOLVER_ENV=local`; production starts `api` and `worker` separately |
| `teamEvolver is already running (PID=...)` | Background mode found a live PID; run `teamEvolver stop` first |
| `teamevolver_config single row is missing; run schema apply` | Database not initialized; run `teamEvolver schema apply` |
| `Invalid logging option: ...` | Invalid logging configuration; see [Service Logging](./15-service-logging.md#configuration) |
| YAML field errors at load | The bootstrap configuration accepts only flat current field names; former section or dot-path forms are rejected; see the [Configuration Reference](./01-configuration.md) |

When `teamEvolver status` reports running but the process is gone, the PID file is stale; `teamEvolver stop` clears it.

## Database

- `/readyz` stays 503: check database address, port, URL-encoded password and network
- Tenants see each other's data or see nothing: make sure the application role is not SUPERUSER or BYPASSRLS and the schema was created by `teamEvolver schema apply`
- Connections exhausted: reserve `(storage_pg_pool_max + storage_pg_control_pool_max) × instances` connections

## Tasks Never Finish

Every long task is a `worker_tasks` row, and accepting APIs return a `job_id`:

```bash
curl -s -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" -H 'X-Tenant-Id: demo' \
  http://127.0.0.1:52010/api/jobs/<job_id>
```

| Status | Action |
|------|------|
| `queued` for a long time | Worker not running, or the tenant status is not `active` |
| `retry_wait` | Waiting for a backoff retry; `error_code` is the last failure |
| `waiting_external` | Waiting on upstream (such as OpenViking Compile); check the upstream task `upstream_task_id` |
| `reconciling` | Outcome unknown, reconciling with upstream; do not resubmit by hand |
| `failed` | Check `error_code`; after fixing, `POST /api/jobs/{job_id}/retry` |

When a Worker exits, the leases of its unfinished tasks expire and other Workers take them over. Status details are in [Health and Jobs API](../api/07-health-status.md).

## Agent Integration

| Symptom | Action |
|------|------|
| `401 TENANT_TOKEN_REQUIRED` | The Agent did not send a valid tenant Key; rotate it in "Members & Access" and update the Agent |
| Ingest returns 202 but the Session never shows up | Query the returned `job_id`; `session.analyze` needs a working model |
| `GET /sync/skills` always 304 | No new version; the ETag changes only after a publish |
| A Skill is not pulled | Make sure it is published and not deleted; pull returns Skills visible to the `user_id` |

Protocol details are in [Agent Protocol](../agent-integrations/06-protocol-v2.md).

## Evolution Produces No Candidate

1. Check `evolution_trigger_mode`. In manual mode, click "Run Evolution" in the console or call `POST /api/skill-evolution/trigger`; in automatic mode, also allow for the configured interval.
2. Check whether the Skill group meets `min_group_sessions` and `min_group_users` (both default to 2); see the [Prompt Studio Guide](./08-prompt-studio.md#settings).
3. Check whether Sessions were classified as chitchat: "Run Overview → Filter Audit".
4. Check the model: connection test on the "Model Settings" page.

## True Replay

| Symptom | Action |
|------|------|
| Validation result `inconclusive` | Efficiency metrics (turns, tool calls, tokens) are missing; check the fields returned by the Replay adapter |
| Both branches give identical results | Make sure the adapter uses a separate workspace per branch |
| Validation task fails | Check the `skill.validate` task `error_code` and Replay adapter connectivity |

The contract is in [Replay Branch API](../api/05-replay-branch.md).

## OpenViking

| Symptom | Action |
|------|------|
| Assets page or knowledge base mining shows "not configured" | Configure Endpoint, Account and Root Key in "Runtime Status → OpenViking Deployment" |
| Reads and writes return 503 | OpenViking unreachable; check Endpoint and network |
| Writing team resources returns 403 | Only tenant administrators can write team scopes |
| Built-in terminal unavailable | OpenViking CLI not installed on the server, or wrong `OPENVIKING_CLI_BIN` path |

## Console

| Symptom | Action |
|------|------|
| `503 console assets are not built` | Frontend not built; run `npm --prefix web-ui run build` |
| No menu after sign-in | The account has no tenant membership; ask a tenant administrator to add it |
| Signed out shortly after sign-in | Session Cookie expired or revoked; the reverse proxy must keep cookies and use one domain |
| Data unchanged after switching tenant | The page reloads fully; if data is still stale, clear the browser cache and retry |

## Trace Observability and Data Sources

- No trace data: see the [Trace Observability Guide](./04-observability.md#troubleshooting)
- Data source test or pull fails: see the [Data Source Onboarding Guide](./10-datasource-onboarding.md#common-errors)
- Successful experiences not synced: see the [Successful Experience Sync Guide](./16-successful-experience-sync.md#status-and-troubleshooting)

## Collecting Information

When reporting a problem, include:

- `teamEvolver config` output (redacted)
- The relevant `job_id` and the `GET /api/jobs/{job_id}` result
- The request `X-Request-ID` and the log for that time window
- `GET /api/logging/status` output
