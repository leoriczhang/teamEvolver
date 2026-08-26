# 常见问题

## 基础概念

### teamEvolver 和 OpenViking 是什么关系？

teamEvolver 是构建在 OpenViking 之上的 Agent 能力进化控制面。OpenViking 负责持久化存储（Memory、Skill、Session、Snapshot），teamEvolver 负责进化逻辑（Evidence 提取、Candidate 生成、True Replay 验证、门禁发布）。没有 OpenViking，teamEvolver 无法运行。

### teamEvolver 是 Agent Runtime 吗？

不是。teamEvolver 不作为面向用户任务的 Agent Runtime，也不接管接入 Agent 的工具循环。它会为 Evidence 提取、进化和验证调用已配置模型。它是架设在现有 Agent Runtime（Hermes、Pi、Codex 等）之上的控制层，负责让团队能力持续进化。

### teamEvolver 和 Langfuse 是什么关系？

Langfuse 是可观测性工具，teamEvolver 用它做两件事：（1）从 Langfuse 拉取其他 Agent 上报的 Session 轨迹；（2）把进化 Pipeline 内部的 LLM 调用也上报 Langfuse 做追踪。Langfuse 是可选依赖，不配置也能运行。

## 部署与配置

### 默认端口是多少？

**52010**。所有能力（控制台、API、健康检查）都走这一个端口。

### 配置文件在哪里？

`~/.teamEvolver/config.yaml`。使用 `teamEvolver config <key> <value>` 命令修改。

### 支持哪些 LLM Provider？

任何兼容 OpenAI Chat Completions API 的 Provider。默认配置是火山方舟 Doubao（`https://ark.cn-beijing.volces.com/api/v3`），可通过 `llm.api_base` 和 `llm.api_key` 修改。

### DreamCycle 默认开启吗？

不开启。DreamCycle 是团队 Memory 持续进化的后台 Job，需要显式设置 `dreamcycle.enabled: true`。默认在凌晨 0-6 点窗口运行。

### 可以连接另一台机器上的自建 OpenViking 吗？

可以。在「平台治理 → 运行状态 → OpenViking 部署」选择“自建 OpenViking”，并在 Endpoint 覆盖中填写远程可达地址，例如 `http://10.0.0.8:1933`。保存后会热重载，无需重启 teamEvolver。

### DreamCycle 和团队 Memory 聚合有什么区别？

跨 User 聚合由管理员显式选择 Account 用户，通过 `ov compile` 生成 `viking://resources/<shared_knowledge_prefix>/` 下的共享结果；DreamCycle 是可选的定时维护引擎，负责概况、去重、清理和可发现性。两条链路独立配置。

## Agent 接入

### 最小接入需要做什么？

1. 使用 control-plane key 调用 `/internal/agents/register` 注册 Agent
2. 保存返回的 `agent_access_token`
3. 管理员在控制台完成 `external_subject → user` 映射
4. 在 Agent 会话结束后调用 `/ingest_session` 上报轨迹

最小接入不需要实现 Replay 或 Skill Sync。

### SUBJECT_NOT_MAPPED 错误怎么办？

这表示 Agent 上报的 `external_subject` 没有映射到 teamEvolver 用户。需要管理员在控制台「用户与权限」中添加映射，或在注册时通过 `subject_mappings` 字段批量同步。

### Agent Access Token 泄露了怎么办？

使用 control-plane key 对同一个 Agent 再次调用 `POST /internal/agents/register`，并传入 `"rotate_access_token": true`。新 Token 签发后旧 Token 立即失效；服务端只保存 SHA-256 哈希。

### 可以接入多个 Agent Runtime 吗？

可以。每个 Agent Runtime 注册时获得独立的 `integration_id` 和 access token，彼此隔离。teamEvolver 会聚合所有已注册 Agent 的 Session 用于进化。

## True Replay

### True Replay 会调用真实外部工具吗？

默认 fail-closed。支持 Replay 的 Agent 必须把外部工具（网络请求、发邮件、写数据库等）与沙箱内工具（文件编辑、bash 命令）区分开。无法确定性重放的外部工具会让 Case 标记为不可运行，而不是回退到实时副作用。参考 [True Replay](../concepts/06-true-replay)。

### Checklist 和评分是什么关系？

Checklist 是**完成性门禁**（pass/fail），不是质量评分。Candidate 必须满足所有 Checklist 项才能通过，然后才比较效率（轮次→工具调用→Token）。效率比较时 Checklist 不计分。

### Baseline 分支也必须通过 Checklist 吗？

是的。如果 Baseline 都无法完成 Checklist，说明测试 Case 本身有问题或 Skill 有基础缺陷，此时不会做 Candidate 对比。

## Skill 与 Memory

### 自动发布还是人工审核？

默认 `publish_mode: validated`。Candidate 达到 `validation.required_results`、`validation.required_approvals` 和运行时兼容门禁后可由后台发布；灰区结果在 `human_review_enabled: true` 时进入人工复核。`publish_mode: direct` 会跳过 Candidate 验证队列直接发布。

### Skill 回滚会删除版本吗？

不会。回滚是「以新版本号恢复历史内容」，所有版本都保留，形成完整审计链。

### 个人 Memory 和团队 Memory 的区别？

- **个人 Memory**：归属于单个用户，只能由该用户的 Agent 写入和读取，`POST /internal/agents/context/remember` 写入的默认是个人 Memory
- **团队 Memory**：默认位于 `viking://resources/shared-knowledge/`，由跨 User 聚合生成，也可由管理员或可选 DreamCycle 维护；Agent 侧只读

## 故障排查

### 服务启动后 /status 显示 openviking_connected: false？

检查 `sharing.viking_endpoint` 和服务 Key（优先 `sharing.viking_team_api_key`）是否正确，网络是否可达 OpenViking 服务。

### Session 上报成功但队列里看不到？

检查 `runtime_context.external_subject` 是否正确映射，Session 是否被 SessionValueClassifier 判定为非 chitchat 且有 value。

### 前端构建时出现 "cannot find module" 错误？

在 `web-ui/` 目录下执行 `npm install` 安装前端依赖后重新 `npm run build`。

更多问题见[故障排查指南](../guides/06-troubleshooting)。
