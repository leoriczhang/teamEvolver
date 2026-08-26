# Memory 体系

Memory 是可检索的长期事实、背景、偏好与团队共识。它为 Agent 提供上下文，而不是像 Skill 一样规定完整任务流程。teamEvolver 将 Memory 分为个人资产和团队资产，并用独立的聚合、维护、实验和审计链路管理它们。

## 个人 Memory 与团队 Memory

| 维度 | 个人 Memory | 团队 Memory |
|------|-------------|-------------|
| 默认路径 | `viking://user/<user>/memories/` | `viking://resources/shared-knowledge/` |
| 可配置项 | 每个用户的 `personal_space.viking_user` | `aggregation.shared_knowledge_prefix` |
| Agent 权限 | 通过 Context Workspace 的 `remember` / `forget` 写自己的 Memory | 只读 |
| 控制台权限 | 本人可编辑自己的 Memory；管理员可切换用户 | 仅管理员可编辑 |
| 主要来源 | 用户偏好、工作习惯、个人事实和 Session 抽取结果 | 多个用户中反复出现并经过聚合的共性知识 |
| 进化方式 | Agent 写入、Workspace 编辑、Memory Lab 实验 | 跨 User 聚合、管理员批量编辑、可选 DreamCycle 维护和 Memory Replay |

团队 Memory 使用 Account 共享的 Resources 命名空间，不属于某个用户的私有 `memories/`。这使授权用户可以检索同一份团队产物，同时仍由 teamEvolver 控制写权限。

## 跨 User 团队 Memory 聚合

当前控制台中的「进化链路 → 团队 Memory 自进化」使用 `MemoryAggregationService` 和 `ov compile`：

1. 管理员提交可选的 OpenViking Account ID 和必填的 Admin Key。
2. 服务端使用本次请求的 Admin Key 枚举用户，排除 team 服务用户；Key 不持久化。
3. 管理员全选、反选或逐个选择参与用户，并选择增量或全量模式。
4. Phase 1 使用 Admin Key 和目标用户身份读取每个用户的 Memory，并并发生成 per-user staging。
5. Phase 2 以最多 15 个源为一组做 tree-reduce，最终写入团队 Memory 根。

默认目录：

```text
个人源     viking://user/<user>/memories/<kind>/
工作根     viking://resources/shared-knowledge-staging/
最终根     viking://resources/shared-knowledge/
```

`shared_knowledge_prefix` 和 `staging_dir` 可配置。工作根始终是最终根的同级目录，中间 `_merge` 文件不会进入最终根，也不会污染最终目录的 L0/L1 摘要。

### 聚合 Skill

聚合输出结构由「团队记忆聚合 Skill」定义。管理员可在控制台直接编辑完整 `SKILL.md`，内容默认持久化到 `~/.teamEvolver/aggregation/okf_skill.md`。下一次聚合时，服务会把它安装到每个参与身份自己的 Skill 空间。

Skill 内容变化会使下一次增量运行重新编译全部选中用户。用户 Memory 内容未变化且上次 staging 成功时，增量模式会复用已有 staging。

### 规模与失败恢复

- Phase 1 默认最大并发为 6。
- `merge_fan_in` 默认 12，运行时限制为 2–15，避免超过 compile 的 16 源上限。
- 单用户失败不会中止其他用户；下一次增量运行会重试失败或内容变化的用户。
- 页面刷新后可恢复当前服务进程内的最近任务；服务重启会清空任务列表，但不会清除磁盘上的指纹状态。

完整接口见 [团队记忆聚合 API](../api/11-team-memory-aggregation.md)。

## DreamCycle 维护

DreamCycle 是可选的 Memory 维护引擎，负责对其配置目标执行概况维护、去重、清理、可发现性检查和整合。它默认关闭，设置 `dreamcycle.enabled: true` 后才启用调度。

### 调度窗口

- `active_start_hour=0`、`active_end_hour=6`：默认活跃窗口为 0:00–6:00
- `rounds_per_window=3`：每个窗口最多 3 轮
- `round_interval_minutes=90`：轮次间隔 90 分钟
- `max_turns_per_job=25`：单个 Job 最大 ReAct 轮次

### 维护 Job

| Job | 职责 |
|-----|------|
| `team_overview` | 维护团队概况和入口信息 |
| `deduplication` | 使用语义相似度识别并合并重复内容 |
| `cleanup` | 归档过期、低价值或被替代的内容 |
| `onboarding_check` | 检查新人能否发现团队、项目、工具和流程 |
| `consolidate` | 从多个来源提炼去个人化的共性知识 |

DreamCycle 仍保留独立的 ReAct Job、策略工具和调度配置。它不是跨 User `ov compile` 聚合 API 的别名，两条链路可以独立启用。

## Memory Change 与 True Replay

DreamCycle 写入会通过 `MemoryChangeLedger` 记录 before/after Snapshot OID、内容 hash、diff hash、来源引用、策略理由和执行结果。记录使用 `teamevolver.memory-change.v1`，存放在平台根下的 `memory-changes/`。

`MemoryTrueReplayRunner` 可以把变更前内容作为 Baseline、变更后内容作为 Candidate，在冻结 Context 下执行 A/B Replay。Checklist 仍是完成门禁；通过后按交互轮次、工具调用数、Token 依次比较效率。结果存放在 `memory-replays/<change_id>/`。

## Workspace 与 Memory Lab

「资产中心 → Agent 工作空间」同时展示个人和团队 Memory：

- 浏览目录、文件及 L0/L1 摘要
- 管理员或资产所有者进入编辑模式
- 跨多个文件保留草稿，统一查看 Diff 后批量保存
- 通过内容哈希前置条件防止覆盖并发修改

Memory Lab 用于编辑不落盘的草稿，并比较改动前后的 Context 注入或 True Replay 结果。实验不会自动修改正式 Memory。

## 配置示例

```yaml
aggregation:
  enabled: true
  shared_knowledge_prefix: shared-knowledge
  staging_dir: staging
  phase1_concurrency: 6
  merge_fan_in: 12

dreamcycle:
  enabled: false
  active_start_hour: 0
  active_end_hour: 6
  rounds_per_window: 3
  round_interval_minutes: 90
  max_turns_per_job: 25
  dedup_merge_threshold: 0.86
  dedup_warn_threshold: 0.72
```

## 代码入口

| 模块 | 路径 |
|------|------|
| 跨 User 聚合服务 | `teamEvolver/aggregation/service.py` |
| 聚合路由与设置 | `teamEvolver/proxy/aggregation_routes.py` |
| 聚合 Skill | `teamEvolver/aggregation/okf_skill.py` |
| Workspace 作用域与批量写 | `teamEvolver/proxy/openviking_workspace.py` |
| DreamCycle 调度器 | `teamEvolver/dreamcycle/scheduler.py` |
| Memory Change 账本 | `teamEvolver/dreamcycle/memory_changes.py` |
| Memory Replay | `teamEvolver/dreamcycle/memory_replay.py` |
| OpenViking 存储客户端 | `teamEvolver/storage/viking.py` |

## 相关文档

- [存储空间与目录布局](./09-storage-layout.md)
- [True Replay](./06-true-replay.md)
- [配置参考](../guides/01-configuration.md)
- [Web 控制台](../guides/03-console.md)
- [团队记忆聚合 API](../api/11-team-memory-aggregation.md)
