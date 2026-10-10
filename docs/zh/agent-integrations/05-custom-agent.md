# 自定义 Agent 接入

自定义 Agent 使用 [Protocol V2](./06-protocol-v2.md) 接入。系统不要求 Agent
注册，也不签发 Agent 级 Token；鉴权只使用租户机器凭证，业务身份由
`user_id` 表示。

## 接入准备

1. 获取 teamEvolver 服务地址。
2. 获取租户机器凭证 `tevt_<random>`。
3. 确认调用使用的 `user_id` 属于该租户 Account。
4. 按需配置 Session 上报、Context Workspace、Skill 拉取和 Replay Adapter。

多租户凭证由控制台创建租户时签发并仅展示一次。单租户环境通过
`TEAMEVOLVER_TENANT_TOKEN` 配置。

## Session 上报

```http
POST /ingest_session
Authorization: Bearer tevt_<random>
Content-Type: application/json
```

请求至少包含稳定的 `session_id`、`user_id`、`runtime.type` 和对话内容。
`runtime.type` 只描述运行时类型，不参与鉴权。

详细契约见 [Session 接入](../api/03-session-ingest.md)。

## Context Workspace

Context 接口位于 `/internal/agents/context/*`。请求必须携带租户凭证，并在
请求中明确 `user_id`；`integration_id` 可作为来源和审计标签，但不需要
预注册。

常用入口：

| 操作 | 接口 |
|------|------|
| 描述能力 | `GET /internal/agents/context/describe` |
| 解析上下文 | `POST /internal/agents/context/resolve` |
| 读取内容 | `POST /internal/agents/context/read` |
| 查询 Skill | `GET /internal/agents/context/skills` |
| 写入 Memory | `POST /internal/agents/context/remember` |
| Context Session | `/internal/agents/context/sessions/*` |

详细契约见 [Context Workspace](../api/04-context-workspace.md)。

## Skill 与 Replay

- Agent 通过 Skill 拉取接口获取已发布版本；发布链路不依赖 Agent 回调注册。
- True Replay 运行时由 `validation.runtimes` 与租户 Replay Adapter 决定。
- 自定义 HTTP/Python Runtime Adapter 必须由部署 Owner 配置，不能由 Agent
  请求携带代码或凭证。
- Baseline 与 Candidate 必须使用相互隔离的会话和工作区。

参考 [Replay API](../api/05-replay-branch.md) 与
[Skill 同步](../api/06-skill-sync.md)。

## 验证

```bash
curl -fsS "http://<teamevolver-host>:52010/health"
curl -H "Authorization: Bearer ${TEAMEVOLVER_TENANT_TOKEN}" \
  "http://<teamevolver-host>:52010/status"
```

服务端不提供 `/internal/agents/register` 或 `/api/agent-integrations`；调用这些
旧接口会返回 `404`。
