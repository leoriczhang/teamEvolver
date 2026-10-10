# Pi Agent / AgentsHub 接入

Pi Agent 与其他自定义 Runtime 一样使用
[Protocol V2](./06-protocol-v2.md)：租户机器凭证负责鉴权，`user_id` 表示
业务用户，不需要 Agent 注册或 capability 声明。

## 基本配置

```bash
export TEAMEVOLVER_URL="http://<teamevolver-host>:52010"
export TEAMEVOLVER_TENANT_TOKEN="tevt_<random>"
```

Pi 上报的 Session 使用实际运行时标签：

```json
{
  "session_id": "pi-session-001",
  "user_id": "alice",
  "runtime": {"type": "pi"}
}
```

`runtime.type` 只用于数据分析和 Replay Runtime 选择，不参与身份认证。

## Context 与 Skill

- Context Workspace 使用 `/internal/agents/context/*`。
- 每次请求显式传入 `user_id`。
- `integration_id` 可固定为部署实例名，用于来源和审计标签。
- Skill 使用拉取模式获取，发布不依赖 webhook 注册。
- OpenViking Root Key 只保存在 teamEvolver 服务端。

## True Replay

部署 Owner 为 Pi Runtime 配置 Replay Adapter，管理员将其绑定到当前租户。
Adapter 负责创建隔离的 Baseline/Candidate 会话和工作区。下游输入不得包含
Checklist、判定标准或候选优劣标签。

Replay 配置与协议见 [Replay API](../api/05-replay-branch.md)。

## 参考入口

- [自定义 Agent 接入](./05-custom-agent.md)
- [Session 接入](../api/03-session-ingest.md)
- [Context Workspace](../api/04-context-workspace.md)
- [Skill 同步](../api/06-skill-sync.md)
