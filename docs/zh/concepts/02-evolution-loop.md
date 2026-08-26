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

上报时携带 integration-scoped token 和 `external_subject`，服务端校验身份映射后写入队列。

### 2. Evidence Extraction（证据提取）

进化引擎的 Judge 阶段对 Session 进行分析，判断哪些内容可以上升为团队资产：

| Evidence 类型 | 去向 |
|--------------|------|
| 可复用的任务方法 | Skill Candidate |
| 长期事实/偏好/共识 | Memory Change（通过 DreamCycle） |
| 特定任务要求 | 丢弃（不属于团队资产） |
| Agent 运行时问题 | 标记为 runtime-issue，不进化 |
| 证据不足 | 归档，等待更多 Evidence 累积 |

### 3. Candidate Generation（候选生成）

当同一类 Evidence 积累到阈值（`evidence_change_debt_threshold=3`）时：
- **Skill Candidate**：基于多轮 Session 中的成功模式，合并为一个 Skill 修订或新建版本
- **Memory Change**：通过 DreamCycle 的 React 引擎聚合、去重后生成提案

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
