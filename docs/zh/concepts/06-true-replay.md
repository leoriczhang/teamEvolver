# True Replay

True Replay 对每个 Test Dataset Case 打开相互隔离的 Baseline 与 Candidate 会话，使用相同的 query、材料、Context、模型、工具和限制，唯一差异是 treatment（被验证的 Skill 集合或 Memory 内容）。

## 执行过程

每个分支由 `team_replay/execution.py:run_branch` 执行：

1. 租户绑定的 ReplayAdapterFactory 打开独立会话；第一轮只发送数据集 query。
2. 收集真实 Agent 回复、轨迹、产物和指标（`AgentObservation`）。
3. 独立裁判（`team_replay/judging.py:judge_checklist`）按 Checklist 给出逐项证据，判定完成门禁。
4. 未完成时，`team_replay/policy.py:select_disclosure_items` 确定性选择本轮允许披露的未满足项（默认每批 4 项），独立用户模拟器（`render_user_feedback`）生成自然反馈。
5. 反馈写入 user 历史并继续执行；全部满足或达到 `max_interactions` 后结束并关闭会话。

客户适配器接触不到 Checklist、裁判状态或未披露要求。反馈只表达当前目标与缺口，不含条目 ID、评分和 A/B 内部用语。裁判不可用时 fail closed。

## 判定

`team_replay/policy.py:progressive_replay_decision` 先看 Checklist，再看效率：

| 场景 | verdict | decision_basis |
| --- | --- | --- |
| 任一分支裁判不可用 | inconclusive | `checklist_judge_unavailable` |
| 双方 Checklist 都未完成 | inconclusive | `both_checklists_incomplete` |
| Candidate 未完成 | reject | `candidate_checklist_incomplete` |
| Candidate 完成、Baseline 未完成 | accept | `candidate_only_completed_checklist` |
| 双方都完成 | 进入效率比较 | 见下表 |

效率比较（`team_replay/metrics.py:objective_replay_decision`）以交互轮次为主指标，工具调用数和总 Token 为次指标：

| 条件 | verdict |
| --- | --- |
| `interaction_turns` 减少 | accept |
| `interaction_turns` 增加 | reject |
| 轮次持平，次指标有改善且无退步 | accept |
| 轮次持平，次指标有退步 | reject |
| 全部持平或指标不可用 | inconclusive |

指标缺失记为 `unavailable`，不补 0。每个结论保留证据、对话、披露记录与 Adapter revision。

## 运行入口

| 入口 | 任务 | 说明 |
| --- | --- | --- |
| Candidate 验证 | `skill.validate` | 固定 Candidate、Baseline 版本、`replay` 配置和 Adapter revision |
| 数据集运行 | `dataset.run` | `POST /api/datasets/{id}/runs`，冻结数据集 revision 和当前 Skill 版本 |
| 显式 Replay | `replay.run` | `POST /api/replays/{skill\|memory}`，调用方提供完整 Baseline/Candidate treatment |
| 实验工作台 | `replay.run` | `POST /api/replay-lab/runs`，对未保存的 Candidate `SKILL.md` 运行 |
| Memory Replay | `memory.replay` | 见 [Memory 体系](./04-memory.md) |
| Adapter 测试 | `replay.run` | `POST /api/replay-adapter/code/test`，仅服务 Root Key |

所有运行都先持久化完整输入（`GET /api/replays/{domain}/{run_id}/input` 可查看）再执行，Worker 重领后从已完成 Case 续跑。

Candidate 验证逐 Case 写入检查点，按 `replay.concurrency`（1–2）并发执行 Case；每个 Case 的 Baseline 与 Candidate 并行执行。`GET /api/replays/skill/{run_id}` 返回 `total`、`completed`、`succeeded`、`failed` 及逐 Case 状态。分支超时记为失败并继续其他 Case，重领不重复执行已记录的 Case。绑定新 Adapter 不会自动重跑历史候选；需再次提交候选 `evaluate`，受理时固定新 revision。

Candidate 验证使用 2 个当次 Case 和最多 3 个历史 Case。当次 Case 由模型从至少 2 条不同 Session 中归纳典型共性任务模式，每个 Case 包含 6–8 条 Checklist，不逐条复制全部 Session。历史池包含进化数据集和标记 `metadata.enabled_for_evolution` 的回归集；普通实验集不进入验证。历史采样不重复、排除与当次相同的任务，优先覆盖不同数据集；不足 3 条时使用全部可用 Case。数据集 revision、材料和采样结果在受理时冻结，Worker 重试不重新采样。

候选评审详情的“所有数据集回放内容”展示每个数据集的结论、三项效率指标、全部 Case 的 A/B 对话、Checklist 证据、工具轨迹和产物。`replay.datasets` 保存逐数据集结果与冻结输入，`replay.sampling` 保存采样数量。

## 并发与限制

`replay` 模块配置（`tenant_configs`）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `concurrency` | 2 | 每租户同时运行的 Replay 槽位，由 `resource_leases` 控制 |
| `timeout_seconds` | 600 | 单分支超时 |
| `max_interactions` | 4 | 单分支最多交互轮次 |
| `llm_model_id` | 空 | 裁判与用户模拟器使用的模型，空则用租户模型配置 |

配置中不允许出现 Key 或密码类字段。

## Test Dataset

数据集为 `team-replay.dataset` 格式，顶层 `skills[]` 声明关联 Skill 集合，每个 Case 包含：

| 字段 | 说明 |
| --- | --- |
| `case_id` | Case 标识 |
| `query` | 首轮输入 |
| `skill_ids` | 本 Case 实际安装的 Skill 子集 |
| `checks` | Checklist 项：`id`、`text`、`kind`（`output` / `trajectory`），可选 `polarity: must_not` |
| `materials` | 输入文件 |
| `provenance` | 来源 Session 等 |
| `replay` | 可选 `progressive_disclosure.batch_size` |

数据集保存为不可变 revision（`dataset_revisions`），编辑使用 `expected_revision` 乐观锁。接口见 [ReplayAdapterFactory 与 Replay API](../api/05-replay-branch.md)。

## 相关文档

- [Checklist 门禁](./07-checklist.md)
- [进化闭环](./02-evolution-loop.md)
- [ReplayAdapterFactory 与 Replay API](../api/05-replay-branch.md)
