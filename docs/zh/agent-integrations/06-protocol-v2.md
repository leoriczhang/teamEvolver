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

## 原始请求与来源声明

可以在 turn 中附带 `request_context` 作为原始请求报告，但服务端将原文保存在 `reported_request_context`，有效等级为 `agent_reported`、完整性为 `unknown`。自报的 `final_request` / `complete` 不能证明最终模型请求已被服务端观测；空 system 内容也不补写成其他文本。

客户端提交的 `provenance`、`source_identity`、`analysis_context` 和 `meta` 中的保留来源字段仅保存到 `reported_provenance`。它们不能设置服务端 `identity_verified`，不能指定分析使用的内部快照引用，也不能把一次上报拆成多个独立来源。数据源 Pull 由绑定 Adapter 和上游项目身份生成来源根、事实修订、目标 Trace 与上下文 Trace 引用，详见 [Session 来源修订](../concepts/05-sessions.md#持续-session-与来源修订)。

上报的 v2 必填字段与幂等边界保持不变：同租户同 `session_id` 的 Push 复用首次受理。持续上游 Session 的增量拉取通过数据源 Pull 处理；协议未新增显式重分析入口。

## 真实 Agent 的业务 Prompt 与上下文归属

真实业务 Agent 可以直接把实际执行记录送入已配置的 Langfuse；无需先转成 Excel，也无需制造离线 Session。管理员先登记业务 Prompt 模板和允许进化的上下文变量，再在执行配置中复制接入信息（`GET /api/skill-evolution/execution-profiles/{profile_id}/integration`，控制台权限）。Agent 的凭证、业务执行身份与 Prompt 的资产登记是三件不同的事，该接口不注册新的鉴权主体。

每个实际交互 Trace 的 `metadata.teamevolver_execution` 使用以下结构。模板引用、revision 和 hash 从实际加载的执行组合读取；变量和公开历史必须来自这次真实请求，不能始终沿用登记时的示例值。

```json
{
  "schema_version": "teamevolver.execution-observation.v1",
  "execution_profile_id": "business-agent-01",
  "agent_id": "agent-01",
  "runtime_type": "my-agent",
  "release_id": "actually-loaded-release-id",
  "composition_hash": "aaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaaa",
  "template_inputs": {
    "schema_version": "teamevolver.execution-inputs.v1",
    "template_refs": {
      "agent-01-main": {
        "template_id": "agent-01-main",
        "revision": "1",
        "content_hash": "bbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbbb"
      }
    },
    "template_variables": {"domain": "这次实际使用的领域内容"},
    "input_messages": [{"role": "user", "content": "实际传入的公开前文"}]
  }
}
```

这段示例中的 hash 只是字段格式示意，不能作为真实接入值。没有模板变量的固定 Prompt 可以省略 `template_inputs`。`release_id` 和 `composition_hash` 保留为上游声明；匹配以登记模板的精确身份、revision、正文 hash 和实际模型请求为依据，不把声明当成整个执行组合已正确加载的证明。

Trace 的 `input` 记录当前用户输入（例如 `{"messages":[{"role":"user","content":"当前问题"}]}`），`output` 记录实际答复。对应 GENERATION observation 的 `input.messages` 保留实际发送给模型的完整有序消息，包含渲染后的业务 Prompt、实际注入内容、公开前文和当前输入；仅在 metadata 中写一个 Prompt 名字不能替代它。当前输入的首尾空白也按原文保留。用户的后续纠正、反馈和实际输出继续作为原始交互记录保存，不能伪造评分或把 Replay 输出当作新的生产 Evidence。

TE 从已绑定的数据源拉取后，沿既有强制分析和候选链路处理这些 Session。模板和实际请求核验一致后，不同变量值仍属于同一共享模板；已登记的上下文值只按该变量的原始值匹配对应组件，不按变量组合创建新对象。未知值不会自动创建可进化组件。来源项目冲突、模板版本不符、变量与实际请求不一致、或新旧 metadata 声明冲突时，原始 Session 仍保留，但相关 Trace 不能作为该配置的进化依据；`request_context.execution_observation_errors` 保留元数据解析原因。

已有平铺的 `metadata.execution_profile_id` / `agent_id` 和离线 `trace.input.template_inputs` 仍可使用；同时提供两种来源时必须一致。持续会话使用稳定 `sessionId` 和每次交互唯一的 Trace ID；重复拉取保留原来源根，新增 Trace 才作为新目标，旧前文只是 Context。仅通过 `POST /ingest_session` 自报的请求仍遵循上节的 `agent_reported` 边界，不会因携带此 metadata 升级为服务端观测证明。

## 边界与约束

- Session envelope 只接受 `schema_version=teamevolver.agent-session.v2` 与 `protocol_version=2.0`；`runtime.integration_id`、`runtime_context.external_subject` 属于已退役的 Agent 注册协议，出现即返回 400。
- 团队 Skill 只通过 `GET /sync/skills` 拉取交付。
- 写入只允许个人 Memory；团队 Memory、团队 Skill 与团队 Resource 对 Agent 只读。
- Context 引用 15 分钟后过期，只能由签发时的 `(tenant_id, user_id)` 使用。

参考 [Session 上报](../api/03-session-ingest.md)、[Agent Context](../api/04-context-workspace.md)、[Skill 拉取](../api/06-skill-sync.md) 与 [Replay Adapter](../api/05-replay-branch.md)。
