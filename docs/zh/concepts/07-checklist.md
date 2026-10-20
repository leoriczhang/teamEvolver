# Checklist 门禁

Checklist 是 True Replay 中每个 Replay Case 必须满足的**扁平完成条件集合**。它是完成性门禁（pass/fail），不是质量评分：所有项都满足才算完成，不计部分分、不加权。

## Checklist 是完成门禁，不是评分

| 维度 | Checklist 门禁 | 质量评分 |
|------|---------------|---------|
| 输出 | 布尔值 `all_satisfied` | 连续分数或等级 |
| 项间关系 | 全部满足（AND） | 加权求和，可互相补偿 |
| 部分完成 | 任一项未满足即不通过 | 部分得分可拉高总分 |
| 评判依据 | 只看是否有具体证据表明完成 | 评估质量、优雅程度等 |
| 决策作用 | 前置门禁 | 门禁通过后才比较效率 |

## 单个 Replay Case 的 Checklist

Checklist 来自 Test Dataset Case 的 `checks` 字段（`team_replay/policy.py:normalize_case_checklist`）：

| 字段 | 说明 |
|------|------|
| `id` | 稳定标识符，Case 内唯一 |
| `text` | 完成条件的自然语言描述 |
| `kind` | `output`（输出要求）或 `trajectory`（执行过程要求） |
| `polarity` | 可选，`must_not` 表示禁止出现的行为 |

缺少 `id`、`text` 或 `kind` 不合法的项会被丢弃。Candidate 生成时由数据集合成步骤（`team_replay/datasets/synthesis.py`）从同源 Evidence 生成 `checks`；也可以在「数据集」页面手工编辑。

## Checklist Judge 评估

每轮交互后由独立裁判评估（`team_replay/judging.py:judge_checklist`）：

1. **输入**：Checklist、逐轮交互、完整消息轨迹和产物。
2. **输出**：`{items:[{id, satisfied, evidence}], all_satisfied, positive_observations}`。输出必须覆盖全部 `id`，且每项都有布尔 `satisfied` 和字符串 `evidence`。
3. **证据要求**：`satisfied=true` 但 `evidence` 为空的项按未满足处理。
4. **fail closed**：模型不可用或输出不合法时，所有项标记为未满足，报告 `judge="unavailable"`，本分支终止。

裁判的系统提示词为 `JUDGE_SYSTEM`，可通过 Replay 裁判配置覆盖。

## 渐进披露

1. 首轮只发送 Case 的 `query`。
2. 每轮裁判评估后，`select_disclosure_items` 从未满足且未披露的项中按顺序选一批（`replay.progressive_disclosure.batch_size`，默认 4）。
3. 用户模拟器只看到这一批要求和已披露的要求，生成自然的用户反馈作为下一轮输入；反馈不含条目 ID 或评分。
4. 全部满足、无可披露项或达到 `max_interactions` 时结束。

## 聚合 Case Checklist

`team_replay/policy.py:aggregate_case_checklists` 汇总多个 Case：

- `all_satisfied`：所有 Case 的所有项都满足才为 true。
- `case_count`、`total`、`satisfied_count`、`unmet_count`：跨 Case 统计。
- `reports`：每个 Case 的报告。
- 任一 Case 裁判不可用时，聚合结果 `judge="unavailable"`。

## Checklist 如何门禁 Candidate

```text
任一裁判不可用 ─────────────► inconclusive（checklist_judge_unavailable）
双方都未完成 ───────────────► inconclusive（both_checklists_incomplete）
Candidate 未完成 ───────────► reject（candidate_checklist_incomplete）
Candidate 完成、Baseline 未完成 ► accept（candidate_only_completed_checklist）
双方都完成 ─────────────────► 效率比较：轮次 → 工具调用 / Token
```

规则实现在 `team_replay/policy.py:progressive_replay_decision`。效率比较见 [True Replay](./06-true-replay.md#判定)。

## Checklist 与质量评分的区别

**Checklist 回答**：任务要求的产出和步骤是否都完成了？例如“是否生成了配置文件”“是否运行了测试”。

**Checklist 不回答**：

- 代码质量好不好：由效率指标和人工审核判断。
- 方案是否最优：效率比较在轮次、工具调用和 Token 上体现。
- 边界条件是否处理：需要 Test Dataset 包含对应的边界 Case。

## Memory Replay 中的 Checklist

Memory Replay 使用同一套裁判、披露和判定规则（`team_memory/replay.py:specification` 把请求转换为单 Case 的 `ReplaySpec`）：

- 每次 Memory Replay 只有一个 Case，请求必须提供 `query` 和 `checklist`；`checklist` 可以是对象数组，也可以是按行分隔的文本，每行生成一个 `output` 项。
- Baseline 为变更前的 Memory 内容，Candidate 为变更后的内容，treatment revision 为内容的 SHA-256。
- 历史 Memory Change 的 Replay 自动读取变更前后快照，见 [Memory 体系](./04-memory.md#memory-change-与-true-replay)。

## 代码入口

| 模块 | 路径 |
|------|------|
| Checklist 规范化、披露与判定 | [policy.py](../../../team_replay/policy.py) |
| 裁判与用户模拟器 | [judging.py](../../../team_replay/judging.py) |
| 分支执行 | [execution.py](../../../team_replay/execution.py) |
| 效率比较 | [metrics.py](../../../team_replay/metrics.py) |
| Checklist 生成 | [synthesis.py](../../../team_replay/datasets/synthesis.py) |
| Memory Replay 输入 | [team_memory/replay.py](../../../team_memory/replay.py) |

## 相关文档

- [True Replay](./06-true-replay.md)
- [进化闭环](./02-evolution-loop.md)
- [发布与回滚](./08-publish-rollback.md)
