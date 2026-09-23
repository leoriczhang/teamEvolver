# Logging status API

`GET /api/logging/status` requires an authenticated console administrator. It describes the current process; it does not read, download or edit arbitrary files.

The response includes `path`, `config_file`, `env_file`, per-field `sources`, `level`, output switches, rotation budgets, `file_state`, `last_write_error`, `dropped_count`, `file_missed_count`, queue depth/capacity and timezone. File states are `starting`, `active`, `disabled`, `degraded`, or `not_configured`. The last error remains available after recovery. Missed file writes can still be available in platform stderr logs. A load balancer may send each call to a different instance.

HTTP responses return a validated or generated `X-Request-ID`. See [Service logging](../guides/15-service-logging.md) for deployment and diagnostics.
