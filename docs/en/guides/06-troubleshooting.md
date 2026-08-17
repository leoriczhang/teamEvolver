# Troubleshooting Guide

This guide compiles troubleshooting approaches and solutions for common teamEvolver issues.

## Log Locations

First check logs when troubleshooting:

| Runtime Mode | Log Location |
|--------------|--------------|
| Foreground mode | stdout/stderr (terminal output) |
| Daemon mode | `~/.teamEvolver/teamEvolver.log` |
| systemd mode | `journalctl -u teamevolver -f` |
| Skill Miner | `teamEvolver/skillminer/logs/` |
| Nginx reverse proxy | `/var/log/nginx/teamevolver.access.log` and `/var/log/nginx/teamevolver.error.log` |

View Daemon log tail:

```bash
tail -f ~/.teamEvolver/teamEvolver.log
```

View recent 100 lines systemd logs:

```bash
journalctl -u teamevolver -n 100 --no-pager
```

## Service Startup Issues

### Problem: Service cannot start

**Symptoms**: Error or immediate exit after executing `teamEvolver start`.

**Troubleshooting steps**:

1. Check if configuration file exists:
   ```bash
   ls -la ~/.teamEvolver/config.yaml
   ```
   If doesn't exist, initialize configuration first:
   ```bash
   teamEvolver config llm.api_key "your-key"
   ```

2. Check if port occupied:
   ```bash
   netstat -tlnp | grep 52010
   # or
   lsof -i :52010
   ```
   If port occupied, you can:
   - Stop the process occupying port
   - Or start with other port: `teamEvolver start --port 52011`
   - Or modify persistent configuration: `teamEvolver config service.port 52011`

3. Foreground start to view detailed errors:
   ```bash
   teamEvolver start
   ```
   Foreground mode outputs errors directly to terminal for easier diagnosis.

4. Check LLM API configuration:
   - Confirm `llm.api_key` not empty
   - Confirm `llm.api_base` accessible:
     ```bash
     curl -s -o /dev/null -w "%{http_code}" https://ark.cn-beijing.volces.com/api/v3/models
     ```

### Problem: Daemon startup timeout

**Symptoms**: `teamEvolver start --daemon` errors "did not become healthy in time".

**Troubleshooting steps**:

1. View log file:
   ```bash
   cat ~/.teamEvolver/teamEvolver.log
   ```

2. Increase timeout:
   ```bash
   TEAMEVOLVER_DAEMON_READY_TIMEOUT_S=30 teamEvolver start --daemon
   ```

3. Check if port bindable: If `service.host` configured to specific NIC address but NIC doesn't exist, bind fails.

### Problem: PID file exists but process dead

**Symptoms**: `teamEvolver status` shows "not running (stale PID file)".

Typically caused by abnormal process crash (OOM, kill -9) not cleaning PID file. `status` command auto-cleans stale PID file, can restart afterwards.

## OpenViking Connection Issues

### Problem: Skill cloud sync fails

**Symptoms**: `teamEvolver skills pull` or `push` reports connection errors.

**Troubleshooting steps**:

1. Check sharing enabled:
   ```bash
   teamEvolver config sharing.enabled
   ```

2. Check deployment mode configuration:
   ```bash
   teamEvolver config sharing.viking_deployment
   ```
   Value should be `cloud` or `local`.

3. Verify API Key configuration:
   - `sharing.viking_personal_api_key` (personal space)
   - `sharing.viking_team_api_key` (team space)
   - Or generic `sharing.viking_api_key`

4. Check endpoint configuration:
   ```bash
   teamEvolver config sharing.viking_endpoint
   ```
   If `viking_deployment` is `cloud`, endpoint should be Volcengine OpenViking address; if `local`, should be self-hosted openviking-server address.

5. Test network connectivity:
   ```bash
   curl -s -o /dev/null -w "%{http_code}" \
     -H "Authorization: Bearer your-key" \
     "https://your-viking-endpoint/api/v1/health"
   ```

6. local mode confirm openviking-server running normally.

### Problem: Pulled skills incomplete or missing

1. Check `push_min_injections` and `push_min_effectiveness` thresholds: low-quality skills not pushed to cloud
2. Confirm API key in use has access to corresponding space
3. Check `viking_root_prefix` not accidentally modified (this value should usually be default; modification reads different namespace)

## Agent Integration Issues

### Problem: Agent cannot connect to teamEvolver

**Symptoms**: Agent calls to `/ingest_session` return 401 Unauthorized.

**Troubleshooting steps**:

1. Check if `EVOLVE_INGEST_API_KEY` environment variable set:
   ```bash
   echo $EVOLVE_INGEST_API_KEY
   ```
   If set, Agent requests must carry correct `Authorization: Bearer <key>` header.

2. If no auth needed, ensure `EVOLVE_INGEST_API_KEY` not set (environment variable not injected at service startup).

3. Check if Agent requests reach correct address and port:
   ```bash
   # Test connectivity from Agent machine
   curl -v http://teamevolver-host:52010/healthz
   ```

4. If Nginx reverse proxy present, check if Nginx correctly forwards Authorization header.

### Problem: Agent reporting sessions returns 403 Forbidden

**Symptoms**: Returns "subject not mapped" or 403 error.

**Cause**: After Agent registration, its subject (identity identifier) needs mapping to a teamEvolver user.

**Solution**:

1. Access user management page in console
2. Find corresponding Agent, configure subject-to-user mapping
3. Or confirm user identifiers like `sharing.viking_personal_user` correct in configuration

### Problem: Agent reporting sessions unresponsive or timeout

1. Check service running: `teamEvolver status`
2. Check if request body size exceeds limit (default 32MB):
   - Oversized sessions may trigger 413 errors
   - Adjustable via `TEAMEVOLVER_MAX_SESSION_BODY_BYTES`
3. Check if reverse proxy timeout configuration too short

## Session & Evolution Issues

### Problem: Sessions not appearing in console after reporting

**Symptoms**: Agent successfully calls `/ingest_session` returns 200, but not visible in console queue.

**Troubleshooting steps**:

1. Check if session marked skip by value classifier:
   - Session Filter stage determines whether session high-value
   - Chitchat, low-quality, duplicate sessions skipped, not entering evolution queue
   - Check history in console if marked "skipped"

2. Check if judged duplicate:
   - Duplicate reports for same session_id deduplicated
   - Using `--force` (when Langfuse pulling) forces reprocessing

3. Confirm ingest endpoint authentication passed: If `EVOLVE_INGEST_API_KEY` configured, requests must carry correct key; otherwise may return 200 but rejected (depending on deployment config).

4. Check value_judge results in logs:
   ```bash
   grep -i "value_judge\|skipped\|queued" ~/.teamEvolver/teamEvolver.log | tail -50
   ```

### Problem: Skill sync not working

**Symptoms**: Evolution produced new skill versions, but not synced to cloud or other local Agents.

**Troubleshooting steps**:

1. Check skill sync outbox: View if any unsuccessfully uploaded change events
2. Confirm `sharing.enabled` is `true`
3. Check `skill_reload_mode` configuration:
   - `off`: No auto-reload, manual trigger needed
   - `poll`: Polling per `skill_reload_interval_seconds` (default 30 seconds)
   - `callback`: Callback mode (requires OpenViking webhook support)
4. Manually trigger sync:
   ```bash
   teamEvolver skills sync
   ```
5. Check if skills meet push thresholds (injection count, effectiveness):
   - Skills below thresholds not pushed
   - Use `--no-filter` to force push: `teamEvolver skills push --no-filter`
6. Check replay endpoint configuration: Pi Agent URL needs correct configuration for distributed validation

### Problem: Evolution not running automatically

1. Check evolution interval configuration:
   ```bash
   teamEvolver config evolve.interval_seconds
   ```
   Default 600 seconds (10 minutes). After first startup need to wait one interval before first evolution round starts.

2. Manually trigger one evolution: Call `/trigger` HTTP endpoint (requires API Key if configured).

3. Check if any sessions waiting in queue: evolution cycle may skip certain stages when no sessions.

4. Check logs for evolution errors:
   ```bash
   grep -i "evolve\|error\|exception" ~/.teamEvolver/teamEvolver.log | tail -100
   ```

## Validation & Replay Issues

### Problem: True Replay validation fails

**Symptoms**: Candidate skills stuck in evaluating or marked replay_error.

**Troubleshooting steps**:

1. Check runtime isolation: True Replay requires independent workspace to execute candidate skills
   - Confirm sufficient disk space and temp directory permissions
   - Pi Agent distributed mode: confirm worker nodes accessible

2. Context Hash mismatch:
   - True Replay relies on deterministic context reconstruction
   - If initial state inconsistent between baseline replay and candidate replay, causes hash mismatch
   - Check if external state (filesystem, network calls) affects replay

3. Check validation Worker logs:
   ```bash
   teamEvolver validation status
   teamEvolver validation run-once
   ```
   Run manually once to view detailed errors.

4. Temporarily switch to lightweight replay mode to verify if True Replay-specific issue:
   ```bash
   teamEvolver config validation.mode replay
   ```

### Problem: Validation never produces results

1. Check if `validation.max_concurrency` too low (default 1)
2. Check if `validation.max_jobs_per_day` hit cap (default 5)
3. Confirm Worker started: background validation Worker starts with main service
4. Check `validation.idle_after_seconds`: Worker may be sleeping; manually trigger run-once to wake

## Performance Issues

### Problem: Evolution runs slowly

**Possible causes and solutions**:

1. **LLM API response slow**:
   - Check `llm.api_base` network latency
   - Consider switching to closer API endpoint
   - Increase timeout configuration (if applicable)

2. **Evolution interval too short, queue backlog**:
   - Appropriately increase `evolve.interval_seconds` (e.g., 1200 or 1800)
   - Check session count in queue; consider adding resources for high throughput

3. **True Replay overhead**:
   - True Replay mode much slower than replay mode due to full workspace isolation and round-by-round replay
   - For performance-sensitive scenarios consider using `replay` mode (but lower precision)

4. **Too many sessions processed per round**:
   - Check `evidence_max_entries`; oversized evidence library increases LLM input length
   - Adjust `dataset_test_cases` and `dataset_min_requirements`, reduce test case count

5. **Token limits causing retries**:
   - Check `bundle_max_prompt_bytes`; if Prompt too large may cause API error retries
   - Appropriately adjust `llm.max_tokens`

### Problem: High memory usage

1. Check if large number of pending sessions accumulated in queue
2. True Replay parallel replay consumes significant memory; reduce `validation.max_concurrency`
3. Appropriately reduce `evidence_max_entries`
4. System level: Add swap or upgrade memory

### Problem: Log files too large

Configure logrotate to rotate logs; see log management section in [deployment guide](./02-deployment.md). Manual cleanup:

```bash
# Truncate log (don't delete file)
> ~/.teamEvolver/teamEvolver.log
```

## Langfuse Integration Issues

### Problem: Langfuse tracing data not appearing in Langfuse UI

1. Run status check:
   ```bash
   teamEvolver langfuse status
   ```
   Confirm `reachable: True`.

2. Confirm `tracing_enabled: true` vs `enabled` distinction:
   - `enabled = true` controls inbound pull
   - `tracing_enabled = true` controls outbound tracing
   - Two independent, can be enabled separately.

3. Check public_key and secret_key correct:
   - Must be Langfuse Project API Keys, not Account API Keys
   - Public Key starts with `pk-lf-`, Secret Key starts with `sk-lf-`

4. Check `tracing_sample_rate`: If set to 0.1, only ~10% of calls traced

5. Check `host` accessible from server:
   - Self-hosted Langfuse confirm URL and port correct
   - cloud.langfuse.com check firewall allows outbound HTTPS

6. Check logs for `[Langfuse] tracing unavailable` warnings

### Problem: Langfuse pull not finding sessions

1. First use `list` command to confirm match count:
   ```bash
   teamEvolver langfuse list --environment production
   ```

2. Check if filter conditions too strict:
   - Whether default filters (`default_environment`, `default_tags`, etc.) match your Langfuse data tags
   - Temporarily clear default filters: set these to empty lists in configuration

3. Langfuse tags are at Trace level not Session level: If your Agent doesn't correctly set trace tags, may not filter by tags/userId.

4. Time range issues: Whether `from_timestamp`/`to_timestamp` cover session times

## Web Console Issues

### Problem: Cannot access console

1. Confirm service running: `teamEvolver status`
2. Check port listening: `netstat -tlnp | grep 52010`
3. If firewall present, confirm port open (or access via Nginx)
4. Check `service.host` bound to correct address:
   - `0.0.0.0` listens on all NICs
   - `127.0.0.1` local access only (requires Nginx proxy)

### Problem: Logged out immediately after login

1. Check `~/.teamEvolver/console_sessions.json` permissions
2. Clear browser cookies and retry
3. Check system time correct (session TTL calculated based on time)

## Diagnostic Tools

### teamEvolver doctor hermes

Run integration diagnostics, automatically checking common configuration issues:

```bash
teamEvolver doctor hermes
```

Output includes detected issues, notes, and suggested next_steps.

### Health Check Endpoints

```bash
# Basic health check
curl http://127.0.0.1:52010/healthz

# Detailed status
curl http://127.0.0.1:52010/status
```

### Configuration Check

View all currently effective configuration:

```bash
teamEvolver config show
```

### Validation Worker Status

```bash
teamEvolver validation status
```

## Getting Help

If above methods cannot resolve issue:

1. Collect relevant log snippets (50 lines before/after error)
2. Record version number: `teamEvolver --version` (if applicable)
3. Record configuration (redact API keys and other sensitive info)
4. Describe reproduction steps
5. Check for related GitHub Issues
