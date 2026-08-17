# CLI & Daemon Reference

teamEvolver provides command-line tools for service management, configuration management, skill synchronization, diagnostics, etc. This document details all CLI commands and daemon runtime mechanics.

CLI entry point defined in `teamEvolver/cli/__init__.py`, executable command name `teamEvolver`.

## General Notes

### Command Structure

```
teamEvolver <command> [subcommand] [options] [arguments]
```

View all available commands:

```bash
teamEvolver --help
```

View subcommand help:

```bash
teamEvolver skills --help
teamEvolver langfuse pull --help
```

### Runtime File Locations

All runtime state files stored under `~/.teamEvolver/` directory:

| File | Description |
|------|-------------|
| `config.yaml` | Main configuration file |
| `teamEvolver.pid` | Daemon mode PID file |
| `teamEvolver.log` | Daemon mode default log file |
| `prompt_overrides.json` | Prompt Studio Prompt override configuration |
| `stage_settings.json` | Per-stage model parameter override configuration |
| `console_sessions.json` | Web console login sessions |
| `users.json` | Console user registry |
| `agent-protocol.env` | Agent protocol environment variables (optional, auto-loaded on daemon start) |

## Service Management Commands

### teamEvolver start

Starts teamEvolver service.

```bash
teamEvolver start [OPTIONS]
```

Options:

| Option | Description |
|--------|-------------|
| `--port INTEGER` | Override service port for this start (does not modify persistent configuration) |
| `-d, --daemon` | Run as daemon (background) process |
| `--log-file PATH` | Specify log file path in Daemon mode, default `~/.teamEvolver/teamEvolver.log` |

Examples:

```bash
# Foreground start (development/debug)
teamEvolver start

# Background start
teamEvolver start --daemon

# Background start with temporary port 30001
teamEvolver start --daemon --port 30001

# Background start with specified log file
teamEvolver start --daemon --log-file /var/log/teamevolver.log
```

Startup behavior:

1. Checks if configuration file exists; if not prompts to run `teamEvolver config` first
2. In Daemon mode:
   - Acquires startup lock to prevent duplicate starts
   - Creates child process, stdin/stdout/stderr redirected to log file
   - Sets `TEAMEVOLVER_RUNTIME_KIND=daemon` and `TEAMEVOLVER_RUNTIME_LOG_PATH` environment variables
   - Waits up to 15 seconds (configurable via `TEAMEVOLVER_DAEMON_READY_TIMEOUT_S` env var) until `/healthz` returns ok
   - On startup failure auto-cleans child process and PID file
3. In foreground mode: Runs Launcher directly until Ctrl+C interrupt received
4. When `--port` specified: Creates temporary configuration file, auto-cleaned on exit

### teamEvolver stop

Stops running teamEvolver daemon.

```bash
teamEvolver stop
```

Behavior:

1. Reads `~/.teamEvolver/teamEvolver.pid` to get PID
2. Sends SIGTERM signal to process
3. Deletes PID file
4. If process doesn't exist (stale PID file), only cleans PID file with notification

Note: Services started in foreground mode cannot be stopped via this command; use Ctrl+C.

### teamEvolver status

Checks teamEvolver service running status.

```bash
teamEvolver status
```

Possible outputs:

```
teamEvolver: not running                    # PID file doesn't exist
teamEvolver: not running (stale PID file)   # Process dead, cleaning stale PID
teamEvolver: starting (PID=12345, service=:52010)  # Process exists but /healthz not ready
teamEvolver: running  (PID=12345, service=:52010) # Running normally
```

Status check confirms service readiness via HTTP request to `http://127.0.0.1:<port>/healthz`.

## Configuration Management Commands

### teamEvolver config

Reads or modifies configuration values.

```bash
teamEvolver config <key_or_action> [value]
```

Usage:

```bash
# View all configuration (including config file path)
teamEvolver config show

# Read single configuration item (dot-separated nested key)
teamEvolver config <key>

# Set configuration item
teamEvolver config <key> <value>
```

Examples:

```bash
# View complete configuration
teamEvolver config show

# Read LLM API Key
teamEvolver config llm.api_key

# Set port
teamEvolver config service.port 52010

# Enable Langfuse tracing
teamEvolver config langfuse.tracing_enabled true

# Set DreamCycle active window
teamEvolver config dreamcycle.active_start_hour 22
teamEvolver config dreamcycle.active_end_hour 6
```

Notes:

- String values `"true"`/`"false"` automatically converted to booleans
- Numeric strings automatically converted to integers or floats
- Set values immediately written to `config.yaml`, but some configuration requires service restart to take effect
- Supports arbitrary depth dot-separated keys, e.g., `evolve.dataset_min_requirements`

## Skill Management Commands

`teamEvolver skills` command group manages local skill synchronization with OpenViking cloud.

```bash
teamEvolver skills <subcommand> [options]
```

### teamEvolver skills push

Pushes local skills to cloud shared storage.

```bash
teamEvolver skills push [--no-filter]
```

Options:

| Option | Description |
|--------|-------------|
| `--no-filter` | Skip quality threshold filtering, push all local skills (default only pushes skills with injection count >=5 and effectiveness >=0.3) |

Push automatically checks statistics in `skills/skill_stats.json`; skills not meeting thresholds filtered to ensure shared skill quality.

### teamEvolver skills pull

Pulls shared skills from cloud to local.

```bash
teamEvolver skills pull
```

Pull results show: downloaded (new), skipped (no change), failed, deleted (locally deleted by cloud). On pull failure automatically attempts backup recovery.

### teamEvolver skills sync

Bidirectional sync: pull then push.

```bash
teamEvolver skills sync
```

Equivalent to executing `pull` then `push` sequentially.

### teamEvolver skills list-remote

Lists cloud-available shared skills.

```bash
teamEvolver skills list-remote
```

Displays each skill's name, category, description, uploader, and upload time.

### Skill Sync Prerequisites

Must correctly configure `sharing` section before using skill sync commands:

```bash
teamEvolver config sharing.enabled true
teamEvolver config sharing.viking_deployment cloud
teamEvolver config sharing.viking_personal_api_key "your-vk-key"
```

## Langfuse Commands

`teamEvolver langfuse` command group for Langfuse integration management and session pull.

```bash
teamEvolver langfuse <subcommand> [options]
```

### teamEvolver langfuse status

Checks Langfuse connection status and configuration.

```bash
teamEvolver langfuse status
```

Output includes: host, session_pull_enabled, tracing_enabled, tracing_environment, key presence, default filters, SDK availability, service connectivity, total sessions.

### teamEvolver langfuse list

Lists Langfuse sessions matching filter conditions (does not perform ingestion).

```bash
teamEvolver langfuse list [FILTER_OPTIONS]
```

Common filter options (applies to both `list` and `pull`):

| Option | Description |
|--------|-------------|
| `-e, --environment ENV` | Filter by environment (repeatable for multiple) |
| `-u, --user-id ID` | Filter by userId |
| `--tag TAG` | Filter by tag (repeatable, all must match) |
| `--release RELEASE` | Filter by release |
| `--version VERSION` | Filter by version |
| `--name NAME` | Filter by trace name |
| `--session-id ID` | Pull specified single session |
| `--from TIMESTAMP` | Only pull sessions after this ISO 8601 time |
| `--to TIMESTAMP` | Only pull sessions before this ISO 8601 time |
| `-m, --metadata KEY=VALUE` | Filter by metadata key-value pairs (repeatable) |
| `--max-sessions N` | Limit returned session count |

Examples:

```bash
# List production environment sessions
teamEvolver langfuse list -e production

# List sessions with specified tags
teamEvolver langfuse list --tag agent --tag coding

# List recent sessions for specific user
teamEvolver langfuse list -u user-123 --from 2026-08-01T00:00:00Z
```

### teamEvolver langfuse pull

Pulls matching Langfuse sessions into teamEvolver evolution pipeline.

```bash
teamEvolver langfuse pull [FILTER_OPTIONS] [OPTIONS]
```

Additional options:

| Option | Description |
|--------|-------------|
| `--user-alias ALIAS` | Set user_alias attribution for pulled sessions |
| `--force` | Force reprocess sessions with unchanged content |
| `--defer-trigger` | Enqueue but don't trigger evolution round |
| `--in-process` | Process directly locally (no dependency on running teamEvolver service) |

By default, `pull` executes via HTTP POST to running teamEvolver service's `/langfuse/pull` endpoint. Using `--in-process` processes directly within CLI process, suitable for bulk imports when service not running.

Examples:

```bash
# Pull using default config
teamEvolver langfuse pull

# Pull and mark source
teamEvolver langfuse pull --user-alias "langfuse-import" -e production

# Local bulk import
teamEvolver langfuse pull --in-process --max-sessions 200 --force
```

## Diagnostic Commands

`teamEvolver diag` related command groups for integration diagnostics and maintenance.

### teamEvolver doctor hermes

Checks local Hermes integration status.

```bash
teamEvolver doctor hermes
```

Diagnostics include:

- Configuration file existence
- Model configuration matches expectations
- Base URL configuration
- Provider configuration
- Proxy matching status
- Skills directory existence and permissions
- Legacy skills directory status
- Latest backup information
- Session boundary mode
- Discovered issues and fix recommendations

### teamEvolver restore hermes

Restores Hermes configuration from backup.

```bash
teamEvolver restore hermes [--backup PATH]
```

| Option | Description |
|--------|-------------|
| `--backup PATH` | Specify backup file path; defaults to latest Hermes backup |

teamEvolver automatically creates backups before modifying Hermes configuration; this command rolls back when configuration anomalies occur.

### teamEvolver validation status

Views background validation Worker configuration and current status.

```bash
teamEvolver validation status
```

Shows validation mode, concurrency settings, required results/approvals count, whether Worker idle, etc.

### teamEvolver validation run-once

Runs one background validation polling iteration.

```bash
teamEvolver validation run-once [--force]
```

| Option | Description |
|--------|-------------|
| `--force` | Force execution even when Worker not idle |

By default, validation Worker only executes polling after idle exceeding `validation.idle_after_seconds`; this command manually triggers one.

## Daemon Process Management Details

### PID File Mechanism

After successful Daemon startup, child process PID written to `~/.teamEvolver/teamEvolver.pid`. `status` and `stop` commands locate process via this file.

If process exits abnormally without cleaning PID file, `status` command detects process non-existence and auto-cleans stale PID file.

### Startup Lock

`start --daemon` uses file lock to prevent concurrent starts. If another daemon startup in progress, displays corresponding PID and exits.

### Health Check Wait

After Daemon startup, parent process polls `http://127.0.0.1:<port>/healthz` endpoint, waiting up to 15 seconds. Timeout judges startup failed, terminates child process and errors. Wait time adjustable via environment variable:

```bash
TEAMEVOLVER_DAEMON_READY_TIMEOUT_S=30 teamEvolver start --daemon
```

### Environment Variable Propagation

Daemon child process inherits current shell environment variables, additionally sets:

- `TEAMEVOLVER_RUNTIME_KIND=daemon`
- `TEAMEVOLVER_RUNTIME_LOG_PATH=<log-path>`

Additionally, if `~/.teamEvolver/agent-protocol.env` file exists, environment variables loaded dotenv-style (as defaults only, not overriding existing environment variables).

### Log Handling

In Daemon mode both stdout and stderr redirected to log file (append mode):

- Default log path: `~/.teamEvolver/teamEvolver.log`
- Customizable via `--log-file`
- Parent process creates log file directory if it doesn't exist
- Foreground mode logs output directly to terminal

### Process Termination

- `teamEvolver stop` sends SIGTERM
- systemd management sends SIGTERM via systemd, sends SIGKILL after timeout
- Ctrl+C triggers graceful shutdown in foreground mode

## Environment Variable Reference

Besides configuration file, following environment variables affect CLI and Daemon behavior:

| Environment Variable | Description |
|---------------------|-------------|
| `TEAMEVOLVER_DAEMON_READY_TIMEOUT_S` | Daemon startup health check timeout seconds, default 15 |
| `EVOLVE_INGEST_API_KEY` | Bearer Token auth key for `/ingest_session` and `/langfuse/pull` endpoints |
| `TEAMEVOLVER_PROXY_API_KEY` | Model proxy endpoint API Key |
| `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` | Session upload request body max bytes, default 32MB |
| `EVOLVE_HISTORY_PATH` | Evolution history JSONL file path |
| `TEAMEVOLVER_PROMPT_OVERRIDES_PATH` | Custom Prompt override file path |
| `TEAMEVOLVER_STAGE_SETTINGS_PATH` | Custom stage settings file path |
| `ARK_API_KEY` | Volcengine Ark API Key (used by Skill Miner) |
| `SKILLMINER_LIFT_AUTO_DRAFT` | Set to `0` to disable auto-generating LIFT drafts |
| `LANGFUSE_*` | Various Langfuse configuration environment variables; see observability guide |

## Exit Codes

| Exit Code | Description |
|-----------|-------------|
| 0 | Success |
| 1 | General error (missing config, command execution failure, etc.) |

Common error scenarios:

- Executing `start` when configuration file doesn't exist: exit code 1, prompts to run `teamEvolver config` first
- Starting when port occupied: Daemon child process exits abnormally, parent errors and prompts to view logs
- Executing `skills push/pull/sync` when sharing not configured: errors prompting to configure sharing first
- Executing `langfuse` commands when Langfuse not configured or keys missing: errors prompting configuration
