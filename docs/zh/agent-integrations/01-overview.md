# Agent 接入概览

Agent 运行时通过三条数据面接口接入 TeamEvolver，统一使用租户 Key + `user_id`，不需要注册 Agent 或映射用户。协议约定见 [协议 v2](./06-protocol-v2.md)。

## 准备

| 项目 | 来源 |
| --- | --- |
| 服务地址 | 部署方提供，如 `https://teamevolver.example.com` |
| 租户 Key（`tevt_...`） | 平台管理员创建租户时签发，或租户管理员轮换；`default` 租户由 `TEAMEVOLVER_TENANT_TOKEN` 配置 |
| `user_id` | Agent 所服务用户在租户内的 ID |

租户 Key 只放在 Agent 服务端或受保护的配置文件中。OpenViking Root Key 和模型 Key 只在 TeamEvolver 服务端使用。

## 三条数据面

| 用途 | 接口 | 时机 |
| --- | --- | --- |
| 上报 Session | [`POST /ingest_session`](../api/03-session-ingest.md) | 每个任务结束后 |
| 获取上下文 | [Agent Context](../api/04-context-workspace.md) | 模型调用前检索 Memory / Skill / Resource，任务中写入个人 Memory |
| 拉取团队 Skill | [`GET /sync/skills`](../api/06-skill-sync.md) | 模型调用前，用 `ETag` 增量更新 |

最小接入只需要上报 Session。Context 和 Skill 拉取让 Agent 使用团队积累的知识。

## True Replay

验证 Skill Candidate 时，TeamEvolver 通过租户绑定的 Replay Adapter 驱动客户 Agent 执行 Baseline 与 Candidate 两个分支。Replay Adapter 是部署方编写的 Python 文件，由服务 Root Key 写入、租户管理员绑定，Agent 侧需要能为每个分支提供独立会话与工作区。见 [Replay Adapter](../api/05-replay-branch.md#replay-adapter)。

## 运行时说明

- [Pi Agent / AgentsHub](./04-pi-agent.md)
- [自定义 Agent](./05-custom-agent.md)
