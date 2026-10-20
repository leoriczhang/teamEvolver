# Agent 接入协议 v2

Agent 数据面使用 `Authorization: Bearer tevt_...` 与请求中声明的 `user_id`。服务端从租户 Key 确定租户，从租户有效配置确定 OpenViking Account。持有租户 Key 的调用方可以代表该租户内任意用户，不需要 Agent 注册或用户映射。控制台账号与 Agent 接入相互独立。

## 身份

唯一主体为 `(tenant_id, user_id)`，由 `teamEvolver/security/agent_principal.py:resolve_agent_principal` 解析。`user_id` 规则（`normalize_user_id`）：

- 非空字符串，经 NFKC 规范化并去除首尾空白后为 1–160 个字符。
- 只允许字母、数字和 `_`、`-`、`.`、`@`。
- 不能是 `.`、`..`，不能包含 `..`。

违反规则返回 `400 USER_ID_INVALID`，缺失返回 `400 USER_ID_REQUIRED`。不同租户中的同名用户彼此隔离。客户端不能覆盖租户或 Account；`X-Tenant-Id` 与 Key 所属租户不同返回 403。

## 数据面

| 接口 | 身份字段 | 用途 |
| --- | --- | --- |
| `POST /ingest_session` | Body `runtime_context.user_id` | 上报 Session |
| `GET /internal/agents/context/describe` | Query `user_id` | Context 能力与作用域 |
| `GET /internal/agents/context/skills` | Query `user_id` | Skill 列表与引用 |
| `POST /internal/agents/context/resolve`、`read`、`remember`、`forget` | Body `user_id` | 检索、读取、个人 Memory 写入与删除 |
| `POST /internal/agents/context/sessions/start`、`append`、`commit` | Body `user_id` | Context Session |
| `GET /sync/skills` | Query `user_id` | 团队 Skill bundle |

`runtime.type` 只用于分析、展示和 Replay 选择，不参与鉴权。租户 Key 还可以访问 `/status`、`/history` 与 `/api/datasource/pull`，其他控制台接口返回 403。

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

## Schema

- [Session v2](../../schemas/agent-session-v2.schema.json)
- [Context 请求 v2](../../schemas/agent-context-request-v2.schema.json)
- [Context 结果 v2](../../schemas/agent-context-result-v2.schema.json)
- [Context 快照 v2](../../schemas/agent-context-snapshot-v2.schema.json)

## 边界与约束

- Session envelope 只接受 `schema_version=teamevolver.agent-session.v2` 与 `protocol_version=2.0`；`runtime.integration_id`、`runtime_context.external_subject` 属于已退役的 Agent 注册协议，出现即返回 400。
- 团队 Skill 只通过 `GET /sync/skills` 拉取交付。
- 写入只允许个人 Memory；团队 Memory、团队 Skill 与团队 Resource 对 Agent 只读。
- Context 引用 15 分钟后过期，只能由签发时的 `(tenant_id, user_id)` 使用。

参考 [Session 上报](../api/03-session-ingest.md)、[Agent Context](../api/04-context-workspace.md)、[Skill 拉取](../api/06-skill-sync.md) 与 [Replay Adapter](../api/05-replay-branch.md)。
