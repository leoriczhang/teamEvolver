# 常见问题

## 基础概念

### teamEvolver 和 OpenViking 是什么关系？

PostgreSQL 是 teamEvolver 可变状态的唯一权威存储：Session、Skill 版本、Candidate、任务与配置都在其中。OpenViking 存放 Agent 可读的 Memory 与 Resource，并提供 Compile；团队 Memory 聚合、知识库挖掘与成功经验同步依赖 OpenViking。未配置 OpenViking 时，Session 接入、Skill 进化、True Replay 与发布仍可运行。

### teamEvolver 是 Agent Runtime 吗？

不是。teamEvolver 不执行面向用户的 Agent 任务，也不接管 Agent 的工具循环。它在现有 Agent Runtime 之上接收 Session、生成并验证 Skill Candidate、维护团队 Memory。分析、进化与验证会调用已配置的模型。

### teamEvolver 和 Langfuse 是什么关系？

两种独立用法：数据源 Adapter 可以从 Langfuse 拉取 Agent 会话，见 [数据源接入指南](../guides/10-datasource-onboarding.md)；链路观测把 teamEvolver 自身的模型调用上报到 Langfuse，见 [链路观测指南](../guides/04-observability.md)。两者都是可选的。

## 部署与配置

### 默认端口是多少？

`api` 角色使用 52010，提供控制台、API 与健康检查。`worker` 角色只在 52011 提供 `/healthz`、`/readyz`。

### 配置文件在哪里？

启动配置来自环境变量和 `TEAMEVOLVER_CONFIG_FILE` 指向的 YAML，`teamEvolver config` 只读显示合并结果。租户与模块配置保存在 PostgreSQL，通过控制台或 API 修改。见 [配置参考](../guides/01-configuration.md)。

### 支持哪些模型？

任何兼容 OpenAI Chat Completions API 的服务。默认 Base URL 为 `https://api.openai.com/v1`，模型为 `gpt-4o-mini`。启动配置通过 `TEAMEVOLVER_LLM_*` 设置，租户默认模型在控制台「模型配置」修改，阶段级模型在「进化链路」覆盖。

### 进化和 Memory 聚合会定时运行吗？

默认不会。Skill 进化和团队 Memory 维护都可在控制台选择手动或定时自动触发；手动运行入口始终保留。Worker 还负责定时数据源拉取与成功经验同步。

### 可以连接另一台机器上的自建 OpenViking 吗？

可以。在「运行状态 → OpenViking 部署」选择远程自建，在 Endpoint 覆盖中填写可达地址，例如 `http://openviking.example.com:1933`。保存后热重载，不需要重启。

### DreamCycle 和团队 Memory 聚合有什么区别？

两者是团队 Memory 运行的两个阶段。聚合阶段读取所选用户的个人 Memory，Compile 为团队 Memory；DreamCycle 维护阶段对已有团队 Memory 去重、修订和标记过时内容。一次运行可以选择仅聚合、仅维护或两者都做，见 [Memory](../concepts/04-memory.md)。

## Agent 接入

### 最小接入需要做什么？

1. 从管理员取得服务地址与租户 Key。
2. 为每个最终用户确定稳定的 `user_id`。
3. Session 结束后按 [Agent 协议](../agent-integrations/06-protocol-v2.md) 调用 `POST /ingest_session`。

最小接入不需要实现 Replay 或 Skill 拉取。

### 租户 Key 泄露了怎么办？

在「成员与接入」轮换租户 Key，或调用 `POST /api/tenants/{tenant_id}/rotate-token`。新 Key 只展示一次，旧 Key 立即失效，服务端只保存哈希。轮换影响该租户下所有 Agent，需同步更新 Agent 配置。

### 可以接入多个 Agent Runtime 吗？

可以。不同 Runtime 使用同一租户 Key，并声明实际用户的 `user_id`。Context 与 Session 按 `(tenant_id, user_id)` 隔离，所有 Session 一起参与进化。

## True Replay

### True Replay 会调用真实外部工具吗？

由 Replay 适配器决定。适配器必须为每个分支提供独立工作区，并区分可在沙箱内重放的工具与有外部副作用的工具。无法安全重放的 Case 应标记为不可运行。见 [True Replay](../concepts/06-true-replay.md)。

### Checklist 和评分是什么关系？

Checklist 是完成性门禁，不是质量评分。Candidate 必须满足全部 Checklist 项，之后才按交互轮次、工具调用数、Token 用量依次比较效率。见 [Checklist](../concepts/07-checklist.md)。

### Baseline 分支也必须通过 Checklist 吗？

是。Baseline 无法完成 Checklist 时，说明 Case 或现有 Skill 有问题，不做效率比较。

## Skill 与 Memory

### 自动发布还是人工审核？

`auto_evaluate=true`（默认）时 Candidate 生成后自动评估。`publish_mode=manual`（默认）要求管理员在「候选评审」中发布；改为 `automatic` 后，只有静态检查、运行时门禁通过且 verdict 为 accept 才会自动发布。见 [进化闭环](../concepts/02-evolution-loop.md)。

### Skill 回滚会删除版本吗？

不会。回滚以历史版本内容写一个新版本，所有版本都保留。见 [发布与回滚](../concepts/08-publish-rollback.md)。

### 个人 Memory 和团队 Memory 的区别？

- **个人 Memory**：位于 `viking://user/<user>/memories/`，Agent 通过 `POST /internal/agents/context/remember` 写入，本人可在控制台编辑
- **团队 Memory**：默认位于 `viking://resources/shared-knowledge/`，由团队 Memory 运行生成和维护，Agent 只读，租户管理员可编辑

## 故障排查

### 运行状态显示 OpenViking 不可用？

检查「运行状态 → OpenViking 部署」中的 Endpoint、Account 与 Root Key，以及服务到 OpenViking 的网络。

### Session 上报成功但控制台看不到？

上报返回 202 只表示已受理。用返回的 `job_id` 调用 `GET /api/jobs/{job_id}` 查看 `session.normalize`、`session.analyze` 是否完成；分析阶段需要可用模型与运行中的 Worker。

### 前端构建报 "cannot find module"？

在仓库根目录执行 `npm --prefix web-ui ci`，再执行 `npm --prefix web-ui run build`。

更多问题见 [故障排查](../guides/06-troubleshooting.md)。
