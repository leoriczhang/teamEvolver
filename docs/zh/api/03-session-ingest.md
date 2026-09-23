# Session 上报 API

## 1. API 实现介绍

Session 上报接口用于 Agent 将完整的会话轨迹数据（对话轮次、工具调用、技能使用、指标等）提交到 teamEvolver。上报的 Session 经过价值分类器筛选后，有价值的会话会进入进化队列，驱动 Skill 的自动优化和演进。

v2 使用租户机器凭证 `tevt_` 和 `runtime_context.user_id`。服务端从租户有效配置确定 Account，未注册 runtime 和未在控制台创建的用户也可上报。客户端不能覆盖租户与 Account。

V1 协议下（兼容窗口），Session 上报只接受**租户机器凭证**（`tevt_`，Agent 数据面唯一的机器凭证）；未携带有效凭证返回 `401 TENANT_TOKEN_REQUIRED`。上报必须提供 `runtime_context.external_subject` 用于主体映射。envelope 中协议必填的 `runtime.integration_id` 会被解析为该租户下已注册的 Agent：未注册返回 `403 UNKNOWN_INTEGRATION_ID`，已注册但状态不是 `active` 返回 `403 INTEGRATION_DISABLED`。`session.ingest.v1` capability 不再参与准入判定（租户内任何已注册且 active 的 Agent 都可以上报自己的数据），它仅作为声明与展示用的元数据保留。

对于已使用 Context Workspace 的 Agent，turn 中的 `context_usage` 字段会经过服务端验证，确保 context_ref 的有效性和归属权。

代码实现：`session_ingestion/push/routes.py` (`register_push_routes`)
共享入站管道：`session_ingestion/service.py` (`ingest`)
协议校验：`session_ingestion/push/protocol.py` (`normalize_session_envelope`)
Context 使用验证：`teamEvolver/integrations/context_workspace.py:514` (`verify_context_usage`)

## 2. 接口和参数说明

### 请求

```
POST /ingest_session
Authorization: Bearer <租户机器凭证 tevt_<random>>
Content-Type: application/json
```

### 请求体（`teamevolver.agent-session.v2` schema）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | 必须为 `teamevolver.agent-session.v2` |
| `protocol_version` | string | 是 | 协议版本，`2.0` |
| `session_id` | string | 是 | Session 唯一标识，最长 160 字符 |
| `runtime` | object | 是 | 运行时信息 |
| `runtime.type` | string | 是 | 运行时标签，用于分析与 Replay，无需注册（V1：与注册时 runtime_type 一致的运行时类型） |
| `runtime.integration_id` | string | 仅 V1 | 集成 ID，必须属于当前租户（由凭证推导）且状态为 `active` |
| `runtime.version` | string | 否 | 运行时版本 |
| `runtime.protocol_version` | string | 否 | 协议版本，默认 `2.0` |
| `runtime_context` | object | 是 | 运行时上下文 |
| `runtime_context.user_id` | string | 是 | 声明租户内的用户，规范化后长度为 1–160 字符 |
| `turns` | array | 是 | 对话轮次数组，至少 1 轮 |
| `turns[].turn_num` | integer | 是 | 轮次序号，从 1 开始 |
| `turns[].prompt_text` | string | 否 | 用户输入文本 |
| `turns[].response_text` | string | 否 | Agent 响应文本 |
| `turns[].messages` | array | 否 | 完整消息列表（system/user/assistant/tool） |
| `turns[].tool_calls` | array | 否 | 工具调用记录 |
| `turns[].tool_results` | array | 否 | 工具返回结果 |
| `turns[].injected_skills` | array | 否 | 本轮注入的 Skill 列表 |
| `turns[].used_skills` | array | 否 | 本轮实际使用的 Skill 列表 |
| `turns[].modified_skills` | array | 否 | 本轮修改的 Skill 列表 |
| `turns[].metrics` | object | 否 | 本轮指标（token 消耗等） |
| `turns[].context_usage` | object | 否 | Context 使用情况 |
| `turns[].context_usage.context_snapshot_id` | string | 否 | Context 快照 ID |
| `turns[].context_usage.memory_refs` | array | 否 | 使用的 Memory 引用列表 |
| `turns[].context_usage.skill_refs` | array | 否 | 使用的 Skill 引用列表 |
| `turns[].context_usage.feedback` | object | 否 | 用户反馈 |
| `metrics` | object | 否 | 会话级指标 |
| `metrics.interaction_turns` | integer | 否 | 总交互轮次 |
| `metrics.tool_call_count` | integer | 否 | 总工具调用次数 |
| `metrics.total_tokens` | integer | 否 | 总 token 消耗 |
| `metrics.input_tokens` | integer | 否 | 输入 token |
| `metrics.output_tokens` | integer | 否 | 输出 token |
| `source_materials` | array | 否 | 源材料列表（代码仓库、文档等） |
| `system_prompt` | string | 否 | 系统提示词 |
| `title` | string | 否 | 会话标题 |
| `force_reprocess` | boolean | 否 | 强制重新处理已处理过的 Session |
| `defer_evolution_trigger` | boolean | 否 | 延迟触发进化周期（批量上报时使用） |

### 请求体大小限制

默认最大 32MB，通过环境变量 `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` 调整（最小 1KB）。

### 响应

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 处理状态：`queued`（已入队）、`duplicate`（重复跳过）、`skipped`（无价值跳过） |
| `session_id` | string | Session ID |
| `queued` | boolean | 是否已进入进化队列 |
| `key` | string | 队列对象键，格式为 `<存储前缀>sessions/{session_id}.json`（`teamEvolver/session_store.py:queue_key`，仅 queued 时返回） |
| `trigger_scheduled` | boolean | 是否已调度进化触发（仅 queued 时返回） |
| `value_judge` | object | Session Analyze 的价值分类结果（skipped/queued 时返回）：`decision`（`valuable|memory_candidate|task_only|chitchat`）、`confidence`、`reason`、`memory_candidates`、`mode`（当前为 `merged`） |

## 3. 使用示例

```bash
curl -X POST "http://localhost:52010/ingest_session" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-session.v2",
    "protocol_version": "2.0",
    "session_id": "sess-20240115-001",
    "runtime": {
      "type": "my-agent",
      "version": "1.0.0",
      "protocol_version": "2.0"
    },
    "runtime_context": {
      "user_id": "user-001"
    },
    "title": "数据库连接问题排查",
    "system_prompt": "You are a helpful coding assistant.",
    "turns": [
      {
        "turn_num": 1,
        "prompt_text": "我的数据库连接一直超时，怎么排查？",
        "response_text": "数据库连接超时可以从以下几个方面排查...",
        "messages": [
          {"role": "user", "content": "我的数据库连接一直超时，怎么排查？"},
          {"role": "assistant", "content": "数据库连接超时可以从以下几个方面排查..."}
        ],
        "tool_calls": [],
        "tool_results": [],
        "injected_skills": ["database-debugging"],
        "used_skills": ["database-debugging"],
        "metrics": {
          "input_tokens": 520,
          "output_tokens": 890
        }
      }
    ],
    "metrics": {
      "interaction_turns": 1,
      "tool_call_count": 0,
      "total_tokens": 1410
    },
    "source_materials": []
  }'
```

## 4. 响应契约与错误处理

### 成功响应示例（已入队）

```json
{
  "status": "queued",
  "session_id": "sess-20240115-001",
  "queued": true,
  "key": ".../sessions/sess-20240115-001.json",
  "trigger_scheduled": true,
  "value_judge": {
    "decision": "valuable",
    "confidence": 0.92,
    "reason": "包含具体技术问题和有效解决方案",
    "memory_candidates": [],
    "mode": "model"
  }
}
```

### 成功响应示例（重复跳过）

```json
{
  "status": "duplicate",
  "session_id": "sess-20240115-001",
  "queued": false
}
```

### 成功响应示例（无价值跳过）

```json
{
  "status": "skipped",
  "session_id": "sess-20240115-001",
  "queued": false,
  "value_judge": {
    "decision": "task_only",
    "confidence": 0.85,
    "reason": "真实任务请求，但尚无完成成果或可演化证据",
    "memory_candidates": [],
    "mode": "model"
  }
}
```

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 401 | `TENANT_TOKEN_REQUIRED` | 缺少有效的租户机器凭证（`tevt_`） |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED` | 严格模式要求协议主版本 2 |
| 400 | `unsupported session schema: <schema>` | schema_version 不是 `teamevolver.agent-session.v2` |
| 400 | `V2 session session_id is required` | 缺少 session_id |
| 400 | `V2 session runtime.type is required` | 缺少 runtime.type |
| 400 | `V2 session turns must be a non-empty list` | turns 为空或不是数组 |
| 403 | `UNKNOWN_INTEGRATION_ID` | （V1）runtime.integration_id 未在该租户注册 |
| 403 | `INTEGRATION_DISABLED` | （V1）runtime.integration_id 对应的 Agent 已注册但状态不是 active |
| 403 | `SUBJECT_NOT_MAPPED` | （V1）runtime_context.external_subject 未映射到 teamEvolver 用户 |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED: <version>` | （V1）协议版本主版本号不是 1 |
| 400 | `unsupported session schema: <schema>` | （V1）schema_version 不是 `teamevolver.agent-session.v1` |
| 400 | `V1 session session_id is required` | （V1）缺少 session_id |
| 400 | `V1 session runtime.type is required` | （V1）缺少 runtime.type |
| 400 | `V1 session runtime.integration_id is required` | （V1）缺少 runtime.integration_id |
| 400 | `V1 session turns must be a non-empty list` | （V1）turns 为空或不是数组 |
| 400 | `invalid context_usage: <reason>` | context_usage 中的引用无效或过期 |
| 400 | `session body must be valid JSON` | 请求体不是有效 JSON |
| 400 | `session body must be an object` | 请求体不是 JSON 对象 |
| 413 | `session body exceeds <limit> bytes` | 请求体超过大小限制 |
| 503 | `session storage is not configured` | Session 存储未配置 |



服务端根据 tenant/user/外部 Session ID 生成内部 ID；返回的 Session ID 可能与输入不同。保存源 ID 供显示与追踪。旧协议仅在 `dual` 窗口兼容，严格模式要求 v2。

| HTTP | Error |
| --- | --- |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` |

当请求体不包含 `schema_version` 且不以 `teamevolver.agent-session.v1` 标识时，系统会回退到遗留兼容模式，使用共享密钥（`EVOLVE_INGEST_API_KEY`）认证，并通过 `user_alias`/`runtime_context.username` 进行用户名映射。此模式仅用于兼容旧版 Agent 接入，新接入应使用 V1 协议。遗留分支行为未变：即使使用租户机器凭证携带遗留 payload，仍走同一宽松的 `resolve_registered_user_id` 映射逻辑。
