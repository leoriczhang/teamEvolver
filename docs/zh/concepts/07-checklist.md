# Checklist 门禁

Checklist 是 True Replay 中每个 Replay Case 必须满足的**扁平完成条件集合**。它是一个完成性门禁（pass/fail），不是质量评分——Candidate 必须完成所有 Checklist 项，不部分分、不加权、不打质量分。

## Checklist 是完成门禁，不是评分

Checklist 的核心定位是**判定任务是否完成**，而非判定完成质量如何。这与质量评分有本质区别：

| 维度 | Checklist 门禁 | 质量评分 |
|------|---------------|---------|
| 输出 | 布尔值（all_satisfied: true/false） | 连续分数或等级 |
| 项间关系 | 所有项必须全部满足（AND 逻辑） | 加权求和，可互相补偿 |
| 部分完成 | 任意一项未满足即不通过 | 部分项得分可拉高总分 |
| 评判依据 | 只看是否有具体 Evidence 表明完成 | 评估完成质量、优雅程度、效率等 |
| 在决策中的作用 | 前置硬性门禁，不通过则直接拒绝 | Checklist 通过后才进行效率比较 |

> **注意**：Checklist Judge 的系统提示明确要求"Evaluate each checklist item using only the supplied responses, tool trajectory, and real workspace artifacts. Output JSON {items:[{id,satisfied,evidence}],all_satisfied}. Do not infer success without concrete evidence."——没有具体 Evidence 不得推断完成。

## 单个 Replay Case 的 Checklist

每个 Replay Case 携带一组 Checklist 项，每项包含：

| 字段 | 说明 |
|------|------|
| `id` | 稳定标识符，输出类为 `R{NN}`（如 R01、R02），轨迹类为 `T{NN}`（如 T01、T02） |
| `text` | 完成条件的自然语言描述 |
| `kind` | `output`（输出类要求）或 `trajectory`（执行轨迹类要求） |
| `satisfied` | （评估后填入）是否满足，布尔值 |
| `evidence` | （评估后填入）满足或不满足的具体 Evidence |

### Checklist 项的来源

Checklist 项可通过两种方式设置：

1. **显式指定**：Case 中直接提供 `checklist` 数组，每项包含 `id`、`text`、`kind`
2. **自动生成**：当 Case 未显式提供 checklist 时，从 `requirements` 和 `trajectory_requirements` 字段自动展平生成：
   - `requirements` 展平为输出类项（R01, R02, ...）
   - `trajectory_requirements` 展平为轨迹类项（T01, T02, ...）
   - 支持 Markdown 列表格式，自动去除列表前缀并去重

展平逻辑会处理：列表/元组/字典（提取 text/requirement 字段）、多行文本、Markdown 列表前缀（`-`、`*`、数字编号等）。

## Checklist Judge 评估

每轮交互后，Checklist Judge 使用 LLM 对当前执行状态进行评估：

1. **输入**：Checklist 项列表、交互历史（interactions）、工具轨迹（tool_trajectory）、workspace 产物文件列表（workspace_artifacts，含文本预览，最多 40 个文件）
2. **输出**：`{items: [{id, satisfied, evidence}], all_satisfied}`
3. **保守降级**：Judge 不可用时，所有项标记为 `satisfied: false`，evidence 为 "checklist judge unavailable"，judge 状态标记为 "unavailable"

Judge 只基于提供的 Evidence 做判断，不做推测。评估结果用于渐进披露协议决定下一轮披露哪些未满足项。

### 渐进披露中的 Checklist

渐进披露协议使用 Checklist 评估结果驱动后续交互：

1. 第一轮执行后，Judge 评估哪些项已满足
2. 从未满足且未披露的项中取一批（batch_size，默认 4）作为下一轮提示
3. Agent 针对这些未满足项补齐工作
4. 重复直到所有项满足或无更多项可披露

每轮披露的提示格式为：
```
第 N 轮 Checklist 检查仍有未满足项。
保留已完成内容，只补齐以下要求：
1. [R01] 具体要求文本
2. [T01] 具体要求文本
...
完成后重新检查现有产物并给出最新结果。
```

## 聚合 Case Checklist

一个 Skill Candidate 的 True Replay 包含多个 Replay Case（来自 Test Dataset）。所有 Case 的 Checklist 结果聚合为候选版本的总体 Checklist 结果：

- **逐 Case 评估**：每个 Case 独立运行 Baseline 和 Candidate 分支，各自得到 Checklist 报告
- **聚合逻辑**：`aggregate_case_checklists` 汇总所有 Case 的结果
  - `all_satisfied`：**所有** Case 的 **所有** Checklist 项都满足才为 true（AND 逻辑）
  - `case_count`：有 Checklist 项的 Case 数量
  - `total`/`satisfied_count`/`unmet_count`：跨 Case 汇总的项数统计
  - `reports`：每个 Case 的详细 Checklist 报告

> **提示**：聚合是严格的——只要有一个 Case 有一个 Checklist 项未满足，整个 Candidate 的 Checklist 门禁就不通过。这保证了 Candidate 不会在已知失败场景下被接受。

## Checklist 如何门禁 Candidate 接受

Checklist 结果与效率比较共同决定 Candidate 是否通过，决策优先级为：

```
Checklist 门禁 ──► 效率比较
    │                  │
    ├─ Candidate 未通过 ──► 直接拒绝（candidate_checklist_incomplete）
    │
    ├─ Candidate 通过，Baseline 未通过 ──► 直接接受（candidate_only_completed_checklist）
    │
    └─ 双方都通过 ──► 进入效率比较（轮次→工具调用→Token）
```

具体决策规则在 `progressive_replay_decision` 中实现，策略 ID 为 `progressive_checklist_then_turn_priority_v1`：

| 场景 | 决策 | decision_basis |
|------|------|----------------|
| Candidate 有 Checklist 但未全部满足 | 拒绝 | `candidate_checklist_incomplete` |
| Candidate 通过、Baseline 有 Checklist 但未通过 | 接受 | `candidate_only_completed_checklist` |
| 双方都通过或双方都无 Checklist | 进入效率比较 | 由 objective_replay_decision 决定 |
| Checklist Judge 不可用 | 所有项视为未满足 → 拒绝 | 保守 fail-closed |

## Checklist 与质量评分的区别

理解 Checklist 的边界很重要：

**Checklist 回答的问题**：任务要求的所有产出和步骤是否都完成了？
- 例如："是否生成了配置文件？"、"是否运行了测试？"、"是否创建了 PR？"
- 只判定存在性和完成性，不判定好坏

**Checklist 不回答的问题**：
- 生成的代码质量好不好？→ 这是效率指标（更少的轮次/工具调用暗示更直接的解法）和人工审核的职责
- 方案是否最优？→ 效率比较可以在轮次/Token 维度体现优劣
- 边界条件是否处理？→ 这需要 Test Dataset 中包含对应的边界 Case

这种设计确保了自动验证的客观性和可复现性：完成性是可判定的事实，质量评估留给人或更复杂的机制。

## Memory Replay 中的 Checklist

DreamCycle 的 Memory True Replay 同样使用 Checklist 门禁，但规模更小：

- 每次 Memory Replay 只有一个 Case（而非 Test Dataset 的多个 Case）
- Checklist 由调用方传入，至少需要 1 项，最多 50 项
- 验证逻辑与 Skill Replay 完全一致：Baseline 和 Candidate 并行执行，Checklist 门禁优先于效率比较
- 要求 before 和 after 的 Memory 内容 hash 不同（`before_hash != after_hash`），否则视为无变更直接报错

## 代码入口

| 模块 | 路径 |
|------|------|
| 渐进披露与 Checklist 决策 | [progressive_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/progressive_replay.py) |
| Checklist 项生成 | [dataset_synthesizer.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dataset_synthesizer.py) |
| 本地 Checklist Judge | [true_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/true_replay.py)（`_evaluate_local_checklist`） |
| 效率比较决策 | [replay_metrics.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/replay_metrics.py) |
| Memory Replay Checklist | [dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py) |

## 相关文档

- [True Replay](./06-true-replay)：Checklist 在 True Replay 中的作用和渐进披露协议
- [进化闭环](./02-evolution-loop)：Checklist 门禁在进化闭环中的位置
- [发布与回滚](./08-publish-rollback)：通过门禁后的发布流程
