# Production Deployment Guide

This guide covers deploying and operating teamEvolver service in production environments.

## Prerequisites

- Python 3.11 or higher
- Accessible LLM API endpoint (Volcengine Ark, OpenAI-compatible interface, etc.)
- OpenViking credentials if using cloud skill sync
- Linux server (Ubuntu 22.04 / Debian 12 recommended)
- At least 2 CPU cores, 4GB RAM

## Runtime Modes

teamEvolver supports two process management modes:

### Foreground Mode (for development/debugging)

```bash
teamEvolver start
```

Foreground mode outputs logs directly to terminal; press Ctrl+C to stop service. Suitable for development and debugging.

### Daemon Mode (for single-machine deployment)

```bash
teamEvolver start --daemon
```

Daemon mode runs service process in background, logs written to `~/.teamEvolver/teamEvolver.log`, PID file at `~/.teamEvolver/teamEvolver.pid`.

```bash
# Check status
teamEvolver status

# Stop service
teamEvolver stop
```

### systemd Mode (recommended for production)

For production environments, systemd is recommended for process management, enabling auto-start on boot, automatic restart, log aggregation, etc.

## systemd Service Configuration

Create systemd service file `/etc/systemd/system/teamevolver.service`:

```ini
[Unit]
Description=teamEvolver Skill Evolution Service
After=network.target

[Service]
Type=simple
User=teamevolver
Group=teamevolver
WorkingDirectory=/home/teamevolver
ExecStart=/home/teamevolver/.local/bin/teamEvolver start
Restart=always
RestartSec=10
Environment=PATH=/home/teamevolver/.local/bin:/usr/local/bin:/usr/bin:/bin
Environment=EVOLVE_INGEST_API_KEY=your-secure-ingest-key-here
Environment=TEAMEVOLVER_PROXY_API_KEY=your-secure-proxy-key-here
Environment=ARK_API_KEY=your-volcengine-ark-key-here

# Security hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/teamevolver/.teamEvolver
ReadWritePaths=/home/teamevolver/.hermes

# Resource limits
LimitNOFILE=65536
MemoryMax=8G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
```

Create dedicated user and start service:

```bash
# Create system user
sudo useradd -r -m -s /bin/bash teamevolver

# Install teamEvolver to user environment
sudo -u teamevolver pip install teamEvolver

# Initialize configuration
sudo -u teamevolver teamEvolver config llm.api_key "your-llm-key"
sudo -u teamevolver teamEvolver config service.host "127.0.0.1"

# Enable and start service
sudo systemctl daemon-reload
sudo systemctl enable teamevolver
sudo systemctl start teamevolver

# Check status
sudo systemctl status teamevolver

# View logs
sudo journalctl -u teamevolver -f
```

## Nginx Reverse Proxy Configuration

In production, Nginx should be used as reverse proxy providing TLS termination, access control, rate limiting, etc.

Create Nginx config file `/etc/nginx/sites-available/teamevolver`:

```nginx
upstream teamevolver_backend {
    server 127.0.0.1:52010;
    keepalive 32;
}

server {
    listen 80;
    server_name teamevolver.example.com;
    return 301 https://$host$request_uri;
}

server {
    listen 443 ssl http2;
    server_name teamevolver.example.com;

    ssl_certificate /etc/letsencrypt/live/teamevolver.example.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/teamevolver.example.com/privkey.pem;
    ssl_protocols TLSv1.2 TLSv1.3;
    ssl_ciphers HIGH:!aNULL:!MD5;

    client_max_body_size 64M;

    # Access logs
    access_log /var/log/nginx/teamevolver.access.log;
    error_log /var/log/nginx/teamevolver.error.log;

    # Console static assets
    location / {
        proxy_pass http://teamevolver_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Agent ingest endpoint - recommend enabling API Key auth
    location /ingest_session {
        limit_req zone=ingest burst=20 nodelay;
        proxy_pass http://teamevolver_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # Optional: Nginx-layer Basic Auth (dual protection with EVOLVE_INGEST_API_KEY)
        # auth_basic "teamEvolver Ingest";
        # auth_basic_user_file /etc/nginx/.htpasswd-teamevolver;
    }

    # Langfuse pull endpoint
    location /langfuse/ {
        proxy_pass http://teamevolver_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # Health check endpoint (internal access only)
    location /healthz {
        proxy_pass http://teamevolver_backend;
        allow 127.0.0.1;
        allow 10.0.0.0/8;
        allow 172.16.0.0/12;
        allow 192.168.0.0/16;
        deny all;
    }

    location /status {
        proxy_pass http://teamevolver_backend;
        allow 127.0.0.1;
        allow 10.0.0.0/8;
        deny all;
    }
}

# Rate limiting configuration
limit_req_zone $binary_remote_addr zone=ingest:10m rate=10r/s;
```

Enable configuration:

```bash
sudo ln -s /etc/nginx/sites-available/teamevolver /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## Firewall Rules

Use `ufw` or `iptables` to restrict network access:

```bash
# Allow SSH, HTTP, HTTPS only
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# Deny external direct access to teamEvolver port
sudo ufw deny 52010/tcp

sudo ufw enable
```

Ensure `service.host` configured as `"127.0.0.1"`, preventing service from directly listening on public addresses.

## Health Checks & Monitoring

### Health Check Endpoints

teamEvolver provides HTTP endpoints for monitoring:

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/healthz` | GET | Health check, returns 200 `{"ok": true}` when service healthy |
| `/status` | GET | Service status details including queue, version, configuration info |

Health check example:

```bash
curl -s http://127.0.0.1:52010/healthz
# {"ok": true}
```

### systemd Monitoring Configuration

Add health check to systemd service file:

```ini
[Service]
# ... previous configuration ...
WatchdogSec=60
```

### Prometheus Monitoring (Optional)

If using Prometheus, monitor `/healthz` endpoint via blackbox-exporter.

## Backup Strategy

teamEvolver persistent data stored at following locations:

| Data | Path | Backup Frequency |
|------|------|-----------------|
| Configuration file | `~/.teamEvolver/config.yaml` | After each change |
| PID and runtime state | `~/.teamEvolver/teamEvolver.pid` | No backup needed |
| Logs | `~/.teamEvolver/teamEvolver.log` | No long-term backup needed |
| Prompt overrides | `~/.teamEvolver/prompt_overrides.json` | Daily |
| Stage settings | `~/.teamEvolver/stage_settings.json` | Daily |
| Local skill library | `~/.hermes/skills/` or configured `skills.dir` | Daily |
| Session storage | OpenViking cloud (if sharing used) | Cloud responsibility |
| OpenViking data | OpenViking data directory (local mode) | Daily |
| Console sessions | `~/.teamEvolver/console_sessions.json` | No backup needed |
| User registry | `~/.teamEvolver/users.json` | Daily |
| Evolution history | `evolve_history.jsonl` | Daily |
| Validation storage | ValidationStore internal data | Daily |

### Local OpenViking Backup

If using `viking_deployment: local` mode, back up OpenViking data directory (typically openviking-server data path).

### Automatic Backup Script Example

Create `/usr/local/bin/teamevolver-backup.sh`:

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/var/backups/teamevolver"
DATE=$(date +%Y%m%d_%H%M%S)
TEAMEVOLVER_HOME="/home/teamevolver/.teamEvolver"
SKILLS_DIR="/home/teamevolver/.hermes/skills"

mkdir -p "$BACKUP_DIR"

# Backup config and state
tar -czf "$BACKUP_DIR/teamevolver-config-$DATE.tar.gz" \
    -C "$TEAMEVOLVER_HOME" \
    config.yaml prompt_overrides.json stage_settings.json users.json console_sessions.json

# Backup skills library
if [ -d "$SKILLS_DIR" ]; then
    tar -czf "$BACKUP_DIR/teamevolver-skills-$DATE.tar.gz" -C "$(dirname $SKILLS_DIR)" skills
fi

# Keep last 30 days of backups
find "$BACKUP_DIR" -name "teamevolver-*.tar.gz" -mtime +30 -delete

echo "Backup completed: $BACKUP_DIR/teamevolver-config-$DATE.tar.gz"
```

Add to crontab:

```bash
# Daily backup at 3 AM
0 3 * * * /usr/local/bin/teamevolver-backup.sh >> /var/log/teamevolver-backup.log 2>&1
```

## Log Management

### Log Locations

| Runtime Mode | Log Location |
|--------------|--------------|
| Foreground mode | stdout/stderr |
| Daemon mode | `~/.teamEvolver/teamEvolver.log` |
| systemd mode | journald (`journalctl -u teamevolver`) |
| DreamCycle | Output with main service logs |
| Skill Miner | `teamEvolver/skillminer/logs/` |

### logrotate Configuration

Create `/etc/logrotate.d/teamevolver`:

```
/home/teamevolver/.teamEvolver/teamEvolver.log {
    daily
    rotate 14
    compress
    delaycompress
    missingok
    notifempty
    create 0640 teamevolver teamevolver
    postrotate
        systemctl kill -s HUP teamevolver.service 2>/dev/null || true
    endscript
}
```

## Security Hardening

### 1. Network Binding

Production must set `service.host` to `127.0.0.1`, exposing service only via Nginx reverse proxy:

```bash
teamEvolver config service.host 127.0.0.1
```

### 2. API Key Configuration

Set strong random keys to protect ingest endpoints:

```bash
# Generate random keys
INGEST_KEY=$(openssl rand -hex 32)
PROXY_KEY=$(openssl rand -hex 32)

# Inject via environment variables (recommended, do not write to config.yaml)
# Set in systemd service file:
# Environment=EVOLVE_INGEST_API_KEY=$INGEST_KEY
# Environment=TEAMEVOLVER_PROXY_API_KEY=$PROXY_KEY
```

### 3. API Key Rotation

Rotate API keys periodically:

1. Generate new keys
2. Update in systemd environment variables
3. Notify all Agent endpoints to update keys
4. Rolling restart service: `sudo systemctl restart teamevolver`
5. After verifying all Agents connect normally, retire old keys

### 4. TLS Configuration

Always use HTTPS; obtain free certificates via Let's Encrypt:

```bash
sudo certbot --nginx -d teamevolver.example.com
```

Auto-renewal configured automatically when certbot installs systemd timer.

### 5. Reverse Proxy Authentication

For internal deployments, additional Basic Auth or OAuth2 proxy can be enabled at Nginx layer:

```bash
# Create htpasswd file
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-teamevolver admin
```

### 6. Filesystem Permissions

```bash
# Restrict config directory permissions
chmod 700 /home/teamevolver/.teamEvolver
chmod 600 /home/teamevolver/.teamEvolver/config.yaml

# Skills directory permissions
chmod 700 /home/teamevolver/.hermes/skills
```

## Resource Requirements

Adjust resource configuration based on team size and usage:

| Scale | Sessions/day | CPU | RAM | Disk | Evolution Interval |
|-------|--------------|-----|-----|------|-------------------|
| Small (<10 people) | <100 | 2 cores | 4GB | 20GB | 600s (default) |
| Medium (10-50 people) | 100-1000 | 4 cores | 8GB | 50GB | 300-600s |
| Large (50+ people) | >1000 | 8 cores | 16GB | 100GB+ | 120-300s |

Notes:

- LLM calls are main time-consuming component; CPU and memory primarily used for evidence processing and replay validation
- True Replay mode requires additional workspace isolation resources
- Enabling DreamCycle increases LLM call volume during night windows
- For high session volumes, appropriately increase `evolve.interval_seconds` to reduce API costs

## Upgrade Procedure

### Version Upgrade Steps

1. Backup config and data:

```bash
/usr/local/bin/teamevolver-backup.sh
```

2. Check current version and changelog:

```bash
teamEvolver --version
```

3. Stop service:

```bash
sudo systemctl stop teamevolver
```

4. Upgrade package:

```bash
sudo -u teamevolver pip install --upgrade teamEvolver
```

5. Check configuration migration:

After startup check whether configuration needs updating; new versions may introduce new configuration sections.

6. Start service:

```bash
sudo systemctl start teamevolver
```

7. Verify health:

```bash
curl -s http://127.0.0.1:52010/healthz
sudo systemctl status teamevolver
```

8. Confirm functionality via console: skill list, session ingestion, evolution pipeline.

### Rollback

If upgrade issues occur:

1. Stop service
2. Install specified old version: `pip install teamEvolver==x.y.z`
3. Restore config from backup (if needed)
4. Restart service

## Multi-Instance Deployment (Advanced)

teamEvolver currently designed for single-instance operation. For high availability, recommend:

1. Active-standby mode: Use Keepalived or similar tools for VIP failover
2. Shared storage: Place `~/.teamEvolver` and skills directory on shared storage
3. Note: Do not run multiple instances pointing to same OpenViking personal space simultaneously; may cause race conditions
