# CLI 与 Daemon 参考

teamEvolver 提供命令行工具用于服务管理、配置管理、技能同步、诊断等操作。本文档详细介绍所有 CLI 命令和守护进程的运行机制。

CLI 入口定义在 `teamEvolver/cli/__init__.py`，可执行命令名为 `teamEvolver`。

## 全局说明

### 命令结构

```
teamEvolver <command> [subcommand] [options] [arguments]
```

查看所有可用命令：

```bash
teamEvolver --help
```

查看子命令帮助：

```bash
teamEvolver skills --help
teamEvolver langfuse pull --help
```

### 运行时文件位置

所有运行时状态文件存储在 `~/.teamEvolver/` 目录下：

| 文件 | 说明 |
|------|------|
| `config.yaml` | 主配置文件 |
| `teamEvolver.pid` | Daemon 模式的 PID 文件 |
| `teamEvolver.log` | Daemon 模式默认日志文件 |
| `prompt_overrides.json` | Prompt Studio 的 Prompt 覆盖配置 |
| `stage_settings.json` | 各阶段模型参数覆盖配置 |
| `console_sessions.json` | Web 控制台登录会话 |
| `users.json` | 控制台用户注册表 |
| `agent-protocol.env` | Agent 协议环境变量（可选，Daemon 启动时自动加载） |

## 服务管理命令

### teamEvolver start

启动 teamEvolver 服务。

```bash
teamEvolver start [OPTIONS]
```

选项：

| 选项 | 说明 |
|------|------|
| `--port INTEGER` | 覆盖本次启动的服务端口（不修改持久化配置） |
| `-d, --daemon` | 以守护进程（后台）模式运行 |
| `--log-file PATH` | Daemon 模式下指定日志文件路径，默认 `~/.teamEvolver/teamEvolver.log` |

示例：

```bash
# 前台启动（开发调试）
teamEvolver start

# 后台启动
teamEvolver start --daemon

# 后台启动并临时使用端口 30001
teamEvolver start --daemon --port 30001

# 后台启动并指定日志文件
teamEvolver start --daemon --log-file /var/log/teamevolver.log
```

启动行为：

1. 检查配置文件是否存在，不存在则提示先运行 `teamEvolver config`
2. Daemon 模式下：
   - 获取启动锁，防止重复启动
   - 创建子进程，子进程的 stdin/stdout/stderr 重定向到日志文件
   - 设置 `TEAMEVOLVER_RUNTIME_KIND=daemon` 和 `TEAMEVOLVER_RUNTIME_LOG_PATH` 环境变量
   - 等待最多 15 秒（可通过 `TEAMEVOLVER_DAEMON_READY_TIMEOUT_S` 环境变量调整）直到 `/healthz` 返回 ok
   - 启动失败自动清理子进程和 PID 文件
3. 前台模式下：直接运行 Launcher，直到收到 Ctrl+C 中断信号
4. 指定 `--port` 时：创建临时配置文件，退出时自动清理

### teamEvolver stop

停止运行中的 teamEvolver 守护进程。

```bash
teamEvolver stop
```

行为：

1. 读取 `~/.teamEvolver/teamEvolver.pid` 获取 PID
2. 向进程发送 SIGTERM 信号
3. 删除 PID 文件
4. 如果进程不存在（僵尸 PID 文件），仅清理 PID 文件并给出提示

注意：前台模式启动的服务无法通过此命令停止，请使用 Ctrl+C。

### teamEvolver status

检查 teamEvolver 服务运行状态。

```bash
teamEvolver status
```

可能的输出：

```
teamEvolver: not running                    # PID 文件不存在
teamEvolver: not running (stale PID file)   # 进程已死，清理僵尸 PID
teamEvolver: starting (PID=12345, service=:52010)  # 进程存在但 /healthz 未就绪
teamEvolver: running  (PID=12345, service=:52010) # 正常运行
```

状态检查通过 HTTP 请求 `http://127.0.0.1:<port>/healthz` 确认服务是否就绪。

## 配置管理命令

### teamEvolver config

读取或修改配置值。

```bash
teamEvolver config <key_or_action> [value]
```

用法：

```bash
# 查看所有配置（含配置文件路径）
teamEvolver config show

# 读取单个配置项（点分隔的嵌套键）
teamEvolver config <key>

# 设置配置项
teamEvolver config <key> <value>
```

示例：

```bash
# 查看完整配置
teamEvolver config show

# 读取 LLM API Key
teamEvolver config llm.api_key

# 设置端口
teamEvolver config service.port 52010

# 启用 Langfuse 追踪
teamEvolver config langfuse.tracing_enabled true

# 设置 DreamCycle 活跃窗口
teamEvolver config dreamcycle.active_start_hour 22
teamEvolver config dreamcycle.active_end_hour 6
```

注意：

- 字符串 `"true"`/`"false"` 自动转换为布尔值
- 数字字符串自动转换为整数或浮点数
- 设置的值会立即写入 `config.yaml`，但部分配置需要重启服务才能生效
- 支持任意深度的点分隔键，如 `evolve.dataset_min_requirements`

## 技能管理命令

`teamEvolver skills` 命令组用于管理本地技能与 OpenViking 云端的同步。

```bash
teamEvolver skills <subcommand> [options]
```

### teamEvolver skills push

将本地技能推送到云端共享存储。

```bash
teamEvolver skills push [--no-filter]
```

选项：

| 选项 | 说明 |
|------|------|
| `--no-filter` | 跳过质量门槛过滤，推送所有本地技能（默认只推送注入次数 >=5 且有效率 >=0.3 的技能） |

推送时会自动检查 `skills/skill_stats.json` 中的统计数据，不满足门槛的技能会被过滤掉以保证共享技能质量。

### teamEvolver skills pull

从云端拉取共享技能到本地。

```bash
teamEvolver skills pull
```

拉取结果会显示：downloaded（新下载）、skipped（无变化）、failed（失败）、deleted（本地被云端删除的技能）。拉取失败时会自动尝试从备份恢复。

### teamEvolver skills sync

双向同步：先 pull 再 push。

```bash
teamEvolver skills sync
```

相当于依次执行 `pull` 和 `push`。

### teamEvolver skills list-remote

列出云端可用的共享技能。

```bash
teamEvolver skills list-remote
```

显示每个技能的名称、分类、描述、上传者和上传时间。

### 技能同步前提条件

使用技能同步命令前必须正确配置 `sharing` 节：

```bash
teamEvolver config sharing.enabled true
teamEvolver config sharing.viking_deployment cloud
teamEvolver config sharing.viking_personal_api_key "your-vk-key"
```

## Langfuse 命令

`teamEvolver langfuse` 命令组用于 Langfuse 集成管理和会话拉取。

```bash
teamEvolver langfuse <subcommand> [options]
```

### teamEvolver langfuse status

检查 Langfuse 连接状态和配置。

```bash
teamEvolver langfuse status
```

输出信息包括：host、session_pull_enabled、tracing_enabled、tracing_environment、密钥是否存在、默认过滤条件、SDK 可用性、服务连通性、总会话数。

### teamEvolver langfuse list

列出匹配过滤条件的 Langfuse 会话（不执行 ingestion）。

```bash
teamEvolver langfuse list [FILTER_OPTIONS]
```

通用过滤选项（适用于 `list` 和 `pull`）：

| 选项 | 说明 |
|------|------|
| `-e, --environment ENV` | 按环境过滤（可重复指定多个） |
| `-u, --user-id ID` | 按 userId 过滤 |
| `--tag TAG` | 按标签过滤（可重复，需全部匹配） |
| `--release RELEASE` | 按 release 过滤 |
| `--version VERSION` | 按 version 过滤 |
| `--name NAME` | 按 trace name 过滤 |
| `--session-id ID` | 拉取指定单个会话 |
| `--from TIMESTAMP` | 只拉取此 ISO 8601 时间之后的会话 |
| `--to TIMESTAMP` | 只拉取此 ISO 8601 时间之前的会话 |
| `-m, --metadata KEY=VALUE` | 按 metadata 键值对过滤（可重复） |
| `--max-sessions N` | 限制返回会话数量 |

示例：

```bash
# 列出生产环境的会话
teamEvolver langfuse list -e production

# 列出指定标签的会话
teamEvolver langfuse list --tag agent --tag coding

# 列出特定用户最近的会话
teamEvolver langfuse list -u user-123 --from 2026-08-01T00:00:00Z
```

### teamEvolver langfuse pull

拉取匹配的 Langfuse 会话进入 teamEvolver 进化流水线。

```bash
teamEvolver langfuse pull [FILTER_OPTIONS] [OPTIONS]
```

额外选项：

| 选项 | 说明 |
|------|------|
| `--user-alias ALIAS` | 为拉取的会话设置 user_alias 归属标记 |
| `--force` | 强制重新处理内容未变化的会话 |
| `--defer-trigger` | 入队但不触发进化轮次 |
| `--in-process` | 本地直接处理（不依赖正在运行的 teamEvolver 服务） |

默认情况下，`pull` 通过 HTTP POST 到正在运行的 teamEvolver 服务的 `/langfuse/pull` 端点执行。使用 `--in-process` 可以在 CLI 进程内直接处理，适合服务未启动时的批量导入。

示例：

```bash
# 使用默认配置拉取
teamEvolver langfuse pull

# 拉取并标记来源
teamEvolver langfuse pull --user-alias "langfuse-import" -e production

# 本地批量导入
teamEvolver langfuse pull --in-process --max-sessions 200 --force
```

## 诊断命令

`teamEvolver diag` 相关命令组用于集成诊断和维护。

### teamEvolver doctor hermes

检查本地 Hermes 集成状态。

```bash
teamEvolver doctor hermes
```

诊断内容包括：

- 配置文件是否存在
- 模型配置是否符合预期
- Base URL 配置
- Provider 配置
- 代理匹配状态
- 技能目录是否存在及权限
- 遗留技能目录状态
- 最新备份信息
- 会话边界模式
- 发现的问题和修复建议

### teamEvolver restore hermes

从备份恢复 Hermes 配置。

```bash
teamEvolver restore hermes [--backup PATH]
```

| 选项 | 说明 |
|------|------|
| `--backup PATH` | 指定备份文件路径，默认使用最新的 Hermes 备份 |

teamEvolver 在修改 Hermes 配置前会自动创建备份，此命令可在配置异常时回滚。

### teamEvolver validation status

查看后台校验 Worker 配置和当前状态。

```bash
teamEvolver validation status
```

显示校验模式、并发设置、所需结果/审批数、Worker 是否空闲等信息。

### teamEvolver validation run-once

运行一次后台校验轮询迭代。

```bash
teamEvolver validation run-once [--force]
```

| 选项 | 说明 |
|------|------|
| `--force` | 即使 Worker 不处于 idle 状态也强制执行 |

默认情况下，校验 Worker 只在空闲超过 `validation.idle_after_seconds` 后才会执行轮询，此命令可以手动触发一次。

## Daemon 进程管理细节

### PID 文件机制

Daemon 启动成功后将子进程 PID 写入 `~/.teamEvolver/teamEvolver.pid`。`status` 和 `stop` 命令通过此文件查找进程。

如果进程异常退出未清理 PID 文件，`status` 命令会检测到进程不存在并自动清理僵尸 PID 文件。

### 启动锁

`start --daemon` 使用文件锁防止并发启动。如果有另一个 daemon 启动过程正在进行，会提示对应 PID 并退出。

### 健康检查等待

Daemon 启动后，父进程会轮询 `http://127.0.0.1:<port>/healthz` 端点，最多等待 15 秒。超时则判定启动失败，终止子进程并报错。可通过环境变量调整等待时间：

```bash
TEAMEVOLVER_DAEMON_READY_TIMEOUT_S=30 teamEvolver start --daemon
```

### 环境变量传递

Daemon 子进程继承当前 shell 的环境变量，额外设置：

- `TEAMEVOLVER_RUNTIME_KIND=daemon`
- `TEAMEVOLVER_RUNTIME_LOG_PATH=<log-path>`

此外，如果存在 `~/.teamEvolver/agent-protocol.env` 文件，会以 dotenv 方式加载其中的环境变量（仅作为默认值，不覆盖已存在的环境变量）。

### 日志处理

Daemon 模式下 stdout 和 stderr 都重定向到日志文件（append 模式）：

- 默认日志路径：`~/.teamEvolver/teamEvolver.log`
- 可通过 `--log-file` 自定义
- 父进程创建日志文件所在目录（如不存在）
- 前台模式下日志直接输出到终端

### 进程终止

- `teamEvolver stop` 发送 SIGTERM
- systemd 管理时由 systemd 发送 SIGTERM，超时后发送 SIGKILL
- Ctrl+C 触发前台模式的优雅关闭

## 环境变量参考

除了配置文件外，以下环境变量影响 CLI 和 Daemon 行为：

| 环境变量 | 说明 |
|----------|------|
| `TEAMEVOLVER_DAEMON_READY_TIMEOUT_S` | Daemon 启动健康检查超时秒数，默认 15 |
| `EVOLVE_INGEST_API_KEY` | `/ingest_session` 和 `/langfuse/pull` 端点的 Bearer Token 认证密钥 |
| `TEAMEVOLVER_PROXY_API_KEY` | 模型代理端点的 API Key |
| `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` | 会话上传请求体最大字节数，默认 32MB |
| `EVOLVE_HISTORY_PATH` | 进化历史 JSONL 文件路径 |
| `TEAMEVOLVER_PROMPT_OVERRIDES_PATH` | 自定义 Prompt 覆盖文件路径 |
| `TEAMEVOLVER_STAGE_SETTINGS_PATH` | 自定义阶段设置文件路径 |
| `ARK_API_KEY` | 火山方舟 API Key（Skill Miner 使用） |
| `SKILLMINER_LIFT_AUTO_DRAFT` | 设为 `0` 禁用自动生成 LIFT 草稿 |
| `LANGFUSE_*` | 各种 Langfuse 配置环境变量，见可观测性指南 |

## 退出码

| 退出码 | 说明 |
|--------|------|
| 0 | 成功 |
| 1 | 一般错误（配置缺失、命令执行失败等） |

常见错误情况：

- 配置文件不存在时执行 `start`：退出码 1，提示先运行 `teamEvolver config`
- 端口被占用时启动：Daemon 子进程异常退出，父进程报错并提示查看日志
- 共享未配置时执行 `skills push/pull/sync`：报错提示先配置 sharing
- Langfuse 未配置或密钥缺失时执行 `langfuse` 命令：报错提示配置
