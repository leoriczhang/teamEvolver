# Replay 分支执行 API

## 1. API 实现介绍

Replay 分支执行 API 是 teamEvolver 向已注册 Agent 发起的回调接口。与其他 Agent API 不同，Replay 请求由 **teamEvolver 主动调用 Agent 的 `replay_url`**，而非 Agent 调用 teamEvolver。当 teamEvolver 验证候选 Skill 时，会同时向 Agent 发送 baseline 和 candidate 两个分支的 replay 请求，对比两者的执行结果。

Replay 请求包含冻结的上下文投影、任务指令、执行限制（超时、最大交互轮次），Agent 必须在隔离沙箱中执行，不得产生外部副作用。成功结果必须包含效率指标（interaction_turns、tool_call_count、total_tokens）和 `context_input_hash`（实际注入上下文的哈希）。

代码实现：`teamEvolver/integrations/replay_adapters.py`
协议校验：`teamEvolver/integrations/agent_protocol.py:259` (`normalize_replay_request`、`normalize_replay_result`)
True Replay 引擎：`teamEvolver/true_replay.py`

## 2. 接口和参数说明

### 请求方向

```
teamEvolver --> POST https://<agent-replay-url>
```

### 请求头

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |
| `Authorization` | `Bearer <replay-api-key>`（如配置了 auth_profile） |

Replay API Key 通过环境变量配置，命名规则为 `TEAMEVOLVER_AGENT_<AUTH_PROFILE>_REPLAY_API_KEY`（auth_profile 转为大写下划线格式）。例如 auth_profile 为 `my_agent` 时，环境变量为 `TEAMEVOLVER_AGENT_MY_AGENT_REPLAY_API_KEY`。

代码：`teamEvolver/integrations/replay_adapters.py:27` (`resolve_replay_api_key`)

### 请求体（`teamevolver.replay-branch-request.v1`）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | `teamevolver.replay-branch-request.v1` |
| `protocol_version` | string | 是 | `1.0` |
| `request_id` | string | 是 | 请求唯一 ID（格式 `replay_<sha256-hash>`），响应中原样返回 |
| `job_id` | string | 是 | 验证任务 ID |
| `branch` | string | 是 | 分支类型：`baseline`（当前 Skill）或 `candidate`（候选 Skill） |
| `case` | object | 是 | 测试用例 |
| `case.query` | string | 是 | 任务指令/用户查询 |
| `case.instruction` | string | 否 | 同 query（兼容字段） |
| `case.materials` | array | 否 | 源材料列表 |
| `limits` | object | 是 | 执行限制 |
| `limits.timeout_seconds` | integer | 是 | 超时时间（秒），30-3600，默认 600 |
| `limits.max_interactions` | integer | 是 | 最大交互轮次，1-20 |
| `context_snapshot` | object | 否 | 冻结的上下文投影（resolve 结果快照） |
| `frozen_context` | object | 否 | 冻结上下文（同 context_snapshot） |
| `skill` | object | 否 | 候选 Skill 内容（branch=candidate 时） |
| `current_skill` | object | 否 | 当前 Skill 内容（branch=baseline 时） |
| `target_skill_name` | string | 否 | 目标 Skill 名称 |
| `source_session` | object | 否 | 源 Session 数据 |

### 超时控制

- 调用方（teamEvolver）设置 HTTP 超时为 `timeout_seconds + 30` 秒。
- Agent **必须**在 `limits.timeout_seconds` 内停止执行，在 HTTP 调用方超时后不得继续消耗模型或工具资源。
- baseline 和 candidate 请求并发发送，共享相同的截止时间。

### 响应体（`teamevolver.replay-branch-result.v1`）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | `teamevolver.replay-branch-result.v1` |
| `protocol_version` | string | 是 | `1.0` |
| `request_id` | string | 是 | 必须与请求的 request_id 完全一致 |
| `branch` | string | 是 | 必须与请求的 branch 完全一致 |
| `status` | string | 是 | 执行状态：`succeeded`、`failed`、`unsupported` |
| `metrics` | object | status=succeeded 时必填 | 效率指标 |
| `metrics.interaction_turns` | integer | 是 | 交互轮次，非负整数 |
| `metrics.tool_call_count` | integer | 是 | 工具调用次数，非负整数 |
| `metrics.total_tokens` | integer | 是 | 总 token 消耗，非负整数 |
| `metrics.input_tokens` | integer | 否 | 输入 token |
| `metrics.output_tokens` | integer | 否 | 输出 token |
| `metrics.cache_read_tokens` | integer | 否 | 缓存读取 token |
| `metrics.reasoning_tokens` | integer | 否 | 推理 token |
| `metrics.api_calls` | integer | 否 | API 调用次数 |
| `output` | object | 否 | 输出结果 |
| `output.final_response` | string | 否 | 最终响应文本 |
| `trace` | object | 否 | 执行轨迹（supports_full_trace=true 时推荐提供） |
| `trace.messages` | array | 否 | 消息列表 |
| `trace.events` | array | 否 | 事件列表 |
| `trace.interactions` | array | 否 | 交互记录 |
| `artifacts` | array | 否 | 产物列表（supports_artifacts=true 时） |
| `context_input_hash` | string | 推荐 | 实际注入上下文的 SHA-256 哈希，用于验证两分支输入一致性 |
| `runtime_checklist_report` | object | 否 | Checklist 执行结果 |
| `checklist_evidence` | object | 否 | Checklist 证据 |
| `error` | object | status!=succeeded 时必填 | 错误信息 |
| `error.code` | string | 是 | 错误码 |
| `error.message` | string | 是 | 错误描述 |
| `error.retryable` | boolean | 是 | 是否可重试 |
| `elapsed_seconds` | number | 否 | 执行耗时（秒） |

### 错误码

Agent 返回的错误码（`error.code`）：

| 错误码 | 说明 | retryable |
|--------|------|-----------|
| `EXECUTION_FAILED` | 执行失败（通用错误） | false |
| `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` | 遇到无法确定性重放的外部工具调用 | false |
| `TIMEOUT` | 执行超时 | false |
| `HTTP_ERROR` | HTTP 通信错误 | 视情况 |

teamEvolver 侧的适配器错误码：

| 错误码 | 说明 |
|--------|------|
| `INVALID_RESPONSE` | Agent 返回格式错误、request_id/branch 不匹配、指标缺失 |
| `TIMEOUT` | HTTP 请求超时 |
| `HTTP_ERROR` | HTTP 连接错误或非 2xx 响应 |

## 3. 隔离要求

Agent 的 Replay 运行时必须满足以下隔离要求：

1. **数据隔离**：仅实例化分支所需的源租户/用户/运行时配置，不得加载完整生产数据库或生产凭证。
2. **上下文确定性**：注入运行时实际使用的冻结 Context 投影（来自请求的 `context_snapshot`），不得执行新的上下文搜索或解析。返回 `context_input_hash` 作为实际注入内容的哈希。
3. **凭证隔离**：将上游模型凭证保留在候选方控制进程之外，位于短期父代理（broker）之后。Worker 进程不得直接持有模型 API Key。
4. **网络隔离**：将 Worker 放置在私有网络命名空间中，其本地模型 sidecar 通过受保护的 Unix socket 连接到父代理。禁止直接访问外网。
5. **文件系统隔离**：分支工作区是唯一可写的主机路径。
6. **外部工具策略**：
   - 工作区本地工具（文件读写、代码搜索等）可在沙箱内正常执行。
   - 记录的外部工具调用：通过工具名+规范化参数签名+调用序列+结果 SHA-256 匹配后确定性重放结果。仅按工具名匹配不符合协议规范。
   - 未记录的网络/外部工具：返回 `REPLAY_EXTERNAL_TOOL_UNSUPPORTED`（fail-closed），不得回退到实时调用。

Pi Agent 当前实现 `external_tool_replay=fail-closed` 策略：工作区本地工具在沙箱内执行，网络能力工具遇到未记录调用时直接使 case 不可运行。

## 4. Checklist 与效率比较

### Checklist 门禁

Checklist 完成度是通过/否决的门禁条件，而非加权分数。每个 checklist 项必须明确 pass/fail。候选分支必须通过所有 checklist 项才能被接受。

### 效率比较维度

效率比较按以下优先级排序（越少越好）：

1. `interaction_turns` -- 交互轮次
2. `tool_call_count` -- 工具调用次数
3. `total_tokens` -- 总 token 消耗

候选分支在 checklist 全部通过的前提下，效率不低于基线（no_regression）才会被自动接受。

## 5. 使用示例

### teamEvolver 发送的 baseline 请求示例

```json
{
  "schema_version": "teamevolver.replay-branch-request.v1",
  "protocol_version": "1.0",
  "request_id": "replay_a1b2c3d4e5f6...",
  "job_id": "job-20240115-001",
  "branch": "baseline",
  "case": {
    "query": "如何配置数据库连接池的最大连接数？",
    "materials": []
  },
  "limits": {
    "timeout_seconds": 600,
    "max_interactions": 4
  },
  "context_snapshot": {
    "snapshot_id": "ctxsnap_...",
    "items": []
  },
  "current_skill": {
    "name": "database-config",
    "content": "# Database Configuration\n..."
  }
}
```

### Agent 返回的成功响应示例

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_a1b2c3d4e5f6...",
  "branch": "candidate",
  "status": "succeeded",
  "metrics": {
    "interaction_turns": 2,
    "tool_call_count": 3,
    "total_tokens": 3200,
    "input_tokens": 2800,
    "output_tokens": 400
  },
  "output": {
    "final_response": "数据库连接池最大连接数配置方法如下..."
  },
  "trace": {
    "messages": [],
    "events": [],
    "interactions": []
  },
  "context_input_hash": "sha256:abc123def456...",
  "runtime_checklist_report": {
    "provides_code_example": {"passed": true},
    "mentions_default_value": {"passed": true}
  },
  "elapsed_seconds": 12.5
}
```

### Agent 返回的 unsupported 响应示例（外部工具不可重放）

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_a1b2c3d4e5f6...",
  "branch": "candidate",
  "status": "unsupported",
  "metrics": {},
  "error": {
    "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
    "message": "external tool call 'send_email' cannot be deterministically replayed",
    "retryable": false
  },
  "elapsed_seconds": 2.1
}
```

## 6. JSON Schema 参考

| Schema | 路径 |
|--------|------|
| Replay 请求 | `docs/schemas/replay-branch-request-v1.schema.json` |
| Replay 结果 | `docs/schemas/replay-branch-result-v1.schema.json` |

### 遗留兼容

早期 Pi Agent 版本使用不同的请求/响应格式。`teamEvolver/integrations/replay_adapters.py:141` (`LegacyAgentsHubHttpAdapter`) 提供一个兼容性周期的适配器，将旧格式转换为 V1 标准格式。新接入的 Agent 应直接实现 V1 格式。
