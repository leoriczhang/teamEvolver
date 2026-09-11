# Replay 分支执行 API

## 1. API 实现介绍

Replay 分支执行 API 是 teamEvolver 向已注册 Agent 发起的回调接口。与其他 Agent API 不同，Replay 请求由 **teamEvolver 主动调用 Agent 的 `replay_url`**，而非 Agent 调用 teamEvolver。当 teamEvolver 验证候选 Skill 时，会同时向 Agent 发送 baseline 和 candidate 两个分支的 replay 请求，对比两者的执行结果。

主模式是**服务端驱动的 Turn 协议**：Agent 注册 `replay.branch.v1` 能力时声明 `orchestration: "server_driven"`，teamEvolver 通过 `teamEvolver/integrations/replay_adapters.py:TurnBasedReplayAdapter` 每个交互轮次调用一次 Agent 的 turn 端点（当 capability 定义了 `request_template` 时改用 `MappedHttpAdapter`，将每轮渲染为客户自己的请求格式）。多轮循环、Checklist 评审、渐进披露和指标聚合全部由服务端完成；Agent 每次只执行一轮，永远不会看到 Checklist。

未声明 `orchestration` 的注册仍走单次调用回退路径：teamEvolver 通过 `teamEvolver/integrations/replay_adapters.py:HttpReplayAdapter` 为每个分支发送一个同步请求，由 Agent 自行完成多轮执行并返回聚合结果。

Replay 请求包含冻结的上下文投影、任务指令、执行限制（超时、最大交互轮次），Agent 必须在隔离沙箱中执行，不得产生外部副作用。回退模式下成功结果必须包含效率指标（interaction_turns、tool_call_count、total_tokens）；服务端驱动模式下 Agent 每轮必须回报 `metrics.tool_call_count` 和 `metrics.total_tokens`（fail-closed，缺失即校验失败）。

代码实现：`teamEvolver/integrations/replay_adapters.py`
协议校验：`teamEvolver/integrations/agent_protocol.py` (`normalize_replay_request`、`normalize_replay_result`、`normalize_replay_turn_request`、`normalize_replay_turn_result`)
True Replay 引擎：`teamEvolver/true_replay.py` (`_spawn_server_driven_branch`)

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

代码：`teamEvolver/integrations/replay_adapters.py:resolve_replay_api_key`

### 请求体（`teamevolver.replay-branch-request.v1`，单次调用回退模式）

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
| `case_index` | integer | 否 | 用例在 Test Dataset 中的序号 |
| `case_id` | string | 否 | 用例 ID（dataset_id 或序号） |
| `baseline_ref` | object | 否 | 基线引用信息 |
| `limits` | object | 是 | 执行限制 |
| `limits.timeout_seconds` | integer | 是 | 超时时间（秒），30-3600，默认 600 |
| `limits.max_interactions` | integer | 是 | 最大交互轮次，1-20 |
| `context_snapshot` | object | 否 | 冻结的上下文投影（resolve 结果快照） |
| `execution_manifest` | object | 否 | 执行清单 |
| `tool_policy` | object | 否 | 工具策略 |
| `checklist_policy` | object | 否 | Checklist 策略 |
| `skill` | object | 否 | 候选 Skill 内容（branch=candidate 时） |
| `current_skill` | object | 否 | 当前 Skill 内容（branch=baseline 时） |
| `target_skill_name` | string | 否 | 目标 Skill 名称 |
| `source_session` | object | 否 | 源 Session 数据 |
| `options.include_full_trace` | boolean | 否 | 是否要求返回完整 trace |

### 超时控制

- 调用方（teamEvolver）设置 HTTP 超时为 `timeout_seconds + 30` 秒。
- Agent **必须**在 `limits.timeout_seconds` 内停止执行，在 HTTP 调用方超时后不得继续消耗模型或工具资源。
- baseline 和 candidate 请求并发发送，共享相同的截止时间。

### 响应体（`teamevolver.replay-branch-result.v1`，单次调用回退模式）

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

### Server-Driven Turn 协议（`orchestration: "server_driven"`）

声明 `orchestration: "server_driven"` 后，teamEvolver 不再发送单个分支请求，而是按交互轮次逐轮调用 Agent 在 `replay_url` 注册的 turn 端点。每轮使用 `teamevolver.replay-turn-request.v1` / `teamevolver.replay-turn-result.v1` schema，由 `teamEvolver/integrations/agent_protocol.py:normalize_replay_turn_request` 和 `teamEvolver/integrations/agent_protocol.py:normalize_replay_turn_result` 校验。当 capability 定义了 `request_template`/`response_mapping` 时，teamEvolver 改用 `MappedHttpAdapter` 将每轮渲染为 Agent 自己的请求/响应格式，Agent 无需感知 teamEvolver 协议。

#### Turn 请求体（`teamevolver.replay-turn-request.v1`）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | `teamevolver.replay-turn-request.v1` |
| `protocol_version` | string | 是 | `1.0` |
| `request_id` | string | 是 | Session 句柄：相同 `request_id` 的后续轮次必须续接同一回放 Session，而不是重置 |
| `turn_num` | integer | 是 | 轮次序号，从 1 开始 |
| `branch` | string | 是 | `baseline` 或 `candidate` |
| `prompt` | string | 是 | 本轮指令（第 1 轮为用户原始 query，后续轮为渐进披露追加提示） |
| `history` | array | 是 | 此前各轮记录：`[{turn_num, prompt, response}]` |
| `limits.turn_timeout_seconds` | integer | 是 | 本轮超时（秒），30-3600，默认 600 |
| `context_snapshot` | object | 仅第 1 轮 | 冻结的上下文投影 |
| `skill` | object | 仅第 1 轮 | 该分支加载的 Skill 内容 |
| `materials` | array | 仅第 1 轮 | 源材料列表 |
| `tool_policy` | object | 仅第 1 轮 | 工具策略 |

#### Turn 响应体（`teamevolver.replay-turn-result.v1`）

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | `teamevolver.replay-turn-result.v1` |
| `protocol_version` | string | 是 | `1.0` |
| `request_id` | string | 是 | 必须与请求的 request_id 完全一致 |
| `turn_num` | integer | 是 | 必须与请求的 turn_num 完全一致 |
| `branch` | string | 是 | 必须与请求的 branch 完全一致 |
| `status` | string | 是 | `succeeded`、`failed`、`unsupported` |
| `final_response` | string | status=succeeded 时 | 本轮最终响应文本 |
| `messages` | array | 推荐 | 本轮完整消息轨迹（assistant/tool 消息）；这是服务端 Checklist Judge 唯一可见的评估证据 |
| `artifacts` | array | 否 | 本轮产物（可选证据，供服务端 Checklist 评估） |
| `metrics` | object | status=succeeded 时必填 | 本轮用量 |
| `metrics.tool_call_count` | integer | 是 | 非负整数；缺失或非法时该轮校验失败（fail-closed，不静默补零） |
| `metrics.total_tokens` | integer | 是 | 非负整数；同上 fail-closed |
| `metrics_incomplete` | boolean | 否 | 指标不完整时置 true（`MappedHttpAdapter` 对 plain 端点缺量补零时用于透明标记） |
| `error` | object | status!=succeeded 时必填 | `code`、`message`、`retryable` |

`status=unsupported` 用于本轮遇到无法确定性重放的外部副作用工具调用（fail-closed，不得回退到实时调用），整个分支随即以 `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` 终止。

服务端负责多轮循环与终止条件（Checklist 全部满足或无更多披露项）、Checklist Judge 评估、渐进披露和逐轮指标聚合；Agent 不聚合指标，也永远不会收到 Checklist。

#### Agent 侧参考实现

`scripts/replay_turn_server.py` 提供开箱即用的 Agent 侧 turn 服务：在 `AGENT_HANDLERS` 中为每个 `runtime_type` 注册一个处理函数即可，路由为 `POST /turn/<runtime_type>`（每轮一次）和 `GET /health`（探活）。详见[自定义 Agent 接入指南](../agent-integrations/05-custom-agent)。

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

以下示例为单次调用回退模式的请求/响应。

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

早期 Pi Agent 版本使用不同的请求/响应格式。`teamEvolver/integrations/replay_adapters.py:LegacyAgentsHubHttpAdapter` 提供一个兼容性周期的适配器，将旧格式转换为 V1 标准格式。新接入的 Agent 应直接实现 V1 格式。
