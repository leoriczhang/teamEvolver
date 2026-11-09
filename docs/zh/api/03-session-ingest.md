# Session 上报与数据源 API

Session 有两种接入方式：Agent 主动上报（Push）和租户数据源拉取（Pull）。两者走同一条受理链路：API 校验身份与协议，在一个短事务内写入 `ingestions` 记录、原始 payload 和 `worker_tasks`，返回 `202`。Worker 依次执行 `session.normalize → session.split → session.analyze → session.archive`，其中 `session.analyze` 包含 Session Analyze 与 Trace Analyze；Pull 先执行 `session.pull`，再按条受理未重复的来源快照。

代码入口：

- 受理：`session_ingestion/api/__init__.py:create_router`
- 接入诊断：`session_ingestion/api/console.py:create_router`
- 协议校验：`session_ingestion/push/protocol.py:normalize_session_envelope`
- 数据源控制面：`session_ingestion/api/datasource.py:create_router`
- Worker 阶段：`session_ingestion/handlers/__init__.py`
- 定时拉取：`session_ingestion/schedule.py:enqueue_due`

## Agent 上报

```
POST /ingest_session
Authorization: Bearer tevt_...
Content-Type: application/json
```

### 请求体

Envelope 为 `teamevolver.agent-session.v2`，Schema 见 [agent-session-v2.schema.json](../../schemas/agent-session-v2.schema.json)。

| 字段 | 类型 | 必填 | 说明 |
| --- | --- | --- | --- |
| `schema_version` | string | 是 | 固定 `teamevolver.agent-session.v2` |
| `protocol_version` | string | 是 | 固定 `2.0` |
| `session_id` | string | 是 | 调用方的 Session ID |
| `runtime.type` | string | 是 | 运行时标签，如 `custom-agent`、`pi`，不参与鉴权 |
| `runtime.version` | string | 否 | 运行时版本 |
| `runtime_context.user_id` | string | 是 | 租户内用户 ID，规则见 [协议 v2](../agent-integrations/06-protocol-v2.md#身份) |
| `turns` | array | 是 | 非空对象数组 |
| `turns[].turn_num` | integer | 否 | 轮次序号 |
| `turns[].prompt_text` / `response_text` | string | 否 | 用户输入与 Agent 回答 |
| `turns[].messages` | array | 否 | 完整消息列表 |
| `turns[].tool_calls` / `tool_results` | array | 否 | 工具调用与结果 |
| `turns[].injected_skills` / `used_skills` / `modified_skills` | array | 否 | Skill 使用情况 |
| `turns[].metrics` | object | 否 | 本轮观测指标 |
| `turns[].context_usage` | object | 否 | 本轮使用的 Context 引用，见下文 |
| `metrics` | object | 否 | 会话级指标 |
| `title`、`system_prompt` | string | 否 | 会话标题与系统提示词 |
| `source_materials` | array | 否 | 任务初始材料 |
| `business_time` | string | 否 | 业务发生时间（ISO 8601） |
| `meta` | object | 否 | 附加元数据 |

`runtime.integration_id` 与 `runtime_context.external_subject` 属于已退役的 Agent 注册协议，出现即返回 400。服务端用租户 Key 确定租户，用租户有效配置确定 OpenViking Account，客户端无法覆盖二者。

Agent 自报的 `provenance`、`source_identity`、`analysis_context` 及 `meta` 中的保留来源字段保存到 `reported_provenance`，不授予服务端核验状态，也不能指定服务端分析所读取的对象引用。turn 中自报的 `request_context` 原文保存到 `reported_request_context`，有效等级为 `agent_reported`、完整性为 `unknown`；自报 `final_request` / `complete` 不等于已观测到完整模型请求。

### context_usage

```json
{
  "context_snapshot_id": "ctxsnap_...",
  "memory_refs": [{"context_ref": "ctxref_...", "operation": "injected"}],
  "skill_refs": [{"context_ref": "ctxref_...", "operation": "read"}],
  "feedback": {}
}
```

受理时由 `teamEvolver/context/usage.py:verify_usage` 校验：快照与引用必须属于当前 `(tenant_id, user_id)` 且未撤销，引用类型与字段一致，`operation` 取 `retrieved`、`injected`、`read`、`selected` 之一，每类最多 200 个。给出快照时，引用必须属于该快照。

### 响应

```json
{
  "accepted": true,
  "ingestion_id": "3f0c...",
  "job_id": "3f0c...:session.normalize",
  "status": "accepted",
  "status_url": "/api/jobs/3f0c...:session.normalize",
  "dedupe": false
}
```

同一租户内同一 `session_id` 再次上报返回第一次受理的 `ingestion_id`、`job_id` 和当前状态，`dedupe` 为 `true`。

`job_id` 只对应首个阶段。用下文的 `/api/ingestions/{ingestion_id}` 查询整条接入链路，即使尚未生成正式 Session 也可诊断。归档后仍可用 [Session 查询](./08-sessions-api.md) 的 `/api/sessions/{session_id}/stages` 查看各阶段结果。

### 处理阶段

| 阶段 | Worker kind | 内容 |
| --- | --- | --- |
| normalize | `session.normalize` | 规范化 envelope，写入阶段结果 |
| split | `session.split` | 保存语义切分计划；未配置切分模型或切分失败时保留完整 Session，分段沿用同一来源 |
| analyze | `session.analyze` | Session Analyze（价值分类）与 Trace Analyze 两个模型阶段，任一失败则重试 |
| archive | `session.archive` | 必需阶段全部成功后写入正式 Session（业务时间 + 入库时间） |

价值分类为 `valuable` 的 Session 进入进化候选；`skipped` 的 Session 也会保存用于审计。模型调用失败不会生成正式 Session。

### 错误

| 状态 | detail | 原因 |
| --- | --- | --- |
| 400 | `unsupported session schema` | `schema_version` 不是 v2 |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED` | `protocol_version` 不是 `2.0` |
| 400 | `retired Agent identity fields are not accepted` | 携带 `integration_id` 或 `external_subject` |
| 400 | `V2 session runtime.type is required` | 缺少 `runtime.type` |
| 400 | `V2 session session_id is required` | 缺少 `session_id` |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | `user_id` 缺失或非法 |
| 400 | `V2 session turns must be a non-empty list of objects` | `turns` 为空或元素不是对象 |
| 400 | `invalid or expired context reference` 等 | `context_usage` 校验失败 |
| 400 | `session body must be valid JSON` / `session body must be an object` | 请求体格式错误 |
| 401 | `TENANT_TOKEN_REQUIRED` | 未使用有效租户 Key |
| 413 | `session body exceeds <n> bytes` | 超过 `TEAMEVOLVER_MAX_SESSION_BODY_BYTES`（默认 32 MiB） |
| 503 | `TENANT_ACCOUNT_REQUIRED` | 租户未配置 OpenViking Account |

### 示例

```bash
curl -X POST http://localhost:52010/ingest_session \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{
    "schema_version": "teamevolver.agent-session.v2",
    "protocol_version": "2.0",
    "session_id": "task-001",
    "runtime": {"type": "my-agent"},
    "runtime_context": {"user_id": "alice"},
    "turns": [{"turn_num": 1, "prompt_text": "分析成本", "response_text": "成本报告已完成。"}]
  }'
```

## GET /api/ingestions/{ingestion_id}

只读查询已受理的接入记录、当前流水线任务和阶段结果，不要求正式 Session 已归档。权限与 Session 控制台查询一致：需要租户管理员（控制台登录或服务 Root Key），租户 Key 不可访问。Root 请求可用 `Authorization: Bearer <TEAMEVOLVER_ROOT_API_KEY>` 与 `X-Tenant-ID` 指定租户。记录不存在或属于其他租户时返回 404。

```bash
curl http://localhost:52010/api/ingestions/3f0c... \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'X-Tenant-ID: default'
```

```json
{
  "ingestion_id": "3f0c...",
  "ingestion_status": "processing",
  "effective_status": "failed",
  "current_job": {
    "job_id": "3f0c...:session.analyze",
    "kind": "session.analyze",
    "status": "failed",
    "attempt": 3,
    "error_code": "retries_exhausted",
    "created_at": "2026-10-28T08:00:00Z",
    "updated_at": "2026-10-28T08:01:00Z"
  },
  "stages": [
    {
      "stage": "trace_analyze",
      "status": "failed",
      "error_code": "ValueError",
      "created_at": "2026-10-28T08:00:30Z",
      "updated_at": "2026-10-28T08:00:59Z"
    }
  ]
}
```

| 字段 | 说明 |
| --- | --- |
| `ingestion_id` | 受理返回的接入 ID，不是正式 Session ID |
| `ingestion_status` | `ingestions.status` 原值；单独为 `processing` 不能证明任务仍在运行 |
| `effective_status` | 结合当前 Worker 任务计算的展示状态，规则见下表 |
| `current_job` | 已存在流水线任务中阶段最靠后的任务；只公开 `job_id`、`kind`、`status`、`attempt`、`error_code`、`created_at`、`updated_at`；未找到任务时为 `null` |
| `stages` | 已记录的阶段结果；每项只公开 `stage`、`status`、`error_code`、`created_at`、`updated_at`；尚无结果时为空数组 |

流水线阶段顺序为 normalize → split → analyze → archive。查询通过平台任务接口做有限次读取，不仅查看受理时返回的首阶段 `job_id`，也不修改业务记录或任务状态。

| 条件 | `effective_status` |
| --- | --- |
| `ingestion_status` 已为 `archived` 或 `skipped` | 保留原值 |
| 当前任务为 `queued`、`running`、`retry_wait`、`waiting_external` 或 `reconciling` | `processing` |
| 当前任务为 `failed` | `failed` |
| 当前任务为 `cancelled` | `cancelled` |
| 未找到任务或当前任务为其他状态 | 保留 `ingestion_status` |

失败后的重试仍使用 [统一任务接口](./07-health-status.md#post-apijobsjob_idretry)：`POST /api/jobs/{current_job.job_id}/retry`。它复用原任务与已受理的来源，不重新拉取数据；只接受 `failed` 任务，其他状态返回 409。重试后任务回到 `queued`，未归档记录的 `effective_status` 随之恢复为 `processing`，不会被旧阶段失败结果固定为失败。

## 数据源 Adapter

数据源 Adapter 是租户提供的 Python 文件，负责连接上游（Doris、Langfuse 等）、过滤并把上游记录转换为标准 Session。Adapter 源码按版本保存在 PostgreSQL 的 `adapter_versions`，租户绑定保存在 `adapter_bindings`；运行中的任务固定其受理时的 revision。仓库 `session_ingestion/adapters/` 下的文件是预置模板，需由 Root Key 写入 PG 后才可绑定。

数据源列表、读取和绑定只处理数据源类型的文件，Replay 和外部评测器文件不会混入。文件名若已被其他适配器类型使用，保存会返回冲突，请另选文件名；不会覆盖另一类文件。

Adapter 文件要求（`session_ingestion/adapters/_runtime.py:validate_content`）：

- 文件名以 `.py` 结尾、不以 `_` 开头，内容不超过 256 KiB。
- 顶层定义 `build_adapter()`。
- 必需字面量字典 `SOURCE`：`enabled`（bool）、`supported_filters` / `required_filters`（字符串列表，required 必须被 supported 包含）、`exclude_session_id_patterns`、`max_sessions`（1–1000，预览默认条数）。可选 `source_instance_id` 与 `upstream_namespace` 必须为非空字符串，用于声明稳定来源范围。
- 凭证从环境变量读取，不写入源码。

### 接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/datasource` | 租户管理员 | 当前绑定、可用 Adapter、定时配置与最近一次拉取状态 |
| PUT | `/api/datasource` | 租户管理员 | `{"file": "x.py", "revision": 3}` 绑定；`file` 为空串表示解绑；省略 revision 取最新 |
| GET | `/api/datasource/code?file=x.py` | Root Key | 读取最新源码与 revision |
| PUT | `/api/datasource/code` | Root Key | `{"file", "code", "expected_revision"}` 保存新版本；revision 不匹配返回 409 |
| POST | `/api/datasource/code/test` | Root Key | `{"file", "code", "mode", ...}` 测试未保存草稿；`mode` 为 `validate`、`health`、`preview`、`session` |
| POST | `/api/datasource/test` | 租户管理员 | 对已绑定 Adapter 做连通性检查，返回 202 |
| POST | `/api/datasource/sessions` | 租户管理员 | `{"filters": {...}, "max_sessions": 100}` 预览 Session 列表，返回 202 |
| POST | `/api/datasource/pull` | 租户管理员或租户 Key | `{"from_timestamp", "to_timestamp", "filters"}` 拉取并接入，返回 202 |
| GET | `/api/datasource/jobs/{job_id}/result` | 租户管理员 | 测试、预览、拉取任务的结果 |
| GET | `/api/datasource/schedule` | 租户管理员 | 同 `GET /api/datasource` |
| PUT | `/api/datasource/schedule` | 租户管理员 | 保存定时拉取配置 |
| POST | `/api/datasource/schedule/run` | 租户管理员 | 立即按定时窗口（前一自然日）拉取一次 |

`validate` 模式同步返回 `{"ok": true, "metadata": {...}}`；其他测试、预览与拉取均以 `session.pull` 任务在 Worker 执行。`max_sessions` 取 1–1000。拉取会遍历完整过滤窗口，不受 `max_sessions` 限制。

### 手动重拉与来源去重

手动调用 `POST /api/datasource/pull` 即使窗口相同也创建新的扫描，以便发现晚到 Trace。请求重试可携带 `Idempotency-Key`：同租户、同 Adapter、同 Key 复用首次受理；用相同 Key 提交不同窗口或过滤条件返回 `409`。需要重新扫描时使用新 Key 或不传该头。定时拉取仍按日期窗口只受理一次。

扫描与逐条接入分开计数。`fetched` 是读取到的条数，`enqueued` 是新入队的来源快照，`duplicate` 是已受理的同源同事实快照；重复不代表原任务已处理完成。来源范围必须由绑定 Adapter 与上游返回确认，具体规则见 [持续 Session 与来源修订](../concepts/05-sessions.md#持续-session-与来源修订)。完全相同的事实复用既有任务与分析，即使分析配置后来变化，也不会因此自动批量重分析。

Langfuse 转换保留原始 Trace `input` 与 GENERATION observation `input`，写入每个 turn 的 `request_context`，包含原始 `source_id`、`model_call_id`、模型与参数。等级分别为 `trace_input` / `generation_input`，完整性均为 `unknown`；这保留原文，包括空 system 字符串，但不声称它就是最终完整模型请求。没有这些原始输入时等级为 `conversation_only`。

### 定时拉取

```json
{"enabled": true, "time": "02:00", "timezone": "Asia/Shanghai", "window": "previous_day"}
```

四个字段必须全部提供，`window` 只支持 `previous_day`。启用前必须先绑定 Adapter。配置保存在租户 `session` 模块配置的 `datasource_schedule`。Worker 每轮租户刷新时调用 `enqueue_due`：本地时间达到 `time` 后，按前一自然日窗口受理一次拉取，同一窗口以 `dedupe_key` 去重，多副本 Worker 不会重复受理。

### CLI

`teamEvolver datasource` 通过 Root Key 调用上述接口，需要设置 `TEAMEVOLVER_ROOT_API_KEY`：

```bash
teamEvolver datasource --tenant demo status
teamEvolver datasource --tenant demo bind example-doris.py
teamEvolver datasource --tenant demo test
teamEvolver datasource --tenant demo list --from-timestamp 2026-10-01T00:00:00+08:00 --to-timestamp 2026-10-02T00:00:00+08:00 --max-sessions 20
teamEvolver datasource --tenant demo pull --from-timestamp 2026-10-01T00:00:00+08:00 --to-timestamp 2026-10-02T00:00:00+08:00
```

`--url` 指定服务地址，默认 `http://127.0.0.1:<api_port>`。

操作步骤见 [数据源接入指南](../guides/10-datasource-onboarding.md)。
