# Service Logging Status API

`GET /api/logging/status` returns the logging runtime state of the current process. It is read-only and requires a platform administrator (or the Root Key). There are no endpoints for downloading or editing logs or reading arbitrary files.

Code entry points: `teamEvolver/observability/api/__init__.py:create_router`, `teamEvolver/logging_runtime.py:logging_status`.

## Response Fields

| Field | Meaning |
| --- | --- |
| `path` | Actual log file path |
| `config_file`, `env_file` | YAML and environment file paths used at startup |
| `sources` | Source of each logging parameter: `cli`, `process_environment`, environment file path, `yaml` or `default` |
| `level`, `file_enabled`, `console_enabled` | Effective level and output switches |
| `directory`, `max_file_mb`, `retention_days`, `max_total_mb` | Directory, rotation and cleanup budget |
| `file_state` | `starting`, `active`, `disabled`, `degraded`; `not_configured` before initialization |
| `last_write_error` | Most recent disk write error (redacted), kept after recovery |
| `dropped_count` | Entries dropped on queue overflow or shutdown timeout |
| `file_missed_count` | Entries not written to disk during file failures |
| `queue_capacity`, `queue_depth` | Bounded queue capacity and current backlog |
| `timezone` | Process local time zone |

## Logging Parameters

| Parameter | Environment variable | Default |
| --- | --- | --- |
| `level` | `TEAMEVOLVER_LOG_LEVEL` | `INFO` |
| `console_enabled` | `TEAMEVOLVER_LOG_CONSOLE_ENABLED` | `true` |
| `file_enabled` | `TEAMEVOLVER_LOG_FILE_ENABLED` | `true` |
| `directory` | `TEAMEVOLVER_LOG_DIR` | `~/.teamEvolver` |
| `max_file_mb` | `TEAMEVOLVER_LOG_MAX_FILE_MB` | 100 |
| `retention_days` | `TEAMEVOLVER_LOG_RETENTION_DAYS` | 14 |
| `max_total_mb` | `TEAMEVOLVER_LOG_MAX_TOTAL_MB` | 2048 |

Environment variables take precedence over the YAML `logging` section. `teamEvolver start --log-file` sets the file and forces file output on. With `TEAMEVOLVER_MULTI_REPLICA=1`, logs go to `<directory>/<instance>/teamEvolver.log`, where the instance name is `TEAMEVOLVER_INSTANCE_ID`, or host name plus PID when unset.

## Request Correlation

Every HTTP response carries `x-request-id` and `x-trace-id`: values sent by the request are echoed back, otherwise the server generates them, and they are written to the request's `http.access` access log.

Troubleshooting and configuration examples are in the [Service Logging Guide](../guides/15-service-logging.md).
