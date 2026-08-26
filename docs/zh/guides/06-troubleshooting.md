# 故障排查指南

本指南汇总 teamEvolver 常见问题的排查思路和解决方案。

## 日志位置

排查问题时首先查看日志：

| 运行模式 | 日志位置 |
|---------|---------|
| 前台模式 | stdout/stderr（终端输出） |
| Daemon 模式 | `~/.teamEvolver/teamEvolver.log` |
| systemd 模式 | `journalctl -u teamevolver -f` |
| Skill Miner | `teamEvolver/skillminer/logs/` |
| Nginx 反向代理 | `/var/log/nginx/teamevolver.access.log` 和 `/var/log/nginx/teamevolver.error.log` |

查看 Daemon 日志尾部：

```bash
tail -f ~/.teamEvolver/teamEvolver.log
```

查看最近 100 行 systemd 日志：

```bash
journalctl -u teamevolver -n 100 --no-pager
```

## 服务启动问题

### 问题：服务无法启动

**症状**：执行 `teamEvolver start` 后报错或立即退出。

**排查步骤**：

1. 检查配置文件是否存在：
   ```bash
   ls -la ~/.teamEvolver/config.yaml
   ```
   如果不存在，先初始化配置：
   ```bash
   teamEvolver config llm.api_key "your-key"
   ```

2. 检查端口是否被占用：
   ```bash
   netstat -tlnp | grep 52010
   # 或
   lsof -i :52010
   ```
   如果端口被占用，可以：
   - 停止占用端口的进程
   - 或使用其他端口启动：`teamEvolver start --port 52011`
   - 或修改持久化配置：`teamEvolver config service.port 52011`

3. 前台启动查看详细错误：
   ```bash
   teamEvolver start
   ```
   前台模式下错误会直接输出到终端，便于定位。

4. 检查 LLM API 配置：
   - 确认 `llm.api_key` 不为空
   - 确认 `llm.api_base` 可访问：
     ```bash
     curl -s -o /dev/null -w "%{http_code}" https://ark.cn-beijing.volces.com/api/v3/models
     ```

### 问题：Daemon 启动超时

**症状**：`teamEvolver start --daemon` 报错 "did not become healthy in time"。

**排查步骤**：

1. 查看日志文件：
   ```bash
   cat ~/.teamEvolver/teamEvolver.log
   ```

2. 增加超时时间：
   ```bash
   TEAMEVOLVER_DAEMON_READY_TIMEOUT_S=30 teamEvolver start --daemon
   ```

3. 检查端口是否可绑定：如果 `service.host` 配置为特定网卡地址但该网卡不存在，会导致绑定失败。

### 问题：PID 文件存在但进程已死

**症状**：`teamEvolver status` 显示 "not running (stale PID file)"。

这通常是进程异常崩溃（如 OOM、kill -9）导致未清理 PID 文件。`status` 命令会自动清理僵尸 PID 文件，之后可以重新启动。

## OpenViking 连接问题

### 问题：技能云同步失败

**症状**：`teamEvolver skills pull` 或 `push` 报连接错误。

**排查步骤**：

1. 检查共享是否启用：
   ```bash
   teamEvolver config sharing.enabled
   ```

2. 检查部署模式配置：
   ```bash
   teamEvolver config sharing.viking_deployment
   ```
   值应为 `cloud` 或 `local`。

3. 验证 API Key 配置：
   - `sharing.viking_personal_api_key`（个人空间）
   - `sharing.viking_team_api_key`（服务/admin Key，兼容字段名）
   - 或通用的 `sharing.viking_api_key`

4. 检查端点配置：
   ```bash
   teamEvolver config sharing.viking_endpoint
   ```
   如果 `viking_deployment` 是 `cloud`，端点应为火山引擎 OpenViking 地址；如果是 `local`，应为自托管 openviking-server 地址。

5. 测试网络连通性：
   ```bash
   curl -s -o /dev/null -w "%{http_code}" \
     -H "Authorization: Bearer your-key" \
     "https://your-viking-endpoint/api/v1/health"
   ```

6. local 模式确认 openviking-server 是否正常运行。

### 问题：拉取的技能不完整或缺失

1. 检查 `push_min_injections` 和 `push_min_effectiveness` 门槛：低质量的技能不会被推送到云端
2. 确认使用的 API Key 有对应空间的访问权限
3. 检查 `viking_root_prefix` 是否被意外修改（此值通常应为默认值，修改后会读取不同的命名空间）

## Agent 接入问题

### 问题：Agent 无法连接到 teamEvolver

**症状**：Agent 调用 `/ingest_session` 返回 401 Unauthorized。

**排查步骤**：

1. 检查是否设置了 `EVOLVE_INGEST_API_KEY` 环境变量：
   ```bash
   echo $EVOLVE_INGEST_API_KEY
   ```
   如果设置了，Agent 请求必须携带正确的 `Authorization: Bearer <key>` 头。

2. 如果不需要认证，确保 `EVOLVE_INGEST_API_KEY` 未设置（服务启动时未注入此环境变量）。

3. 检查 Agent 的请求是否到达正确的地址和端口：
   ```bash
   # 从 Agent 所在机器测试连通性
   curl -v http://teamevolver-host:52010/healthz
   ```

4. 如果有 Nginx 反向代理，检查 Nginx 是否正确转发 Authorization 头。

### 问题：Agent 上报会话返回 403 Forbidden

**症状**：返回 "subject not mapped" 或 403 错误。

**原因**：Agent 注册后，其 subject（身份标识）需要映射到一个 teamEvolver 用户。

**解决方法**：

1. 访问控制台「用户与权限」页面
2. 找到对应 Agent，配置 subject 到用户的映射
3. 或在配置中确认 `sharing.viking_personal_user` 等用户标识正确

### 问题：Agent 上报会话无响应或超时

1. 检查服务是否正在运行：`teamEvolver status`
2. 检查请求体大小是否超过限制（默认 32MB）：
   - 过大的会话可能触发 413 错误
   - 可通过 `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` 调整
3. 检查是否有反向代理超时配置过短

## 会话与进化问题

### 问题：会话上报后未出现在控制台

**症状**：Agent 成功调用 `/ingest_session` 返回 200，但控制台队列中看不到。

**排查步骤**：

1. 检查会话是否被价值分类器标记为跳过：
   - Session Filter 阶段会判断会话是否为高价值
   - 闲聊、低质量、重复的会话会被跳过，不进入进化队列
   - 在控制台历史记录中查看是否标记为 "skipped"

2. 检查是否被判定为重复：
   - 相同 session_id 的重复上报会被去重
   - 使用 `--force`（Langfuse 拉取时）可以强制重处理

3. 确认 ingest 端点认证通过：如果配置了 `EVOLVE_INGEST_API_KEY`，请求必须携带正确的密钥，否则虽然可能返回 200 但被拒绝处理（取决于部署配置）。

4. 查看日志中的 value_judge 结果：
   ```bash
   grep -i "value_judge\|skipped\|queued" ~/.teamEvolver/teamEvolver.log | tail -50
   ```

### 问题：技能同步不工作

**症状**：进化产生了新技能版本，但没有同步到云端或本地其他 Agent。

**排查步骤**：

1. 检查技能同步 outbox：查看是否有未成功上传的变更事件
2. 确认 `sharing.enabled` 为 `true`
3. 检查 `skill_reload_mode` 配置：
   - `off`：不自动重载，需要手动触发
   - `poll`：按 `skill_reload_interval_seconds` 轮询（默认 30 秒）
   - `callback`：回调模式（需要 OpenViking 支持 webhook）
4. 手动触发同步：
   ```bash
   teamEvolver skills sync
   ```
5. 检查技能是否满足推送门槛（注入次数、有效率）：
   - 低于门槛的技能不会被 push
   - 使用 `--no-filter` 可以强制推送：`teamEvolver skills push --no-filter`
6. 检查 replay 端点配置：分布式校验时 Pi Agent URL 需正确配置

### 问题：进化没有自动运行

1. 检查进化间隔配置：
   ```bash
   teamEvolver config evolve.interval_seconds
   ```
   默认 600 秒（10 分钟）。首次启动后需要等待一个间隔才会开始第一轮进化。

2. 手动触发一次进化：调用 `/trigger` HTTP 端点（需要 API Key 如果配置了）。

3. 检查队列中是否有等待处理的会话：没有会话时进化循环可能跳过某些阶段。

4. 查看日志中是否有进化错误：
   ```bash
   grep -i "evolve\|error\|exception" ~/.teamEvolver/teamEvolver.log | tail -100
   ```

## 校验与回放问题

### 问题：True Replay 校验失败

**症状**：候选技能状态停留在 evaluating 或标记为 replay_error。

**排查步骤**：

1. 检查运行时隔离：True Replay 需要独立的工作区执行候选技能
   - 确认有足够的磁盘空间和临时目录权限
   - Pi Agent 分布式模式下确认 worker 节点可访问

2. 上下文 Hash 不匹配：
   - True Replay 依赖确定性的上下文重建
   - 如果基线回放和候选回放的初始状态不一致，会导致 Hash 不匹配
   - 检查是否有外部状态（如文件系统、网络调用）影响回放

3. 检查校验 Worker 日志：
   ```bash
   teamEvolver validation status
   teamEvolver validation run-once
   ```
   手动运行一次以查看详细错误。

4. 临时切换到轻量 replay 模式验证是否是 True Replay 特有问题：
   ```bash
   teamEvolver config validation.mode replay
   ```

### 问题：校验一直没有结果

1. 检查 `validation.max_concurrency` 是否过低（默认 1）
2. 检查 `validation.max_jobs_per_day` 是否达到上限（默认 5）
3. 确认 Worker 已启动：后台校验 Worker 随主服务启动
4. 检查 `validation.idle_after_seconds`：Worker 可能进入休眠，手动触发 run-once 唤醒

## 性能问题

### 问题：进化运行缓慢

**可能原因和解决方案**：

1. **LLM API 响应慢**：
   - 检查 `llm.api_base` 网络延迟
   - 考虑更换更近的 API 端点
   - 增大超时配置（如适用）

2. **进化间隔过短，队列堆积**：
   - 适当增大 `evolve.interval_seconds`（如 1200 或 1800）
   - 检查队列中会话数量，高吞吐量下考虑增加资源

3. **True Replay 开销大**：
   - True Replay 模式比 replay 模式慢很多，因为需要完整的工作区隔离和逐轮回放
   - 对性能敏感的场景可考虑使用 `replay` 模式（但精度较低）

4. **单次轮次处理会话过多**：
   - 检查 `evidence_max_entries`，过大的证据库会增加 LLM 输入长度
   - 调整 `dataset_test_cases` 和 `dataset_min_requirements`，减少测试用例数量

5. **Token 限制导致重试**：
   - 检查 `bundle_max_prompt_bytes`，如果 Prompt 太大可能导致 API 错误重试
   - 适当调整 `llm.max_tokens`

### 问题：内存占用过高

1. 检查是否队列中积累了大量待处理会话
2. True Replay 并行回放会占用较多内存，降低 `validation.max_concurrency`
3. 适当减小 `evidence_max_entries`
4. 系统层面：增加 swap 或升级内存

### 问题：日志文件过大

配置 logrotate 轮转日志，参考[部署指南](./02-deployment.md)中的日志管理章节。手动清理：

```bash
# 清空日志（不删除文件）
> ~/.teamEvolver/teamEvolver.log
```

## Langfuse 集成问题

### 问题：Langfuse 追踪数据没有出现在 Langfuse UI

1. 运行状态检查：
   ```bash
   teamEvolver langfuse status
   ```
   确认 `reachable: True`。

2. 确认 `tracing_enabled: true` 和 `enabled` 区分：
   - `enabled = true` 控制入站拉取
   - `tracing_enabled = true` 控制出站追踪
   两者独立，可单独开启。

3. 检查 public_key 和 secret_key 是否正确：
   - 必须是 Langfuse Project API Keys，不是 Account API Keys
   - Public Key 以 `pk-lf-` 开头，Secret Key 以 `sk-lf-` 开头

4. 检查 `tracing_sample_rate`：如果设为 0.1，只有约 10% 的调用会被追踪

5. 检查 `host` 是否可从服务器访问：
   - 自托管 Langfuse 确认 URL 和端口正确
   - cloud.langfuse.com 检查防火墙是否允许出站 HTTPS

6. 查看日志中是否有 `[Langfuse] tracing unavailable` 警告

### 问题：Langfuse 拉取找不到会话

1. 先使用 `list` 命令确认匹配数量：
   ```bash
   teamEvolver langfuse list --environment production
   ```

2. 检查过滤条件是否过严：
   - 默认过滤条件（`default_environment`、`default_tags` 等）是否符合你的 Langfuse 数据标签
   - 临时清除默认过滤：在配置中设置这些为空列表

3. Langfuse 标签是在 Trace 级别而非 Session 级别：如果你的 Agent 没有正确设置 trace tags，可能无法通过 tags/userId 过滤。

4. 时间范围问题：`from_timestamp`/`to_timestamp` 是否覆盖了会话时间

## Web 控制台问题

### 问题：无法访问控制台

1. 确认服务运行：`teamEvolver status`
2. 检查端口监听：`netstat -tlnp | grep 52010`
3. 如果有防火墙，确认端口开放（或通过 Nginx 访问）
4. 检查 `service.host` 是否绑定到正确地址：
   - `0.0.0.0` 监听所有网卡
   - `127.0.0.1` 仅本地访问（需 Nginx 代理）

### 问题：登录后立即退出

1. 检查 `~/.teamEvolver/console_sessions.json` 权限
2. 清除浏览器 cookie 后重试
3. 检查系统时间是否正确（会话 TTL 基于时间计算）

## 诊断工具

### teamEvolver doctor hermes

运行集成诊断，自动检查常见配置问题：

```bash
teamEvolver doctor hermes
```

输出包括检测到的 issues、notes 和建议的 next_steps。

### 健康检查端点

```bash
# 基本健康检查
curl http://127.0.0.1:52010/healthz

# 详细状态
curl http://127.0.0.1:52010/status
```

### 配置检查

查看当前所有生效配置：

```bash
teamEvolver config show
```

### 校验 Worker 状态

```bash
teamEvolver validation status
```

## 获取帮助

如果以上方法无法解决问题：

1. 收集相关日志片段（错误前后 50 行）
2. 记录版本号：`teamEvolver --version`（如适用）
3. 记录配置（脱敏 API Key 等敏感信息）
4. 描述复现步骤
5. 检查是否有相关的 GitHub Issue
