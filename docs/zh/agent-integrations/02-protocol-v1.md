# Protocol V1 协议规范

## 概述

Protocol V1 允许 Agent 将 teamEvolver 用作其上下文和进化控制面。OpenViking 凭证保留在 teamEvolver 服务端，Agent 获得一个限定作用域的访问令牌后可以：

- 上报带版本号的 Session 数据；
- 解析和读取个人/团队 Memory 与 Skill 上下文；
- 仅写入或遗忘已映射用户的个人 Memory；
- 在 Agent 的真实运行时中执行一次 baseline 或 candidate replay 分支；
- 接收已发布的团队 Skill 更新。

协议版本为 `1.0`。未知主版本号返回 `PROTOCOL_VERSION_UNSUPPORTED` 错误。不带版本号的 payload 通过单周期遗留适配器处理。

相关代码：`teamEvolver/integrations/agent_protocol.py`

## 注册

### 注册端点

```
POST /internal/agents/register
Authorization: Bearer <control-plane-key>
```

控制面密钥为环境变量 `EVOLVE_INGEST_API_KEY`。未配置时 V1 注册默认失败关闭（fail-closed）。

代码入口：`teamEvolver/integrations/agent_registry.py:register_agent()`

### 注册 Payload 格式

V1 注册必须指定 `schema_version` 为 `teamevolver.agent-registration.v1`，不得包含 OpenViking 端点或密钥。

最小注册 payload 示例：

```json
{
  "schema_version": "teamevolver.agent-registration.v1",
  "protocol_version": "1.0",
  "agent_id": "example:tenant-a",
  "runtime_type": "example",
  "runtime_version": "3.2",
  "capabilities": {
    "session.ingest.v1": {},
    "context.workspace.v1": {
      "scopes": [
        "personal_memory",
        "team_memory",
        "personal_skills",
        "team_skills"
      ]
    },
    "replay.branch.v1": {
      "transport": "http",
      "endpoint": "https://agent.example/replay/v1",
      "max_interactions": 20,
      "supports_materials": true,
      "supports_artifacts": true,
      "supports_full_trace": true,
      "idempotent": false,
      "auth_profile": "example"
    }
  },
  "endpoints": {
    "health_url": "https://agent.example/health",
    "replay_url": "https://agent.example/replay/v1"
  }
}
```

### Capability 字段说明

| 字段 | 类型 | 说明 |
|------|------|------|
| `session.ingest.v1` | object | 支持 Session 上报 |
| `context.workspace.v1` | object | 支持 Context Workspace，`scopes` 指定可访问范围 |
| `replay.branch.v1` | object | 支持 True Replay，需提供 `endpoint`、`max_interactions`、`supports_*` 等参数 |
| `skill.sync.v1` | object | 支持 Skill 推送同步，需提供 `skill_sync_url` |

### 主体映射（Subject Mappings）

控制面可以在注册时同步已存在于 teamEvolver 中的用户映射：

```json
{
  "subject_mappings_authoritative": true,
  "subject_mappings": [
    {
      "external_subject": "user-123",
      "team_evolver_user_id": "alice"
    }
  ]
}
```

此扩展仅在使用控制面密钥认证的注册路由上接受。它不会自动创建用户或凭证。未知的目标用户会在 `subject_sync.missing_user_ids` 中报告；权威同步会移除同一集成的过期映射。

### 注册响应

首次成功注册或显式轮换令牌时，响应的 `credentials.agent_access_token` 字段会返回一次访问令牌。注册表仅存储其 SHA-256 哈希。令牌前缀为 `tev1_`。

## 身份认证

访问令牌标识一个集成（integration），而非单个用户。每个 Context 请求还需提供 `external_subject` 参数。管理员通过以下方式映射：

```
integration_id + external_subject -> teamEvolver user
```

未映射的主体返回 `403 SUBJECT_NOT_MAPPED`。仅运行时用户名映射属于遗留模式，V1 不再支持。

代码实现：`teamEvolver/proxy/agent_context.py:119` (`_agent_context_auth` 和 `_agent_context_user`)

## Context Workspace

所有 Context Workspace 调用使用：

```
Authorization: Bearer <agent-access-token>
```

### 端点列表

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/internal/agents/context/describe` | 获取作用域描述和预算限制 |
| POST | `/internal/agents/context/resolve` | 根据查询解析上下文条目，返回不透明 `context_ref` |
| POST | `/internal/agents/context/read` | 读取指定 `context_ref` 的内容 |
| GET | `/internal/agents/context/skills` | 获取技能清单 |
| POST | `/internal/agents/context/remember` | 写入个人 Memory |
| POST | `/internal/agents/context/forget` | 删除个人 Memory |
| POST | `/internal/agents/context/sessions/start` | 开始一个 Context Session |
| POST | `/internal/agents/context/sessions/append` | 向 Context Session 追加事件 |
| POST | `/internal/agents/context/sessions/commit` | 提交 Context Session 并上报使用情况 |

代码实现：`teamEvolver/proxy/agent_context.py`

### 不透明引用（Opaque Refs）

`resolve` 返回短生命周期的不透明 `context_ref` 值（格式：`ctx_<random>`），永远不会返回个人 OpenViking URI 或密钥。`read` 仅接受由同一集成和用户签发的 ref。团队 Memory 和团队 Skill 为只读。`remember` 和 `forget` 仅限个人 Memory 范围。

Ref 默认 TTL 为 900 秒（15 分钟），最短 60 秒，最长 3600 秒。

代码实现：`teamEvolver/integrations/context_workspace.py:93` (`issue_ref`)

### Session 提交与使用上报

Session commit 可以包含 Agent 实际读取的显式 ref 列表：

```json
{
  "context_session_id": "ctxs_...",
  "used_context_refs": ["ctx_...", "ctx_..."]
}
```

teamEvolver 在服务端解析这些 ref 并在 `session.commit` 之前向 OpenViking 提交 `session.used` 记录。Ref 必须属于同一个 Context Session、集成和用户。使用上报按 payload 持久化，因此失败的 commit 可以重试而不会重复计入 OpenViking 使用量。

本地部署的 OpenViking 服务必须在 HTTP 请求之间持久化保存 `/used` 记录直到 Commit 消费它们；teamEvolver 的本地 OpenViking 服务将此待处理状态存储在 Session 的 `.usage.jsonl` 中。

代码实现：`teamEvolver/proxy/agent_context.py:322` (`_agent_context_submit_usage`)

### 内容层级与默认注入

- 默认注入应使用 L0（摘要）/ L1（概览）层级。
- 需要完整内容或 Skill Bundle 时必须显式调用 `read`（`level=full`）。
- 当全局条目预算跨越多个请求范围时，结果必须在应用预算前按范围交错排列，避免过大的个人空间挤占团队 Memory 或 Skill 上下文。

四个内容层级：

| 层级 | OpenViking 端点 | 说明 |
|------|-----------------|------|
| `l0` | `/api/v1/content/abstract` | 摘要（约 1000 字符） |
| `l1` | `/api/v1/content/overview` | 概览（约 4000 字符） |
| `l2` | `/api/v1/content/read` | 带偏移和限制的读取 |
| `full` | `/api/v1/content/read` | 完整内容（Skill 时返回 bundle） |

## Session 上报

```
POST /ingest_session
Authorization: Bearer <agent-access-token>
```

代码入口：`teamEvolver/proxy/routes.py:2971` (`ingest_session`)

### 必填身份字段

```json
{
  "schema_version": "teamevolver.agent-session.v1",
  "protocol_version": "1.0",
  "session_id": "session-1",
  "runtime": {
    "type": "example",
    "integration_id": "example:tenant-a",
    "version": "3.2",
    "protocol_version": "1.0"
  },
  "runtime_context": {
    "external_subject": "user-123"
  },
  "turns": [
    {
      "turn_num": 1,
      "prompt_text": "Perform the task",
      "response_text": "Done",
      "messages": [],
      "tool_calls": [],
      "tool_results": [],
      "injected_skills": [],
      "used_skills": [],
      "modified_skills": [],
      "metrics": {},
      "context_usage": {
        "context_snapshot_id": "ctxsnap_...",
        "memory_refs": [],
        "skill_refs": [],
        "feedback": {}
      }
    }
  ],
  "metrics": {},
  "source_materials": []
}
```

`runtime.integration_id` 必须与访问令牌匹配。Context 引用会对照服务端凭证进行验证；调用方提供的 scope 或 URI 值将被丢弃。

`runtime_context.external_subject` 为必填字段，用于主体映射解析。未映射时返回 `403 SUBJECT_NOT_MAPPED`。

## Replay 分支

HTTP Agent 暴露在 `replay.branch.v1` 中注册的确切端点。teamEvolver 为每个分支发送一个同步请求。baseline 和 candidate 调用并发执行，共享相同的 Context 和执行清单。

代码实现：`teamEvolver/integrations/replay_adapters.py`

### 超时与截止时间

调用方拥有截止时间控制权。Agent 必须在 `limits.timeout_seconds` 之前停止；在 HTTP 调用方超时后不得继续消耗模型或工具资源。timeout_seconds 范围：30-3600 秒；max_interactions 范围：1-20。

### 请求格式

teamEvolver 发送给 Agent 的 replay 请求格式：

```json
{
  "schema_version": "teamevolver.replay-branch-request.v1",
  "protocol_version": "1.0",
  "request_id": "replay_<hash>",
  "job_id": "<job-id>",
  "branch": "baseline",
  "case": {
    "query": "<task instruction>",
    "materials": []
  },
  "limits": {
    "timeout_seconds": 600,
    "max_interactions": 4
  },
  "context_snapshot": {},
  "skill": {},
  "current_skill": {},
  "source_session": {}
}
```

### 成功响应指标

成功结果必须包含非负整数指标：

| 指标 | 说明 |
|------|------|
| `interaction_turns` | 交互轮次 |
| `tool_call_count` | 工具调用次数 |
| `total_tokens` | 总 token 消耗 |

缺少指标、`request_id`/`branch` 不匹配或 schema 无效将失败关闭为 `INVALID_RESPONSE`。

### 响应格式

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_<hash>",
  "branch": "baseline",
  "status": "succeeded",
  "metrics": {
    "interaction_turns": 3,
    "tool_call_count": 5,
    "total_tokens": 4500
  },
  "output": {
    "final_response": "..."
  },
  "trace": {
    "messages": [],
    "events": [],
    "interactions": []
  },
  "context_input_hash": "<sha256>",
  "runtime_checklist_report": {},
  "elapsed_seconds": 45.2
}
```

### 运行时隔离要求

运行时必须隔离 replay 状态和凭证：

- 仅实例化分支所需的源租户/用户/运行时配置，不得加载完整生产数据库；
- 注入运行时实际使用的冻结 Context 投影，并将其哈希作为 `context_input_hash` 返回；
- 将上游模型凭证保留在候选方控制进程之外，位于短期父代理之后；
- 将 Worker 放置在私有网络命名空间中，其本地模型 sidecar 通过受保护的 Unix socket 连接到父代理；
- 将分支工作区保持为唯一可写的主机路径；
- 当记录的外部副作用无法确定性注入到当前运行时，返回 `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` 错误。

支持记录外部工具注入的 Agent 必须通过规范化工具名称、规范参数签名、同签名调用序列和结果 SHA-256 来标识每个结果。仅按工具名称匹配不符合 Protocol V1 规范。

Pi Agent 当前声明 `external_tool_replay=fail-closed`：工作区本地工具在分支沙箱内执行，而具备网络能力或外部工具则使该 case 不可运行，而非回退到实时副作用。

### Checklist 与效率比较

Checklist 完成度是门禁条件，而非加权分数。效率比较按以下顺序排序：interaction_turns（越少越好）、tool_call_count（越少越好）、total_tokens（越少越好）。

## Skill Sync

支持 `skill.sync.v1` 的 Agent 可以通过两种方式接收 Skill 更新：

1. **拉取模式**：通过 `GET /internal/agents/context/skills` 主动拉取技能清单
2. **推送模式**：在注册时提供 `skill_sync_url`，teamEvolver 在 Skill 发布/回滚时向该 URL 发送 webhook 回调

代码实现：`teamEvolver/integrations/skill_sync_adapters.py`

### 推送回调格式

```json
{
  "schema_version": "teamevolver.skill-changed.v1",
  "protocol_version": "1.0",
  "event_id": "skill_evt_<hash>",
  "action": "publish",
  "job_id": "<mutation-id>",
  "skills": [
    {
      "name": "<skill-name>",
      "version": 3,
      "sha256": "<hash>",
      "tree_sha256": "<tree-hash>"
    }
  ],
  "tenant_ids": ["<tenant>"]
}
```

### 确认与验证

Agent 收到推送后必须返回：

```json
{
  "ok": true,
  "results": {
    "<tenant-id>": {
      "verification": {
        "skills": [
          {
            "name": "<skill-name>",
            "matched": true,
            "actual_version": 3,
            "actual_sha256": "<hash>",
            "actual_tree_sha256": "<tree-hash>"
          }
        ]
      }
    }
  }
}
```

teamEvolver 会验证版本号和哈希是否匹配，不匹配则标记为同步失败并重试。

## 上线顺序

推荐的上线顺序：

1. 注册为 V1 并映射主体；
2. 以 `shadow` 模式运行 Context；
3. 将一个集成切换为 `enabled`；
4. 启用 V1 Session 上报；
5. 启用 Context 感知的 Replay；
6. 经过一个兼容周期后禁用遗留存储和共享密钥路径。

回滚期间绝不能降低 replay 安全性、基线 CAS 或中央 Checklist 判定。

## JSON Schema 参考

| Schema | 路径 |
|--------|------|
| Agent 注册 | `docs/schemas/agent-registration-v1.schema.json` |
| Session 上报 | `docs/schemas/agent-session-v1.schema.json` |
| Context 请求 | `docs/schemas/agent-context-request-v1.schema.json` |
| Context 结果 | `docs/schemas/agent-context-result-v1.schema.json` |
| Context 快照 | `docs/schemas/agent-context-snapshot-v1.schema.json` |
| Replay 请求 | `docs/schemas/replay-branch-request-v1.schema.json` |
| Replay 结果 | `docs/schemas/replay-branch-result-v1.schema.json` |
