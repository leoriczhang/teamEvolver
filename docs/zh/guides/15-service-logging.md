# 服务日志

服务日志由 `teamEvolver/logging_runtime.py` 统一管理。`api` 与 `worker` 进程在前台、后台和容器中都写入同一种格式的轮转文件，同时输出到标准错误流供平台采集。日志配置属于服务级启动配置，修改后重启生效。

## 日志位置

| 场景 | 路径 |
|------|------|
| 本地默认 | `~/.teamEvolver/teamEvolver.log` |
| 容器自定义日志目录示例 | `/app/deploy/logs/teamEvolver.log` |
| `--log-file PATH` | 指定路径，并强制启用文件输出 |
| 多副本 | `TEAMEVOLVER_MULTI_REPLICA=true` 时位于 `<directory>/<instance>/teamEvolver.log` |

多副本模式下 `<instance>` 取 `TEAMEVOLVER_INSTANCE_ID`；未设置时取主机名加 PID。同一路径的第二个进程无法获得文件锁，会降级为只写标准错误流。

平台管理员可在「运行状态 → 服务日志」或 `GET /api/logging/status` 查看当前进程的实际路径、文件状态、队列深度与丢弃计数。负载均衡下每次请求可能命中不同实例。

## 配置

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

| 字段 | 环境变量 | 默认 |
|------|----------|------|
| `level` | `TEAMEVOLVER_LOG_LEVEL` | INFO |
| `directory` | `TEAMEVOLVER_LOG_DIR` | `~/.teamEvolver` |
| `file_enabled` | `TEAMEVOLVER_LOG_FILE_ENABLED` | true |
| `console_enabled` | `TEAMEVOLVER_LOG_CONSOLE_ENABLED` | true |
| `max_file_mb` | `TEAMEVOLVER_LOG_MAX_FILE_MB` | 100 |
| `retention_days` | `TEAMEVOLVER_LOG_RETENTION_DAYS` | 14 |
| `max_total_mb` | `TEAMEVOLVER_LOG_MAX_TOTAL_MB` | 2048 |

优先级从高到低：`--log-file`、环境变量、YAML、默认值。布尔值接受 `1/0`、`true/false`、`yes/no`、`on/off`；容量与天数必须为正整数，非法值使启动失败。

启动时记录 `logging.configured` 事件，包含最终路径与每个字段的来源。

## 轮转与保留

- 按本地日期和文件大小轮转，归档名为 `teamEvolver.log.YYYY-MM-DD.000001`，序号递增不覆盖
- 启动、轮转时及每分钟检查一次保留期和总容量，优先删除最旧归档
- 只删除匹配归档命名的普通文件，不删除活动文件、符号链接或其他文件
- 活动文件可能使总量短暂超过 `max_total_mb`，应保证 `max_total_mb >= max_file_mb`

## 降级行为

日志通过最多 8192 条的有界队列由独立线程写盘：

- 队列满时丢弃并计入 `dropped_count`，ERROR 及以上级别同时直接写标准错误流
- 目录不可写、磁盘满或轮转失败时文件状态变为 `degraded`，期间日志写标准错误流，每分钟尝试恢复
- `file_missed_count` 统计未写入文件的记录数，这些记录仍已输出到标准错误流
- 进程退出时最多等待约 3 秒排空队列

降级与恢复分别记录 `logging.file_degraded`、`logging.file_recovered`，周期健康摘要为 `logging.health`。日志写入失败不影响服务处理请求。

## 日志格式

每行包含带时区的时间、级别、logger、PID、事件名和结构化字段。HTTP 请求使用经过校验的 `X-Request-ID`（缺失时生成），并在响应头回传；线程池任务继承当前请求上下文，Worker 任务以 `task_id` 关联。成功的 GET 轮询记为 DEBUG。

以下内容不写入日志：模型 Prompt 与响应正文、来源文档正文、审批意见、凭证、Cookie。匹配 `api_key`、`password`、`secret`、`authorization`、`cookie`、`token`、`root_key` 的字段与已注册的配置密钥会被脱敏。异常栈保留文件、行号和异常类型，省略异常消息中的业务数据。

## 排障顺序

1. 用 `logging.configured` 确认进程加载了预期的配置与目录。
2. 用请求 ID 或 `job_id` 关联 `http.completed` 与后续事件。
3. 未匹配路由的请求记为 `http.completed`，错误码 `TE_ROUTE_NOT_REGISTERED`，路由字段为 `<unmatched>`。

## 相关文档

- [配置参考](./01-configuration.md)
- [日志状态 API](../api/15-logging-status.md)
- [故障排查](./06-troubleshooting.md)
