# Agent 接入协议 v2

Agent 数据面使用 `Authorization: Bearer tevt_...` 和请求中声明的 `user_id`。
租户由服务端解析凭证确定；OpenViking Account 来自该租户有效配置。
持有租户 Key 的调用方可以代表该租户 Account 内任意用户，不需要创建 Agent 注册或用户映射。
控制台用户注册与 Agent 接入独立。

## 身份

唯一主体为 `(tenant_id, user_id)`。`user_id` 必须非空，经 NFKC 规范化和去除首尾空白后最长
160 字符，只允许字母、数字、`_-.@`，禁止 `..` 与路径分隔符。
同名用户在不同租户中的 Account、Session 和 Context 隔离。
客户端提供的租户或 Account 不能覆盖服务端配置；冲突的 `X-Tenant-Id` 被拒绝。

## 数据面

| 接口 | 身份字段 | 用途 |
| --- | --- | --- |
| `POST /ingest_session` | `runtime_context.user_id` | v2 Session 上报 |
| `GET /internal/agents/context/describe` | Query `user_id` | Context 能力与作用域 |
| `GET /internal/agents/context/skills` | Query `user_id` | Skill refs |
| 其余七个 Context POST 接口 | Body `user_id` | 检索、读取、记忆与 Session |
| `GET /sync/skills` | Query `user_id` | 本租户共享 Skill bundle |

`runtime.type` 仅用于分析、展示和运行环境选择，不参与鉴权。
`tevt_` 不能访问管理员控制面。OpenViking Root Key 与模型 Key 留在服务端。

## 最小 Session

```json
{
  "schema_version": "teamevolver.agent-session.v2",
  "protocol_version": "2.0",
  "session_id": "task-001",
  "runtime": {"type": "my-agent"},
  "runtime_context": {"user_id": "alice"},
  "turns": [{"turn_num": 1, "prompt_text": "分析成本", "response_text": "成本报告已完成。"}]
}
```

参考 [Session API](../api/03-session-ingest.md)、[Context API](../api/04-context-workspace.md)、
[Skill pull](../api/06-skill-sync.md) 和 [Replay adapter](../api/05-replay-branch.md)。
Schema：[Session v2](../../schemas/agent-session-v2.schema.json)、
[Context 请求 v2](../../schemas/agent-context-request-v2.schema.json)、
[Context 结果 v2](../../schemas/agent-context-result-v2.schema.json)、
[Context 快照 v2](../../schemas/agent-context-snapshot-v2.schema.json)。

## 迁移边界

兼容发布默认 `identity_mode=dual`、`delivery_mode=push`。
先升级客户端，再切 `tenant_user/pull`，完成观察期后迁移数据；最终兼容代码清理单独发布。
存量 v1 Schema 保留原义，仅用于迁移期。详见 [升级与恢复](../guides/11-agent-deregistration.md)。
