# 成功经验同步接口

## GET /api/experience-sync/status

租户管理员只读接口，复用 TE 控制台登录和当前租户上下文。普通用户返回 403；不接受请求参数指定其他租户。同步器不增加 Agent 数据面协议。

```json
{
  "enabled": true,
  "tenant_id": "t_example",
  "target_directory": "viking://resources/agent_knowledge_workspace/input/proven_experiences",
  "account": "example",
  "last_scan": 1800000000.0,
  "last_full_scan": 1800000000.0,
  "counts": {"pending": 2, "synced": 10, "retry": 1},
  "index_pending": 1,
  "last_error": "OV_TIMEOUT"
}
```

时间为 Unix 秒，尚未扫描时为 null。`pending` 是待上传或待核对，`retry` 是失败后等待重试；只有正文已回读验证且 OV 的语义与向量处理均完成才计为 `synced`。计数来自当前目标在 PG 中保存的状态，读取时可能与并发后台任务存在短暂差异。`last_error` 是安全错误码，不包含远端错误正文。

关闭功能时返回 `enabled=false`、零计数，不读取历史状态；配置或存储故障通过 `last_error` 显示，不将未知状态解释为同步成功。首次扫描前的零计数也不代表目录没有历史经验。

接口不返回文档或 Session 正文、凭证或 DSN，不提供删除操作。配置与恢复方式见[运维指南](../guides/16-successful-experience-sync.md)。


## POST /api/experience-sync/trigger

当前租户管理员手动发起一轮完整核对，无请求体；租户由已认证控制台上下文解析，不允许通过请求体修改租户、OV 账户、目标路径或同步开关。页面入口：**经验库 → 成功经验同步到 OV → 立即同步到 OV**。普通用户不展示此入口，调用接口返回 403。

请求持久化后返回 **202 Accepted**，不等待 OV 上传：

```json
{
  "tenant_id": "t_example",
  "manual": {
    "request_id": "84499019-559f-4c7c-b1ce-7ee9a1ba4d85",
    "state": "queued",
    "requested_at": 1800000000.0
  }
}
```

查询状态响应新增 `manual`（尚未触发时为 null）。状态为 `queued`、`running` 或 `completed`，相应附带 `started_at` / `finished_at` Unix 秒时间；`scan_complete` 表示发现阶段结束。`completed` 仅代表这一轮核对结束，仍可能有失败、等待退避重试或被拒绝的源文档，须结合 `counts` 和 `last_error` 判断结果。刷新页面或重启服务后可继续查询。

完整核对依然分页执行，按正文摘要增量写入；已同步正文回读一致时不重复写入。不会因页面筛选条件只同步某个 Skill，也不会强制重试尚未到期的失败项。源端删除不会删除 OV 文件。请求复用持久化状态、PG 租户锁及全服务最多两个执行线程，不在请求线程上传；同一轮已排队/运行的请求在锁可用时返回同一 request_id。

| HTTP | 错误码/原因 | 处理 |
| --- | --- | --- |
| 403 | 非管理员、租户不可用 | 检查登录和当前租户权限 |
| 409 | `SYNC_DISABLED` | 在有效租户配置启用 `experience_sync.enabled` |
| 409 | `SYNC_ALREADY_RUNNING` | 当前后台执行持有锁，稍后查询状态或重试 |
| 429 | `SYNC_BUSY` | 触发受理并发已满（2）或本实例待唤醒租户已满（32） |
| 503 | `SYNC_NOT_RUNNING` / `SYNC_STOPPING` | 检查 PG 配置、服务生命周期及日志 |
| 503 | `PG_REQUIRED` / `SHARING_DISABLED` / `OV_IDENTITY_OR_KEY_MISSING` / `INVALID_SYNC_CONFIG` / `INVALID_SYNC_TARGET_DIRECTORY` / `INVALID_OV_ENDPOINT` | 修复相应配置 |
| 503 | `SYNC_STORAGE_FAILURE` | 检查状态存储；响应不暴露底层异常正文 |

只处理 `experience_library/{skill_slug}.json` 中的 `exemplary` 记录。经验库列表来自 Session 分析，未进入该 JSON 链路的条目不会因点击按钮被上传。


## POST /api/experience-sync/import

管理员触发当前租户**存量全量导入**，无请求体，沿用上节身份、开关、PG、sharing、持久化受理、并发及错误约定。202 返回 `tenant_id` 与 `manual`，其中 `operation="import_all"`；原 trigger 返回 `operation="sync"`。相同运行中的操作合并为同一请求，另一类操作正在进行时返回 409 `SYNC_OTHER_OPERATION_RUNNING`；执行中持锁时仍可能返回 409 `SYNC_ALREADY_RUNNING`。

导入范围为旧聚合 JSON、全部分页 Session 索引和已有归档/历史经验中的 `exemplary`，不受经验库页面筛选或 10,000 条限制。此接口不接收任意文件、账户或正文，不调用模型。原 trigger 仍只核对旧 JSON；导入不自动改变今后的 Session 增量接入策略。

`GET /api/experience-sync/status` 的 `manual.import` 返回：

```json
{
  "phase": "index",
  "until": "2026-09-22T08:00:00+00:00",
  "processed_sources": 12001,
  "eligible_records": 11001,
  "prepared_documents": 0,
  "rejected_sources": 1,
  "last_error": "IMPORT_SOURCE_TOO_LARGE"
}
```

阶段为 `objects` → `index` → `prepare` → `complete`；`complete` 表示导入文档已准备，上传与核对仍使用 `manual.state`、顶层 `counts` 和 `last_error` 判断。进度可能附带内部分页游标，不含经验正文或凭证。导入错误保存在 `manual.import.last_error`，与顶层上传错误分别查看；存在跳过来源时不能报告全部导入成功。详细归并、限额及并发源变更边界见[运维指南](../guides/16-successful-experience-sync.md)。


### 缺口与索引状态（兼容新增字段）

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `index_pending` | integer | 正文已写入、语义或向量处理未确认的条数；是 pending/retry 的子集，不额外累加到总文档数 |
| `manual.import.rejected_records` | integer | 有缺口的数组记录数；同来源有效记录仍处理 |
| `manual.import.error_counts` | object | 安全错误码 → 发生次数；包含来源级与记录级错误，不等于来源数 |
| `manual.import.errors_sample` | array | 最多 20 条摘要，按发现顺序；完整记录保存在现有同步状态前缀 |

摘要包含 `source_key`、可用的 `session_id`、`code`、`source_bytes`、`revision`、`stage`、`ordinal`（从 1 开始，来源级为 0）、`record_bytes`、`limit_bytes`；不可用大小为 null。旧任务可能没有新增字段，客户端按缺省值展示，并保留旧缺口总数。`rejected_sources` 表示有至少一个缺口的来源，可能同时贡献有效经验。`completed` 加缺口表示部分完成，不改变原状态枚举。

存量 Session/历史读取的可选配置 `experience_sync.import_max_source_mb` 默认 64（整数 1—1,024）；不是 HTTP 请求参数。每页最多 100 条/1 MiB、单条最多 256 KiB；来源版本确认前仅私有暂存，不进入上传队列。实现入口：`teamEvolver/storage/experience_import.py`；预算与错误处理见[运维指南](../guides/16-successful-experience-sync.md)。


### 重试执行诊断

状态额外返回 `server_time`、`last_error_at`、`retry` 和 `last_delivery_pass`。`retry` 包含已到期 `due`、退避中 `deferred`、最早可重试的 `next_attempt_at`、最近尝试 `last_attempt_at`、`error_counts` 以及最多 5 条 `samples`。样例只含 URI、来源/经验标识、失败次数、时间及安全的 `failure`（阶段、方法、接口、HTTP 状态、OV request ID），不含正文和凭证。旧记录没有阶段信息时返回 null，不能推断成新故障。

`last_delivery_pass` 是持久化的最近一批上传检查，包含开始/结束时间、检查量、尝试量、退避跳过量及该批最早重试时间，不代表全库完成。`worker` 是响应实例的运行状态（running/idle/blocked/lock_busy/waiting/stopped）、进程 ID、调度器是否运行及安全错误；多实例时不能把本实例空闲解释为其他实例也空闲。接口仍为租户管理员只读。

`next_attempt_at` 是最早可执行时间，实际开始受调度、分页和租户锁影响。重复导入相同正文不重置原退避。写入超时后 TE 尝试回读同一 URI；摘要匹配只确认正文已写入，语义/向量状态仍为 unknown，不报告 synced。
