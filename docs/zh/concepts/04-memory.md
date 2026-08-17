# Memory 体系

Memory 是可检索的长期事实、背景、偏好与团队共识。与 Skill 不同，Memory 不直接规定完整任务执行流程——它为 Agent 提供上下文背景，而非操作步骤。teamEvolver 将 Memory 分为个人资产和团队资产两层，由 DreamCycle 持续维护和进化。

## 个人 Memory 与团队 Memory

teamEvolver 中的 Memory 按归属分为两类：

| 维度 | 个人 Memory | 团队 Memory |
|------|------------|------------|
| 存储路径 | `viking://user/peers/{peer}/memories/` | `viking://user/memories/` |
| 写入权限 | 仅对应个人 Agent 可写 | 仅 DreamCycle 维护引擎可写 |
| 共享范围 | 归属于单个用户，不默认共享 | 经共享性判断后对团队成员和 Agent 可用 |
| 内容来源 | 用户偏好、个人工作习惯、特定上下文 | 跨多人反复出现的共性经验、团队共识、长期有效的事实 |
| 进化方式 | Agent 直接写入/遗忘，无需门禁 | DreamCycle 聚合、去重、清理、合并，按风险自动或人工处理 |

Agent 通过 Context Workspace 接口的 `remember`/`forget` 操作只能写入个人 Memory；团队 Memory 只能通过 DreamCycle 的 Memory Evolution 流程变更。

## DreamCycle

DreamCycle 是团队 Memory 的持续进化过程。它在预设的时间窗口内按优先级运行一组维护 Job，对团队 Memory 进行聚合、去重、清理、概况维护和可发现性维护。

DreamCycle 默认**不启用**。需在配置中设置 `dreamcycle.enabled: true` 才会启动调度器。

### 调度窗口

DreamCycle 使用基于时间窗口的调度策略：

- **活跃时段**：`dreamcycle.active_start_hour=0`、`dreamcycle.active_end_hour=6`，默认凌晨 0:00–6:00 运行
- **每轮次间隔**：`dreamcycle.round_interval_minutes=90`，每轮之间间隔 90 分钟
- **每晚轮次**：`dreamcycle.rounds_per_window=3`，每晚最多执行 3 轮
- **单 Job 最大轮次**：`dreamcycle.max_turns_per_job=25`，每个 Job 最多 25 个 ReAct 推理轮次

调度器支持跨午夜窗口配置（如 `active_start_hour=22, active_end_hour=6`）。支持守护进程模式（`--daemon`）和单次执行模式（`--once`，忽略时间窗口立即运行一轮）。

### 维护 Job

DreamCycle 按优先级顺序执行以下 Job：

| Job | 优先级 | 职责 |
|-----|--------|------|
| `team_overview` | 10（最先执行） | 维护团队概况：成员名单、角色职责、当前项目汇总、常用服务/工具/地址信息 |
| `dedup`（deduplication） | 20 | 语义去重：识别语义重复或高度重叠的团队 Memory 条目并合并/归档，使用嵌入模型做相似度检测 |
| `cleanup` | 30 | 清理过期内容：归档已完成项目的过程细节、超过 30 天的临时信息、被新版本取代的旧版本、调试产物和检查报告 |
| `onboarding_check` | 40 | 新人可发现性检查：模拟新人搜索"团队做什么""有哪些人""做什么项目""用什么工具""工作流程"，验证核心入口存在 |
| `consolidate` | 50（最低优先级，机会性执行） | 跨成员整合：从各 peer 的个人 Memory 中提炼**≥2 个不同 peer**独立出现的共性模式，去个人化后沉淀为团队 Memory |

> **注意**：`consolidate` Job 对个人 Memory 只有只读权限，严禁搬运个人 Memory 原文到团队 Memory，必须提炼为抽象后的共性结论并剥离个人身份信息。

### 语义去重

DreamCycle 使用嵌入模型做语义相似度检测，两个阈值控制去重行为：

- **`dreamcycle.dedup_merge_threshold=0.86`**：余弦相似度 ≥0.86 时，视为同一内容，触发合并
- **`dreamcycle.dedup_warn_threshold=0.72`**：相似度介于 0.72–0.86 之间时，标记为可能重复，由 LLM 判断是否需要合并

未配置嵌入模型（`dreamcycle.embed_model` 为空）时，语义去重被禁用，去重判断退化为仅由 LLM 基于文本判断，不使用词法重叠。

### Memory Change Ledger

DreamCycle 的每一次 Memory 写入（新增/合并/归档/清理）都通过 `MemoryChangeLedger` 记录为不可变的变更记录：

1. **变更前快照**：写入前对受影响路径做 OpenViking Snapshot commit，记录 `before_oid` 和 `before_hash`
2. **变更执行**：通过 Viking 工具（remember/forget/merge/sanitize）执行实际写入
3. **变更后快照**：写入后再做一次 Snapshot commit，记录 `after_oid`、`after_hash` 和 `diff_hash`
4. **持久化记录**：将完整的变更记录（change_id、run_id、job_name、action、target_paths、source_refs、reason、before/after 快照引用、决策结果）写入 `memory-changes/{date}/{change_id}.json`

变更记录的 schema 版本为 `teamevolver.memory-change.v1`，每条记录包含完整审计链：actor（默认 `teamEvolver:dreamcycle`）、started_at、completed_at、result（applied/partial/failed/noop）、snapshot_status。

### Blackboard

同一轮 DreamCycle 的所有 Job 共享一个 Blackboard 实例，用于跨 Job 传递已处理的 URI 事实和中间观察结果，避免重复读取和处理同一 Memory 条目。每个 Job 使用独立的 ReAct 引擎实例，但共享工具注册表和 Blackboard。

## Memory Replay

DreamCycle 对已应用的 Memory Change 支持内容级 True Replay 验证（`MemoryTrueReplayRunner`）。与 Skill 的 True Replay 类似，Memory Replay 并行执行两个分支：

- **Baseline 分支**：加载变更前的 Memory 内容（before_oid 对应的快照）
- **Candidate 分支**：加载变更后的 Memory 内容（after_oid 对应的快照）

两个分支共享冻结的 Context 投影（通过 `shared_context_hash` 保证一致性），差异仅限于被变更的 Memory 条目本身。验证使用相同的 Checklist 门禁和效率比较规则（轮次→工具调用→Token）。

Memory Replay 的结果 schema 版本为 `teamevolver.memory-true-replay.v1`，记录在 `memory-replays/{change_id}/{replay_id}.json`。

## DreamCycle 与 OpenViking 的关系

DreamCycle 的所有 Memory 读写都通过 OpenViking API 完成，不直接操作本地文件系统：

- **认证身份**：DreamCycle 以配置的 `agent_id`（通过 `OPENVIKING_AGENT_ID` 或 OpenViking API Key 解析）作为 OpenViking 用户身份（`X-OpenViking-User`）
- **维护空间**：默认维护自身用户的 `viking://user/memories/`；配置 `customer_id`（`OPENVIKING_CUSTOMER_ID`）时，维护范围收窄到 `viking://user/peers/{customer_id}/memories/`
- **读取范围**：读工具可以跨所有用户（含 peers）搜索和读取；写/归档工具严格限制在认证用户自身的 Memory 空间
- **快照能力**：利用 OpenViking Snapshot 做变更前后的内容版本捕获，支持 diff 和回滚到历史快照
- **测试后端**：支持 `memory://` 协议的进程内 InMemoryObjectStore，仅用于单元测试和 mock 模式，不作为用户可见的存储后端

> **提示**：`teamEvolver/storage/memory.py` 提供的是测试专用的内存对象存储，并非 DreamCycle 的 Memory 存储后端。DreamCycle 的持久化存储始终是 OpenViking。

## 配置参考

```yaml
dreamcycle:
  enabled: false               # 是否启用 DreamCycle 调度器
  active_start_hour: 0         # 活跃窗口开始小时（默认 0 点）
  active_end_hour: 6           # 活跃窗口结束小时（默认 6 点）
  rounds_per_window: 3         # 每晚最多执行轮次
  round_interval_minutes: 90   # 轮次间隔分钟数
  max_turns_per_job: 25        # 单 Job 最大 ReAct 推理轮次
  dedup_merge_threshold: 0.86  # 语义合并阈值
  dedup_warn_threshold: 0.72   # 语义告警阈值
  embed_model: ""              # 嵌入模型名，为空则禁用语义去重
```

## 代码入口

| 模块 | 路径 |
|------|------|
| DreamCycle 调度器 | [dreamcycle/scheduler.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/scheduler.py) |
| DreamCycle 配置 | [dreamcycle/config.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/config.py) |
| Memory Change 账本 | [dreamcycle/memory_changes.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_changes.py) |
| Memory Replay 运行器 | [dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py) |
| Blackboard | [dreamcycle/blackboard.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/blackboard.py) |
| ReAct 引擎 | [dreamcycle/react/engine.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/react/engine.py) |
| Job 基类与具体 Job | [dreamcycle/jobs/](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/jobs/) |
| Viking 工具集 | [dreamcycle/tools/viking.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/tools/viking.py) |
| 内存对象存储（测试用） | [storage/memory.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/storage/memory.py) |
| OpenViking 存储客户端 | [storage/viking.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/storage/viking.py) |
| 默认配置值 | [config_store/defaults.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/config_store/defaults.py) |

## 相关文档

- [架构总览](./01-architecture)：DreamCycle 在系统架构中的位置
- [进化闭环](./02-evolution-loop)：Memory Evolution 在进化闭环中的阶段
- [Skill 体系](./03-skills)：Skill 与 Memory 的边界对比
- [True Replay](./06-true-replay)：Memory Replay 使用的验证机制
