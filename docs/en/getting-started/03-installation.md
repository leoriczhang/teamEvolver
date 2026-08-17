# Installation & Deployment

## Environment Requirements

| Component | Minimum Version | Description |
|-----------|-----------------|-------------|
| Python | 3.10+ | Runs the teamEvolver backend |
| pip | 23.0+ | Installs Python packages |
| Node.js | 18+ | Only required when modifying the frontend |
| OpenViking | 0.4+ | Persistent storage backend, local or cloud |
| Operating System | Linux / macOS | Windows requires WSL2 |

## Installation Methods

### pip Installation (Recommended)

```bash
pip install teamEvolver
```

### Source Installation

```bash
git clone https://github.com/leoriczhang/teamEvolver.git
cd teamEvolver
pip install -e ".[all]"
```

Optional dependency groups:

| extra | Contents |
|-------|----------|
| `sharing` | OpenViking shared storage support (boto3) |
| `mining` | Skill Miner document mining (hermes-agent) |
| `validation` | True Replay validation (openai SDK) |
| `truereplay` | Full True Replay capability |
| `dev` | Development dependencies (pytest, anyio) |
| `all` | All dependencies |

### Frontend Build (Developers Only)

Standard installation uses pre-built frontend artifacts; no build required. If you modify `web-ui/` source:

```bash
cd web-ui
npm install
npm run build
# Artifacts output to teamEvolver/web/dist/
```

## Initial Configuration

Run `teamEvolver config` for interactive setup, or directly edit `~/.teamEvolver/config.yaml`.

### Minimal Runnable Configuration

```yaml
service:
  port: 52010
  host: 0.0.0.0

llm:
  provider: custom
  model_id: doubao-seed-evolving
  api_base: https://ark.cn-beijing.volces.com/api/v3
  api_key: "your-llm-api-key"

sharing:
  enabled: true
  backend: viking
  viking_deployment: local
  viking_endpoint: http://localhost:1933
  viking_api_key: "your-openviking-key"

evolve:
  enabled: true
  publish_mode: validated
  human_review_enabled: true

validation:
  enabled: true
  mode: true_replay
```

### Configuration File Locations

| Path | Description |
|------|-------------|
| `~/.teamEvolver/config.yaml` | Main configuration file |
| `~/.teamEvolver/skills/` | Local Skill cache directory |
| `~/.teamEvolver/teamEvolver.pid` | Daemon PID file |
| `~/.teamEvolver/logs/` | Log directory |

## Startup & Management

### Foreground Startup (for debugging)

```bash
teamEvolver start
```

### Background Daemon

```bash
teamEvolver start --daemon
```

### Check Status

```bash
teamEvolver status
```

### Stop Service

```bash
teamEvolver stop
```

### View Logs

```bash
# In Daemon mode
tail -f ~/.teamEvolver/logs/teamEvolver.log
```

## Verify Installation

```bash
# 1. Check service health
curl -fsS http://localhost:52010/health

# 2. Check status
curl -fsS http://localhost:52010/status | python -m json.tool

# 3. Access console
# Open http://localhost:52010/console in browser

# 4. Run tests (for source installation)
python -m pytest tests/ -v
```

## Upgrading

```bash
pip install --upgrade teamEvolver
# Or for source installation:
cd teamEvolver && git pull && pip install -e ".[all]"

# Restart service
teamEvolver stop && teamEvolver start --daemon
```

## Related Documentation

- [Configuration Reference](../guides/01-configuration): Complete documentation of all configuration options
- [Production Deployment](../guides/02-deployment): Best practices for production environment deployment
