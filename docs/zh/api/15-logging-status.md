# 服务日志状态 API

`GET /api/logging/status` 返回当前进程的日志运行状态，只读，要求平台管理员（或 Root Key）。没有日志下载、编辑或任意文件读取接口。

代码入口：`teamEvolver/observability/api/__init__.py:create_router`、`teamEvolver/logging_runtime.py:logging_status`。

## 响应字段

| 字段 | 含义 |
| --- | --- |
| `path` | 实际日志文件路径 |
| `config_file`、`env_file` | 启动时使用的 YAML 与环境文件路径 |
| `sources` | 每个日志参数的来源：`cli`、`process_environment`、环境文件路径、`yaml` 或 `default` |
| `level`、`file_enabled`、`console_enabled` | 生效级别与输出开关 |
| `directory`、`max_file_mb`、`retention_days`、`max_total_mb` | 目录、轮转与清理预算 |
| `file_state` | `starting`、`active`、`disabled`、`degraded`；未初始化时为 `not_configured` |
| `last_write_error` | 最近一次写盘错误（已脱敏），恢复后保留 |
| `dropped_count` | 队列溢出或关闭超时未处理的条数 |
| `file_missed_count` | 文件故障期间未落盘的条数 |
| `queue_capacity`、`queue_depth` | 有界队列容量与当前积压 |
| `timezone` | 进程本地时区 |

## 日志参数

| 参数 | 环境变量 | 默认 |
| --- | --- | --- |
| `level` | `TEAMEVOLVER_LOG_LEVEL` | `INFO` |
| `console_enabled` | `TEAMEVOLVER_LOG_CONSOLE_ENABLED` | `true` |
| `file_enabled` | `TEAMEVOLVER_LOG_FILE_ENABLED` | `true` |
| `directory` | `TEAMEVOLVER_LOG_DIR` | `~/.teamEvolver` |
| `max_file_mb` | `TEAMEVOLVER_LOG_MAX_FILE_MB` | 100 |
| `retention_days` | `TEAMEVOLVER_LOG_RETENTION_DAYS` | 14 |
| `max_total_mb` | `TEAMEVOLVER_LOG_MAX_TOTAL_MB` | 2048 |

环境变量优先于 YAML `logging` 段。`teamEvolver start --log-file` 指定文件并强制开启文件输出。`TEAMEVOLVER_MULTI_REPLICA=1` 时日志写入 `<directory>/<instance>/teamEvolver.log`，实例名取 `TEAMEVOLVER_INSTANCE_ID`，未设置时为主机名加 PID。

## 请求关联

每个 HTTP 响应都带 `x-request-id` 与 `x-trace-id`：请求携带时原样回传，否则由服务端生成，并写入该请求的访问日志 `http.access`。

排障与配置示例见 [服务日志指南](../guides/15-service-logging.md)。
