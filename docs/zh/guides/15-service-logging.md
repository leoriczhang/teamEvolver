# 服务日志与 Ontology 排障

## 日志在哪里

TE 以前以前台方式运行时只向标准错误流输出；旧 daemon 直接追加文件且不轮转。本版本前台、容器和 daemon 都由 TE 子进程的统一日志组件写文件，同时保留平台采集的标准错误流。

- 本地默认：`~/.teamEvolver/teamEvolver.log`。
- SIT／PRD 配置模板：`/app/deploy/logs/teamEvolver.log`。
- 多实例：开启 `TEAMEVOLVER_MULTI_REPLICA=1` 后，在目录下按 `TEAMEVOLVER_INSTANCE_ID` 隔离（未设时使用主机名加 PID），避免文件竞争。稳定且唯一的实例标识允许重启后续写；文件锁阻止相同目录的第二个进程同时写入，冲突者降级为 stderr。容量与保留期按实例目录计算；自动生成 PID 目录时，已停止实例的目录由部署平台生命周期策略清理。
- 管理员在「运行状态 → 服务日志」或 `GET /api/logging/status` 查看**当前进程**实际路径和文件状态。负载均衡下每次请求可能命中不同实例。

`ontology.state_dir` 保存解析、抽取和候选制品，并非日志目录。启用 Ontology 时会尝试创建、检查目录；不可写会记录 `ontology.workspace_unwritable`，后续需要写盘的构建仍可能失败。日志目录独立创建；日志写入失败不会阻止服务继续提供功能。

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

| YAML 字段 | 环境变量 | 默认值 |
| --- | --- | --- |
| level | TEAMEVOLVER_LOG_LEVEL | INFO |
| directory | TEAMEVOLVER_LOG_DIR | ~/.teamEvolver |
| file_enabled | TEAMEVOLVER_LOG_FILE_ENABLED | true |
| console_enabled | TEAMEVOLVER_LOG_CONSOLE_ENABLED | true |
| max_file_mb | TEAMEVOLVER_LOG_MAX_FILE_MB | 100 |
| retention_days | TEAMEVOLVER_LOG_RETENTION_DAYS | 14 |
| max_total_mb | TEAMEVOLVER_LOG_MAX_TOTAL_MB | 2048 |

环境变量布尔值支持 `1/0`、`true/false`、`yes/no`、`on/off`。容量和天数必须为正整数。CLI `--log-file /path/custom.log` 优先级最高，并显式启用文件输出；支持前台和 `--daemon`。其余优先级为进程环境、加载的环境文件、YAML、默认值。配置为**服务级**，租户覆盖无效；修改后重启，单独更新仓库文件不会刷新运行实例。

启动事件 `logging.configured` 记录配置文件、环境文件、最终日志路径和各字段来源；`startup.configuration_loaded` 区分 Ontology YAML 值与环境覆盖值，`ontology.configuration` 显示实际开关。避免输出整份 YAML 或 `.env`。

## 轮转与降级

文件按进程本地日期及大小（MiB）轮转，归档为 `teamEvolver.log.YYYY-MM-DD.000001`，重启继续追加，归档序号不覆盖旧文件。使用自定义 `--log-file` 时归档使用对应文件名前缀。每天第一次写入触发跨日分卷。

启动、轮转和每分钟检查保留期与总容量，优先清理最旧归档。只删除匹配本组件归档格式的普通文件，不删除活动文件、任务制品、其他文件或符号链接。活动文件可能使总容量暂时超过预算；应保持 `max_total_mb >= max_file_mb` 并预留单条日志空间。

日志队列最多 8192 条，由单独线程写盘。队列满会计数丢弃，ERROR 及以上同时紧急输出 stderr；退出最多等待约 3 秒排空，未完成部分计数。目录不可写、磁盘满或轮转失败时，文件状态变为 `degraded`，每分钟重试恢复，故障期间日志输出 stderr；即使 `console_enabled=false`，故障告警仍保留。`file_missed_count` 是未成功落盘的记录数，**并不代表平台 stderr 也丢失了**。

`logging.file_degraded`、`logging.health`、`logging.file_recovered` 可在平台日志查看。文件系统永久阻塞时，写入线程可能仍被内核阻塞；有界队列保护请求路径，但无法保证落盘。

## 诊断字段与排障顺序

每条包含带时区时间、级别、logger、PID、事件及结构化字段。HTTP 使用经过校验的 `X-Request-ID`，在响应中回传；线程池继承当前关联上下文，后台构建通过任务 ID 和执行代次关联。成功 GET 轮询为 DEBUG；失败和关键写操作保留 INFO／WARNING／ERROR。

1. 找 `logging.configured`，确认正在使用预期 YAML、环境文件和目录。
2. 找 `ontology.configuration`：`enabled=false` 意味着没有注册本体路由，不是 OV 权限故障。
3. 找 `ontology.workspace`、`ontology.store_ready`、`ontology.worker_started`：分别确认工作目录、PG schema 初始化及 worker 状态。
4. 用请求 ID 或 `job_id` 关联 `http.completed`、`ontology.ov_response`、`ontology.failure`、`ontology.stage_started/completed`。
5. 发布超时后关注 `ontology.reconciling` 和 `commit` 阶段；只有 OV 回执确认后才是已发布，不要仅凭抽取完成推断发布成功。

| 事件／代码 | 来源与含义 |
| --- | --- |
| http.completed / TE_ROUTE_NOT_REGISTERED | TE 未匹配路由；路由字段为 `<unmatched>`，不记录任意 URL 正文 |
| ontology.failure / OV_NOT_FOUND | OV 返回普通 404，检查其扩展路由与部署版本 |
| ontology.failure / ONTOLOGY_DISABLED | OV 返回租户本体未启用，检查该 Account 的授权 |
| ontology.failure / FORBIDDEN | OV 拒绝当前主体权限 |
| ontology.identity_mismatch | OV 返回主体与 TE 服务端映射不一致 |
| ontology.failure / OV_TIMEOUT、OV_NETWORK_ERROR | TE 到 OV 超时或连接故障 |
| ontology.execution_failed | 构建失败；记录重试决定、任务及执行代次 |
| ontology.late_result_fenced | 取消或新执行代次使旧结果失效 |
| ontology.model_response / model_failed | 模型名、耗时、调用序号、状态及用量／失败类别 |

重复故障按主体和阶段限频汇总；首个故障、错误类别变化及恢复立即记录。原始 Prompt、来源正文、模型响应正文、审批意见、凭证和 Cookie 不属于日志数据。异常栈保留文件、行号和异常类型，省略可能包含业务数据的异常详情与源代码行。SkillMiner 输出持续限长读取，只转为运行状态、错误标记等元数据；非结构化正文省略。

这项改动提供诊断证据，**不改变本体授权、PG 表结构或功能开关，也不代表既有 Not Found 已解决**。SIT 验收仍须在实际容器确认挂载路径、运行账号权限与重启后的日志文件。

## 授权保存显示 `[object Object]`

2026-09-21 在 SIT 复现：Ontology 页面将 JSON 字符串以 `text/plain;charset=UTF-8` 提交，TE 的 `PUT /te/enterprise/v1/access` 返回 422，详情为 `body: Input should be a valid dictionary`。旧客户端直接将校验错误数组传入 Error，导致显示 `[object Object]`；授权没有写入，随后的 capabilities 查询持续返回 403 `ONTOLOGY_DISABLED`。

修复为所有 Ontology JSON POST／PUT 显式声明 `Content-Type: application/json`；客户端只展示校验位置和消息，不展示响应中的 `input` 或 `ctx`。授权页显示 OV 返回的账户、用户及生效状态。应分别检查 PUT 保存回执与后续 GET 查询结果；仅看到 GET 403 不能证明保存请求已执行。

本次通过 TE 授权接口为 SIT 的 `product_agent/team` 保存运营权限，收到 200 回执，随后 capabilities 返回 200。服务端授权立即生效；前端请求头与错误展示的代码修复需要更新前端版本后生效。
