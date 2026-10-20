# Service Logging

Service logs are managed by `teamEvolver/logging_runtime.py`. `api` and `worker` processes write the same rotating file format in foreground, background and container modes, and also write to standard error for platform collection. Logging is a service-level bootstrap setting; changes take effect after restart.

## Log Location

| Scenario | Path |
|------|------|
| Local default | `~/.teamEvolver/teamEvolver.log` |
| Example custom container log directory | `/app/deploy/logs/teamEvolver.log` |
| `--log-file PATH` | The given path; file output is forced on |
| Multiple replicas | `<directory>/<instance>/teamEvolver.log` when `TEAMEVOLVER_MULTI_REPLICA=true` |

In multi-replica mode `<instance>` is `TEAMEVOLVER_INSTANCE_ID`, or host name plus PID when unset. A second process on the same path cannot get the file lock and degrades to standard error only.

Platform administrators can see the current process's actual path, file state, queue depth and drop counts in "Runtime Status → Service Logs" or `GET /api/logging/status`. Behind a load balancer each request may hit a different instance.

## Configuration

```yaml
logging:
  level: INFO
  console_enabled: true
  file_enabled: true
  directory: /app/deploy/logs
  max_file_mb: 100
  retention_days: 14
  max_total_mb: 2048
```

| Field | Environment variable | Default |
|------|----------|------|
| `level` | `TEAMEVOLVER_LOG_LEVEL` | INFO |
| `directory` | `TEAMEVOLVER_LOG_DIR` | `~/.teamEvolver` |
| `file_enabled` | `TEAMEVOLVER_LOG_FILE_ENABLED` | true |
| `console_enabled` | `TEAMEVOLVER_LOG_CONSOLE_ENABLED` | true |
| `max_file_mb` | `TEAMEVOLVER_LOG_MAX_FILE_MB` | 100 |
| `retention_days` | `TEAMEVOLVER_LOG_RETENTION_DAYS` | 14 |
| `max_total_mb` | `TEAMEVOLVER_LOG_MAX_TOTAL_MB` | 2048 |

Priority from high to low: `--log-file`, environment variables, YAML, defaults. Booleans accept `1/0`, `true/false`, `yes/no`, `on/off`; sizes and days must be positive integers, and invalid values fail startup.

At startup the `logging.configured` event records the final path and the source of each field.

## Rotation and Retention

- Rotates by local date and file size; archives are named `teamEvolver.log.YYYY-MM-DD.000001` with increasing sequence numbers that never overwrite
- Retention and total size are checked at startup, on rotation and every minute; the oldest archives are deleted first
- Only regular files matching the archive naming are deleted; the active file, symlinks and other files are never touched
- The active file may briefly push the total above `max_total_mb`; keep `max_total_mb >= max_file_mb`

## Degradation

Logs go through a bounded queue of at most 8192 records written by a dedicated thread:

- When the queue is full, records are dropped and counted in `dropped_count`; ERROR and above are also written directly to standard error
- When the directory is unwritable, the disk is full or rotation fails, the file state becomes `degraded`; logs go to standard error and recovery is retried every minute
- `file_missed_count` counts records not written to the file; they were still written to standard error
- At exit, the process waits about 3 seconds to drain the queue

Degradation and recovery are logged as `logging.file_degraded` and `logging.file_recovered`; the periodic health summary is `logging.health`. Log write failures never affect request handling.

## Log Format

Each line contains a time with time zone, level, logger, PID, event name and structured fields. HTTP requests use a validated `X-Request-ID` (generated when missing) and echo it in the response header; thread-pool work inherits the request context and Worker tasks are correlated by `task_id`. Successful GET polling is logged at DEBUG.

The following never enter logs: model prompt and response bodies, source document bodies, approval notes, credentials, cookies. Fields matching `api_key`, `password`, `secret`, `authorization`, `cookie`, `token`, `root_key`, and registered configuration secrets, are redacted. Stack traces keep file, line and exception type and omit business data in exception messages.

## Investigation Order

1. Use `logging.configured` to confirm the process loaded the expected configuration and directory.
2. Correlate `http.completed` and later events by request ID or `job_id`.
3. Requests to unmatched routes are logged as `http.completed` with error code `TE_ROUTE_NOT_REGISTERED` and route field `<unmatched>`.

## Related Documents

- [Configuration Reference](./01-configuration.md)
- [Logging Status API](../api/15-logging-status.md)
- [Troubleshooting](./06-troubleshooting.md)
