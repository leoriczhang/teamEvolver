# Service logging

Foreground, container and daemon starts use the same bounded logging queue and writer thread. Logs go to stderr and `~/.teamEvolver/teamEvolver.log` by default. SIT/PRD templates use `/app/deploy/logs/teamEvolver.log`. Ontology working artifacts remain under `ontology.state_dir`; they are not log files.

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

These are service settings; tenant overrides are ignored. Corresponding environment variables are `TEAMEVOLVER_LOG_LEVEL`, `TEAMEVOLVER_LOG_CONSOLE_ENABLED`, `TEAMEVOLVER_LOG_FILE_ENABLED`, `TEAMEVOLVER_LOG_DIR`, `TEAMEVOLVER_LOG_MAX_FILE_MB`, `TEAMEVOLVER_LOG_RETENTION_DAYS`, and `TEAMEVOLVER_LOG_MAX_TOTAL_MB`. Priority: explicit `--log-file` (also enables file output), process environment, loaded environment file, YAML, defaults. Restart to apply changes.

Directories are created automatically. Rotation uses the local date and size in MiB; archives are named `teamEvolver.log.YYYY-MM-DD.000001`. Startup, rotation and minute checks delete only matching archives, oldest first. Active files, artifacts, unrelated files and symlinks are never removed. Keep the total budget larger than the per-file limit. The active file can temporarily exceed the total budget.

Enable `TEAMEVOLVER_MULTI_REPLICA=1` for shared log volumes. Give each replica a stable, unique `TEAMEVOLVER_INSTANCE_ID` to reuse its directory on restart; otherwise hostname and PID are used. A file lock prevents concurrent ownership. Retention is per instance; abandoned PID directories need platform lifecycle cleanup.

A queue holds at most 8192 records. Overflow is counted; ERROR and higher also use stderr. Shutdown drains for up to approximately three seconds. File failures degrade to stderr, even when regular console output is disabled, with recovery attempted every minute. Administrators can inspect the current process through the runtime view and [logging status API](../api/15-logging-status.md). A blocked filesystem cannot guarantee delivery, but it does not block the request event loop.

Validated `X-Request-ID` values correlate HTTP requests, OV calls and thread-pool work. Background jobs use task ID and attempt. `TE_ROUTE_NOT_REGISTERED` identifies an unmatched TE route; `OV_NOT_FOUND`, `ONTOLOGY_DISABLED`, `FORBIDDEN`, `OV_TIMEOUT` and identity mismatch events identify upstream failures. Successful GET polling uses DEBUG. Repeated ontology failures are summarized; first failure, changed error and recovery are logged immediately.

Credentials, query parameters and business payloads are excluded from diagnostic events. Tracebacks retain frame locations and exception types, not arbitrary exception values or code lines. Legacy SkillMiner output is drained with bounded reads and reduced to safe metadata. Adding diagnostics does not change permissions, publication semantics, schemas or feature flags and does not itself resolve existing Not Found errors. Actual SIT mounts and permissions still require deployment acceptance.

## Access save displays `[object Object]`

A SIT reproduction on 2026-09-21 identified Ontology JSON requests sent as `text/plain`, causing TE to return HTTP 422 before persisting access. The old client rendered FastAPI validation arrays as `[object Object]`. The fix explicitly sets `application/json` on Ontology POST/PUT requests and displays validation locations/messages without echoing `input` or `ctx`. Inspect the PUT receipt separately from subsequent capabilities GET errors. The frontend fix requires an updated frontend deployment.
