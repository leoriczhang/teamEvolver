# 服务日志状态 API

`GET /api/logging/status` 是控制台管理员只读接口，复用 TE 现有认证，普通用户无权访问。返回当前服务进程状态，无日志下载、编辑或任意文件读取接口。

主要字段：

| 字段 | 含义 |
| --- | --- |
| path、config_file、env_file | 实际日志路径、YAML 与环境文件路径 |
| sources | 各日志参数来源：cli、process_environment、env_file 路径、yaml 或 default |
| level、file_enabled、console_enabled | 最终生效级别及输出开关 |
| max_file_mb、retention_days、max_total_mb | 轮转和清理预算 |
| file_state | starting、active、disabled、degraded；未初始化为 not_configured |
| last_write_error | 最近写盘错误，经过脱敏；恢复后保留作为历史诊断 |
| dropped_count | 队列溢出或关闭超时未处理条数 |
| file_missed_count | 文件故障期间未成功落盘条数 |
| queue_capacity、queue_depth | 有界队列容量及当前积压 |
| timezone | 进程当前本地时区 |

所有 HTTP 响应回传经校验或重新生成的 `X-Request-ID`。排障说明与配置示例见 [服务日志与 Ontology 排障](../guides/15-service-logging.md)。
