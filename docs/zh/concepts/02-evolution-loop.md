# 进化闭环

进化闭环是 teamEvolver 的核心运作机制：从 Agent 真实工作中采集 Session → 提取可复用经验 → 生成候选改进 → 在真实隔离环境验证 → 审核发布 → 下发到 Agent，形成持续增强的循环。

## 闭环总览

```
   ┌──────────┐     ┌──────────┐     ┌──────────┐
   │ Session  │────►│ Evidence │────►│Candidate │
   │  Ingest  │     │ Extract  │     │ Generate │
   └──────────┘     └──────────┘     └────┬─────┘
        ▲                                 │
        │                                 ▼
   ┌────┴─────┐     ┌──────────┐     ┌──────────┐
   │  Publish │◄────│  Review  │◄────│ Validate │
   │ & Sync   │     │  Gate    │     │(TrueReplay)
   └──────────┘     └──────────┘     └──────────┘
```

## 阶段详解

### 1. Session Ingest（会话采集）

Agent 在每次会话结束后通过 `/ingest_session` 上报完整轨迹：
- 完整消息序列（system/user/assistant/tool）
- 工具调用和工具结果
- 注入和使用的 Skill 列表
- 效率指标（轮次、工具调用数、Token 消耗）
- 使用的 Context 引用

上报使用租户 Key 与 runtime_context.user_id，服务端绑定租户内主体后写入队列。

### 2. Evidence Extraction（证据提取）

证据提取分两层，分别发生在不同阶段：

**会话级分析（Analyze 阶段）**：入库前，合并分析器（`team_skills/evolution/stages/analyze.py:analyze_session`，系统提示词为模块常量 `_ANALYZE_SYSTEM`）在一次模型调用中完成价值分类、摘要和四维评分（0.0-1.0）：

| 维度 | 权重 | 含义 |
|------|------|------|
| `task_completion` | 0.55 | 用户目标是否完成 |
| `response_quality` | 0.30 | 最终结果的正确性、完整性与清晰度 |
| `efficiency` | 0.05 | 执行路径是否避免了不必要的重试/绕路 |
| `tool_usage` | 0.10 | 工具使用是否恰当且有效 |

加权得到 `overall_score`，输出还包含每个维度的评分要点（`reasons`，中文要点列表）和整体 `rationale`。每个非空 Session 都必须在入库前完成合并分析（分类、摘要、评分）；已有 benchmark/aggregate 分数只作为输入证据，不会跳过分析。只有已持久化完整 `_summary` 与 `_judge_scores` 的 Session 在后续进化周期中复用结果，不重复调用模型。

**证据路由（进化 Prompt 内）**：不存在独立的"证据分类"阶段。候选生成时，进化 Prompt（`team_skills/evolution/stages/execute.py:evolve_skill_from_sessions`，路由规则为模块常量 `_EVIDENCE_ROUTING_RULES`）要求把每条候选经验归入且只归入一个桶：

| 桶 | 含义 |
|----|------|
| `team_skill` | 可复用的 SOP、稳定环境事实、工具/领域操作规程 |
| `user_memory` | 可归因于某个用户的偏好或习惯 |
| `task_requirement` | 仅针对当前交付物的明确要求或纠正 |
| `agent_runtime` | 中断、上下文丢失、工具故障、编排失败等运行时问题 |
| `insufficient_evidence` | 与 Skill 没有可论证因果联系的观察 |

只有 `team_skill` 证据才能修改共享 Skill；若全部观察落在其余桶中，进化选择 `skip`，Session 归档。曾经的 DreamCycle Memory 路由已被取代：ingest 阶段分类结果不为 `valuable` 的会话直接跳过并归档（见 [Sessions](./05-sessions)）。

### 3. Candidate Generation（候选生成）

每个进化周期，引擎把消费的 Session 按关联 Skill 分组，每个分组是一个独立分支，各自运行一次进化（`team_skills/evolution/runtime/orchestrator.py:_evolve_skill_group`）：基于该 Skill 关联的 Session 与跨周期 Evidence 账本，产出修订、新建或 `skip` 决策。不存在"证据积累到阈值才生成候选"的机制；`evidence_change_debt_threshold` 只影响 Evidence 账本的跨周期引导，不是候选生成阈值。

分支级约束：

- **团队证据最低要求**：每个分支的规划证据必须覆盖至少 `evolve.min_group_sessions`（默认 2）个不同 Session 且 `evolve.min_group_users`（默认 2）个不同 User（`team_skills/evolution/kernel/settings.py:EvolveServerConfig`），不满足则本轮跳过该分支；设为 0 表示关闭对应检查。
- **并行度上限**：分组分支与无 Skill 新建分支的并行数量由 `evolve.max_parallel_groups` 限制。
- **部分提交**：某分支失败时，其 Session 保留在队列中等待下轮重试；成功分支的 Session 正常消费并归档，避免持续失败的分组阻塞整个队列（`team_skills/evolution/runtime/orchestrator.py:_run_once`）。

Candidate 创建时不影响已发布的团队资产，仅存在于验证队列。

### 4. Dataset Synthesis（数据集合成）

从同源 Evidence 中自动生成测试用例：
- 从 Session 中提取用户输入作为测试任务
- 每个 Candidate 生成 `dataset_test_cases=2` 个以上的测试 case
- 累积到 `dataset_min_requirements=12` 条后启动验证

### 5. True Replay Validation（真实验证）

在接入方 Agent 的真实 Runtime 中，对每个测试 case 并行执行：
- **Baseline 分支**：加载当前已发布 Skill
- **Candidate 分支**：加载待验证的 Skill Candidate

两者共享相同的冻结 Context（通过 Snapshot Hash 保证一致性），在隔离环境中运行。结果比较：
1. **Checklist 门禁**：Baseline 和 Candidate 都必须完成所有 Checklist 项，不满足则直接拒绝
2. **效率对比**：Checklist 通过后，按轮次→工具调用数→总 Token 消耗排序，Candidate 必须不劣于 Baseline

### 6. Review Gate（审核门禁）

通过自动验证的 Candidate 进入管理员审核队列：
- 管理员在控制台查看 Evidence、变更 diff、True Replay 对比结果
- 可通过、拒绝或要求修改
- 超时（`human_review_timeout_seconds=86400`）后按配置自动处理

### 7. Publish & Sync（发布与同步）

审核通过后：
1. `SkillMutationService` 事务性提交新版本（记录 commit 历史 + tombstone 旧版本）
2. 持久化 outbox 写入分发队列
3. 已注册的 Agent 在下一次 `context/skills` 拉取或 webhook 推送时获得新版本
4. Skill Sync Adapter 确保至少一次送达，Agent 端收到后确认 `{"ok": true, "results": {...}}`

### 8. Rollback（回滚）

任何时候可回滚到历史版本：
- 以新版本形式恢复历史内容（保留版本链和审计记录）
- 不同时删除其他版本

## 进化触发

| 触发方式 | 说明 |
|---------|------|
| 自动周期 | `evolve.interval_seconds=600`（10分钟）扫描队列 |
| 手动触发 | `POST /trigger` 立即执行一次进化周期 |
| Session 驱动 | 积累足够 Evidence 时自动唤醒 |
| 连续排空 | 配置 `evolve.drain_max_per_cycle`（默认 0 = 不设上限）后，若单周期排空达到上限仍有积压，或周期内又有新会话入队，下一周期约 1 秒后自动启动，而不是等待完整间隔（`team_skills/evolution/runtime/orchestrator.py:run_periodic`）；排空本身按批读取会话（`team_skills/evolution/runtime/mixins.py:_drain_sessions`），队列空闲时仍按完整间隔休眠 |

## 发布模式

`evolve.publish_mode` 只接受两个值：

- `validated`：Candidate 进入验证队列；满足结果数、通过数和运行时兼容门禁后可由后台发布，灰区在启用 `human_review_enabled` 时进入人工复核。
- `direct`：进化结果直接发布，不经过 Candidate 验证队列。

当前没有 `evolve.enabled` 总开关。需要暂停自动扫描时，应停止服务或在部署层暂停进化进程，不要使用未定义的配置项。

## 相关文档

- [Skill 体系](./03-skills)：Skill 的结构、版本、生命周期
- [True Replay](./06-true-replay)：验证机制的详细说明
- [Checklist 门禁](./07-checklist)：完成性判定规则
- [发布与回滚](./08-publish-rollback)：版本管理和审计
