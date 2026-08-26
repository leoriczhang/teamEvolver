# 生产部署指南

本指南介绍如何在生产环境中部署和运维 teamEvolver 服务。

## 前置条件

- Python 3.11 或更高版本
- 可用的 LLM API 端点（火山方舟、OpenAI 兼容接口等）
- 若使用技能云同步，需准备 OpenViking 凭证
- Linux 服务器（推荐 Ubuntu 22.04 / Debian 12）
- 至少 2 核 CPU、4GB 内存

## 运行模式

teamEvolver 支持两种进程管理方式：

### 前台模式（用于开发调试）

```bash
teamEvolver start
```

前台运行时日志直接输出到终端，按 `Ctrl+C` 停止服务。适用于开发和调试。

### Daemon 模式（用于单机部署）

```bash
teamEvolver start --daemon
```

Daemon 模式会将服务进程放入后台运行，日志写入 `~/.teamEvolver/teamEvolver.log`，PID 文件位于 `~/.teamEvolver/teamEvolver.pid`。

```bash
# 查看状态
teamEvolver status

# 停止服务
teamEvolver stop
```

### Docker Compose 模式

仓库根目录的 `compose.yaml` 会构建前端、安装完整 Python 依赖并捆绑 OpenViking CLI。它不会在同一容器中启动 OpenViking Server，仍需连接独立的云端或自建实例。

```bash
docker compose up -d --build
docker compose ps
docker compose logs -f teamevolver
```

可通过 `TEAMEVOLVER_PORT` 修改宿主机端口，通过 `TEAMEVOLVER_IMAGE` 指定版本化镜像。配置和 SkillMiner 产物挂载到 `runtime/`。连接宿主机或其他机器上的自建 OpenViking 时，`sharing.viking_endpoint` 必须使用容器可访问的地址；容器内的 `localhost:1933` 指向容器自身。

停止服务但保留数据：

```bash
docker compose down
```

### systemd 模式（推荐用于生产环境）

对于生产环境，建议使用 systemd 管理进程，可以实现开机自启、自动重启、日志聚合等能力。

## systemd 服务配置

创建 systemd 服务文件 `/etc/systemd/system/teamevolver.service`：

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

# 安全加固
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=read-only
ReadWritePaths=/home/teamevolver/.teamEvolver
ReadWritePaths=/home/teamevolver/.hermes

# 资源限制
LimitNOFILE=65536
MemoryMax=8G
CPUQuota=200%

[Install]
WantedBy=multi-user.target
```

创建专用用户并启动服务：

```bash
# 创建系统用户
sudo useradd -r -m -s /bin/bash teamevolver

# 安装 teamEvolver 到该用户环境
sudo -u teamevolver pip install teamEvolver

# 初始化配置
sudo -u teamevolver teamEvolver config llm.api_key "your-llm-key"
sudo -u teamevolver teamEvolver config service.host "127.0.0.1"

# 启用并启动服务
sudo systemctl daemon-reload
sudo systemctl enable teamevolver
sudo systemctl start teamevolver

# 查看状态
sudo systemctl status teamevolver

# 查看日志
sudo journalctl -u teamevolver -f
```

## Nginx 反向代理配置

生产环境中应使用 Nginx 作为反向代理，提供 TLS 终止、访问控制、请求限流等能力。

创建 Nginx 配置文件 `/etc/nginx/sites-available/teamevolver`：

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

    # 访问日志
    access_log /var/log/nginx/teamevolver.access.log;
    error_log /var/log/nginx/teamevolver.error.log;

    # 控制台静态资源
    location / {
        proxy_pass http://teamevolver_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;
    }

    # Agent 接入端点 - 建议启用 API Key 认证
    location /ingest_session {
        limit_req zone=ingest burst=20 nodelay;
        proxy_pass http://teamevolver_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;

        # 可选：Nginx 层 Basic Auth（配合 EVOLVE_INGEST_API_KEY 双重保护）
        # auth_basic "teamEvolver Ingest";
        # auth_basic_user_file /etc/nginx/.htpasswd-teamevolver;
    }

    # Langfuse 拉取端点
    location /langfuse/ {
        proxy_pass http://teamevolver_backend;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }

    # 健康检查端点（允许内网访问）
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

# 请求限流配置
limit_req_zone $binary_remote_addr zone=ingest:10m rate=10r/s;
```

启用配置：

```bash
sudo ln -s /etc/nginx/sites-available/teamevolver /etc/nginx/sites-enabled/
sudo nginx -t
sudo systemctl reload nginx
```

## 防火墙规则

使用 `ufw` 或 `iptables` 限制网络访问：

```bash
# 仅允许 SSH、HTTP、HTTPS
sudo ufw allow 22/tcp
sudo ufw allow 80/tcp
sudo ufw allow 443/tcp

# 拒绝外部直接访问 teamEvolver 端口
sudo ufw deny 52010/tcp

sudo ufw enable
```

确保 `service.host` 配置为 `"127.0.0.1"`，禁止服务直接监听公网地址。

## 健康检查与监控

### 健康检查端点

teamEvolver 提供以下 HTTP 端点用于监控：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/healthz` | GET | 健康检查，返回 200 `{"ok": true}` 表示服务正常 |
| `/status` | GET | 服务状态详情，包含队列、版本、配置信息 |

健康检查示例：

```bash
curl -s http://127.0.0.1:52010/healthz
# {"ok": true}
```

### systemd 监控配置

在 systemd 服务文件中添加健康检查：

```ini
[Service]
# ... 前述配置 ...
WatchdogSec=60
```

### Prometheus 监控（可选）

如果使用 Prometheus，可通过 blackbox-exporter 监控 `/healthz` 端点。

## 备份策略

teamEvolver 的持久化数据存储在以下位置：

| 数据 | 路径 | 备份频率 |
|------|------|---------|
| 配置文件 | `~/.teamEvolver/config.yaml` | 每次变更后 |
| PID 和运行时状态 | `~/.teamEvolver/teamEvolver.pid` | 无需备份 |
| 日志 | `~/.teamEvolver/teamEvolver.log` | 无需长期备份 |
| Prompt 覆盖 | `~/.teamEvolver/prompt_overrides.json` | 每日 |
| 阶段设置 | `~/.teamEvolver/stage_settings.json` | 每日 |
| 本地技能库 | `~/.hermes/skills/` 或配置的 `skills.dir` | 每日 |
| 会话存储 | OpenViking 云端（若使用 sharing） | 云端负责 |
| OpenViking 数据 | OpenViking 数据目录（local 模式） | 每日 |
| 控制台会话 | `~/.teamEvolver/console_sessions.json` | 无需备份 |
| 用户注册表 | `~/.teamEvolver/users.json` | 每日 |
| 进化历史 | `evolve_history.jsonl` | 每日 |
| 校验存储 | ValidationStore 内部数据 | 每日 |

### 本地 OpenViking 备份

若使用 `viking_deployment: local` 模式，需备份 OpenViking 数据目录（通常位于 openviking-server 的数据路径）。

### 自动备份脚本示例

创建 `/usr/local/bin/teamevolver-backup.sh`：

```bash
#!/bin/bash
set -euo pipefail

BACKUP_DIR="/var/backups/teamevolver"
DATE=$(date +%Y%m%d_%H%M%S)
TEAMEVOLVER_HOME="/home/teamevolver/.teamEvolver"
SKILLS_DIR="/home/teamevolver/.hermes/skills"

mkdir -p "$BACKUP_DIR"

# 备份配置和状态
tar -czf "$BACKUP_DIR/teamevolver-config-$DATE.tar.gz" \
    -C "$TEAMEVOLVER_HOME" \
    config.yaml prompt_overrides.json stage_settings.json users.json console_sessions.json

# 备份技能库
if [ -d "$SKILLS_DIR" ]; then
    tar -czf "$BACKUP_DIR/teamevolver-skills-$DATE.tar.gz" -C "$(dirname $SKILLS_DIR)" skills
fi

# 保留最近 30 天的备份
find "$BACKUP_DIR" -name "teamevolver-*.tar.gz" -mtime +30 -delete

echo "Backup completed: $BACKUP_DIR/teamevolver-config-$DATE.tar.gz"
```

添加到 crontab：

```bash
# 每天凌晨 3 点执行备份
0 3 * * * /usr/local/bin/teamevolver-backup.sh >> /var/log/teamevolver-backup.log 2>&1
```

## 日志管理

### 日志位置

| 运行模式 | 日志位置 |
|---------|---------|
| 前台模式 | stdout/stderr |
| Daemon 模式 | `~/.teamEvolver/teamEvolver.log` |
| systemd 模式 | journald（`journalctl -u teamevolver`） |
| DreamCycle | 随主服务日志输出 |
| Skill Miner | `teamEvolver/skillminer/logs/` |

### logrotate 配置

创建 `/etc/logrotate.d/teamevolver`：

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

## 安全加固

### 1. 网络绑定

生产环境必须将 `service.host` 设置为 `127.0.0.1`，仅通过 Nginx 反向代理暴露服务：

```bash
teamEvolver config service.host 127.0.0.1
```

### 2. API Key 配置

设置强随机密钥保护接入端点：

```bash
# 生成随机密钥
INGEST_KEY=$(openssl rand -hex 32)
PROXY_KEY=$(openssl rand -hex 32)

# 通过环境变量注入（推荐，不要写入 config.yaml）
# 在 systemd 服务文件中设置：
# Environment=EVOLVE_INGEST_API_KEY=$INGEST_KEY
# Environment=TEAMEVOLVER_PROXY_API_KEY=$PROXY_KEY
```

### 3. API Key 轮换

定期轮换 API Key：

1. 生成新密钥
2. 在 systemd 环境变量中更新
3. 通知所有 Agent 端更新密钥
4. 滚动重启服务：`sudo systemctl restart teamevolver`
5. 验证所有 Agent 正常接入后，废弃旧密钥

### 4. TLS 配置

始终使用 HTTPS，通过 Let's Encrypt 获取免费证书：

```bash
sudo certbot --nginx -d teamevolver.example.com
```

配置自动续期：certbot 安装时会自动添加 systemd timer。

### 5. 反向代理认证

对于内网部署，可在 Nginx 层额外启用 Basic Auth 或 OAuth2 代理：

```bash
# 创建 htpasswd 文件
sudo apt install apache2-utils
sudo htpasswd -c /etc/nginx/.htpasswd-teamevolver admin
```

### 6. 文件系统权限

```bash
# 限制配置目录权限
chmod 700 /home/teamevolver/.teamEvolver
chmod 600 /home/teamevolver/.teamEvolver/config.yaml

# 技能目录权限
chmod 700 /home/teamevolver/.hermes/skills
```

## 资源需求

根据团队规模和使用量调整资源配置：

| 规模 | 会话量/天 | CPU | 内存 | 磁盘 | 进化间隔 |
|------|----------|-----|------|------|---------|
| 小型（<10 人） | <100 | 2 核 | 4GB | 20GB | 600s（默认） |
| 中型（10-50 人） | 100-1000 | 4 核 | 8GB | 50GB | 300-600s |
| 大型（50+ 人） | >1000 | 8 核 | 16GB | 100GB+ | 120-300s |

注意事项：

- LLM 调用是主要耗时环节，CPU 和内存主要用于证据处理和回放校验
- True Replay 模式需要额外的工作区隔离资源
- 启用 DreamCycle 后会在夜间窗口增加 LLM 调用量
- 会话量较大时，适当增大 `evolve.interval_seconds` 以降低 API 成本

## 升级 procedure

### 版本升级步骤

1. 备份配置和数据：

```bash
/usr/local/bin/teamevolver-backup.sh
```

2. 查看当前版本和更新日志：

```bash
teamEvolver --version
```

3. 停止服务：

```bash
sudo systemctl stop teamevolver
```

4. 升级包：

```bash
sudo -u teamevolver pip install --upgrade teamEvolver
```

5. 检查配置迁移：

启动后检查配置是否需要更新，新版本可能引入新的配置节。

6. 启动服务：

```bash
sudo systemctl start teamevolver
```

7. 验证健康状态：

```bash
curl -s http://127.0.0.1:52010/healthz
sudo systemctl status teamevolver
```

8. 通过控制台确认功能正常：技能列表、会话摄入、进化流水线。

### 回滚

若升级出现问题：

1. 停止服务
2. 安装指定旧版本：`pip install teamEvolver==x.y.z`
3. 从备份恢复配置（如需要）
4. 重启服务

## 多实例部署（高级）

teamEvolver 目前设计为单实例运行。如需高可用，建议：

1. 主备模式：使用 Keepalived 或类似工具做 VIP 切换
2. 共享存储：将 `~/.teamEvolver` 和技能目录放在共享存储上
3. 注意：不要同时运行多个实例指向同一个 OpenViking 个人空间，可能导致竞态条件
