# Agent 接入概览

准备服务地址、租户 Key 和 `user_id`，按 [v2 协议](./06-protocol-v2.md) 接入。
租户凭证由管理员配置或签发，Agent 不需要注册。Account 使用租户配置。

1. 用 [Session API](../api/03-session-ingest.md) 上报真实 Session。
2. 用 [Context API](../api/04-context-workspace.md) 获取个人/团队上下文。
3. 用 [Skill pull](../api/06-skill-sync.md) 在模型调用前更新团队 Skill。
4. 需要 True Replay 时，由管理员选择预装 [Replay adapter](../api/05-replay-branch.md)。

Hermes 使用 [安装指南](./03-hermes.md)。其他运行时使用 [自定义接入](./05-custom-agent.md)。
已有部署按 [分阶段升级](../guides/11-agent-deregistration.md) 迁移。
