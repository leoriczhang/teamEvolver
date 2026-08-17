# Agent 接入概览

teamEvolver 作为 Agent 团队能力进化控制面，提供标准化的接入协议，让各类 Coding Agent、IDE 插件、AI 平台能够将会话数据回流、使用团队共享的 Memory 与 Skill、参与 True Replay 验证，并实时接收团队技能更新。

## 接入层级

teamEvolver 定义了两个接入层级，从最小可用到完整能力逐级叠加：

| 能力 | 最小接入 | 完整接入 |
|------|---------|---------|
| Agent 注册 (`/internal/agents/register`) | 必需 | 必需 |
| 主体映射 (subject mapping) | 必需 | 必需 |
| Session 上报 (`POST /ingest_session`) | 必需 | 必需 |
| Context Workspace (resolve/read/skills/remember/forget) | 否 | 必需 |
| Context Session (start/append/commit) | 否 | 必需 |
| True Replay 分支执行 (replay.branch.v1) | 否 | 必需 |
| Skill Sync 推送/拉取 | 否 | 必需 |
| Memory 写入 (remember/forget) | 否 | 必需 (个人范围) |

**最小接入**即可让 Agent 将会话数据回传至 teamEvolver，参与进化闭环的会话价值筛选和候选 Skill 生成。

**完整接入**进一步允许 Agent 使用 teamEvolver 作为统一的 Context 后端，在隔离沙箱中执行 True Replay 对比验证，并实时同步团队发布的 Skill 更新。

## 前置条件

- teamEvolver 服务已部署并运行，默认监听 `http://<host>:52010`
- 已配置 OpenViking 存储后端（共享 Memory/Skill 的持久化层）
- 获取控制面密钥 `EVOLVE_INGEST_API_KEY`（环境变量，由 teamEvolver 运维提供）
- 对于完整接入，Agent 需要暴露可被 teamEvolver 回调的 HTTP 端点（Replay URL、Skill Sync Webhook URL）

## 能力矩阵

以下是 teamEvolver Protocol V1 定义的能力标识及其对应的端点：

| 能力 ID | 描述 | 最小接入 | 完整接入 |
|---------|------|---------|---------|
| `session.ingest.v1` | 会话数据上报 | 必需 | 必需 |
| `context.workspace.v1` | Context Workspace 读写 | 否 | 必需 |
| `replay.branch.v1` | True Replay 分支执行回调 | 否 | 必需 |
| `skill.sync.v1` | Skill 变更推送接收 | 否 | 必需 |
| `memory.personal.read.v1` | 个人 Memory 读取 | 否 (通过 workspace) | 必需 |
| `memory.personal.write.v1` | 个人 Memory 写入 (remember/forget) | 否 | 可选 |
| `memory.team.read.v1` | 团队 Memory 读取 | 否 (通过 workspace) | 必需 |
| `skill.personal.read.v1` | 个人 Skill 读取 | 否 (通过 workspace) | 可选 |
| `skill.team.read.v1` | 团队 Skill 读取 | 否 (通过 workspace) | 必需 |
| `skill.bundle.v1` | Skill Bundle 全量拉取 | 否 | 推荐 |

## 上线顺序

推荐按照以下顺序逐步启用能力，确保每一步稳定后再开启下一层：

1. **注册与主体映射** -- 通过 `/internal/agents/register` 注册 Agent，在管理界面或注册 payload 中配置 `external_subject` 到 teamEvolver 用户的映射关系。
2. **Shadow 模式 Context** -- 启用 Context Workspace 但不将返回内容注入模型 prompt，仅验证连通性和权限配置。
3. **启用 Session 上报** -- 切换至一个集成点开启 V1 Session ingest，验证会话数据正确入库和归因。
4. **启用 Context 注入** -- 将 Context Workspace 返回的 Memory/Skill 内容注入模型上下文。
5. **启用 True Replay** -- 配置 replay_url，让 teamEvolver 在候选 Skill 验证时回调 Agent 执行基线/候选分支对比。
6. **启用 Skill Sync** -- 配置 skill_sync_url，接收团队 Skill 发布/回滚事件，实时更新本地 Skill 缓存。
7. **关闭遗留路径** -- 经过一个兼容周期后，禁用旧版共享密钥和直连存储路径。

回滚时不得降低 Replay 安全级别、基线 CAS 检查或中央 Checklist 判定。

## 相关文档

- [Protocol V1 协议规范](./02-protocol-v1.md) -- 详细的线协议定义、请求响应格式和错误码
- [Hermes 接入指南](./03-hermes.md) -- Hermes Coding Agent 的接入步骤
- [Pi Agent 接入指南](./04-pi-agent.md) -- Pi Coding Agent 运行时接入参考
- [自定义 Agent 接入](./05-custom-agent.md) -- 从零开始接入自定义 Agent 的步骤指南
- [API 参考](../api/01-overview.md) -- HTTP API 的完整参考文档
- JSON Schema 定义位于 `docs/schemas/` 目录
