# Agent 注册已退役

teamEvolver 不再提供 `/internal/agents/register`、`/api/agent-integrations`
或独立 Agent 注册表。新客户端直接使用“租户 Key + `user_id`”接入，不需要
预注册 Agent、声明 capability，也不会获得单独的 Agent Token。

## 身份模型

- 多租户部署：控制台创建租户时签发一次 `tevt_` 租户机器凭证，服务端只保存哈希。
- 单租户部署：运维通过 `TEAMEVOLVER_TENANT_TOKEN` 配置同一种租户凭证。
- 请求中的 `user_id` 表示该租户 Account 内的业务用户。
- `integration_id` 仅作为 Context、Session 与审计归属标识，不需要注册记录。
- OpenViking 访问只使用服务端配置的 `sharing.viking_api_key` Root Key。

## 当前入口

| 场景 | 接口 |
|------|------|
| Session 上报 | `POST /ingest_session` |
| Context 描述 | `GET /internal/agents/context/describe` |
| Context 解析 | `POST /internal/agents/context/resolve` |
| Context 读写 | `/internal/agents/context/*` |
| Skill 拉取 | `GET /skills` |
| Replay | 由 `validation.runtimes` 和 Replay Adapter 配置 |

调用方式与完整请求契约见：

- [Protocol V2：租户 Key + 用户身份](../agent-integrations/06-protocol-v2.md)
- [Context Workspace API](./04-context-workspace.md)
- [Session 接入](./03-session-ingest.md)

旧注册接口返回 `404`。不要继续发送 registration schema、capability、callback
endpoint 或 Agent 级存储凭证。
