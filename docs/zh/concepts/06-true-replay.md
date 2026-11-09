# True Replay

True Replay 对每个 Test Dataset Case 打开相互隔离的 Baseline 与 Candidate 会话，使用相同的 query、材料、Context、模型、工具和限制，唯一差异是 treatment（被验证的 Skill 集合、业务 Prompt／上下文目标组件，或 Memory 内容）。纯 Prompt 组合可有 0 个 Skill，仍须验证完整执行计划中的实际目标文本与非目标环境，见 [业务 Prompt 与上下文进化](./12-execution-evolution.md)。

## 执行过程

每个分支由 `team_replay/execution.py:run_branch` 执行：

1. 租户绑定的 ReplayAdapterFactory 打开独立会话；第一轮只发送数据集 query。
2. 收集真实 Agent 回复、轨迹、产物和指标（`AgentObservation`）。
3. 独立裁判（`team_replay/judging.py:judge_checklist`）按 Checklist 给出逐项证据，判定完成门禁。
4. 未完成时，`team_replay/policy.py:select_disclosure_items` 确定性选择本轮允许披露的未满足项（默认每批 4 项），独立用户模拟器（`render_user_feedback`）生成自然反馈。
5. 反馈写入 user 历史并继续执行；全部满足或达到 `max_interactions` 后结束并关闭会话。

客户适配器接触不到 Checklist、裁判状态或未披露要求。反馈只表达当前目标与缺口，不含条目 ID、评分和 A/B 内部用语。裁判不可用时 fail closed。

同一轮裁判对同一份证据采用 `checklist-format-retry-v1`：最多 2 次 provider 调用，仅在已证实的 JSON 解析、schema 校验或截断格式失败后补 1 次。Checklist、证据、模型和请求参数保持相同，不重发 Agent 请求。有效的负面判定、provider 错误、超时、预算不足、输入无效，以及无格式失败证据的通用不可用结果均不重试。每次实际调用的用量累计计费，缺失用量不补 0；严格校验、不修补 JSON 和原有完成标准保持不变，历史结果不回写。

## 判定

默认采用 Checklist 与效率模式，`team_replay/policy.py:progressive_replay_decision` 先看 Checklist，再看效率：

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

## 可选外部评分

控制台的自进化链路与独立实验工作台提供“回放评价方式”配置。默认保留 Checklist 与效率模式，也可选择外部评价 Agent，配置 HTTP 地址、评分要求、凭证 Secret Ref、超时、候选最低均分与最小提升幅度。仅租户管理员可保存，修改只影响之后提交的任务。

外部模式仍实际运行 Baseline 与 Candidate，保留真实轮次、工具和 Token 记录。执行结束后，每个 Case 把相同的冻结任务输入、变量、公共上下文及两个分支的真实输出与轨迹交给外部 Agent；原评分、参考答案和候选编辑材料不作为评分请求输入。接口协议见 [Replay API](../api/05-replay-branch.md)。

评分固定为 0–100，越高越好。全部所选 Case 等权求均分；候选均分达到最低分，且相对 Baseline 有正提升、提升幅度达到配置值时通过。均分下降或未达到要求时未通过；完全持平时无法确定。历史与当次 Case 使用相同评分方式。每个数据集也单独展示其均分，整体按全部 Case 汇总，不按数据集数量投票，也不套用默认模式的单 Case 效率否决。多个必需运行环境仍分别验证。

外部模式下，`max_interactions=1` 只执行原始 Query，不调用内置 Checklist 裁判或反馈生成器，Checklist 显式记为 `not_run`，不能记为通过。配置多轮时继续保留 Checklist 的渐进反馈；辅助裁判或反馈不可用时停止追加反馈，把已经取得的真实执行结果交给外部 Agent 评分，不伪造完成性。默认 Checklist 与效率模式仍严格要求内置裁判可用。外部模式不以 Checklist 是否完成或 Token 是否增加决定采纳；真实业务执行失败、候选未实际加载、评分请求失败、缺少任一分支分数、非法分数或缺失 Case，仍不能判为通过，不自动退回默认模式。单分支数据集运行没有 A/B 比较，继续显示原完成情况。

模式、阈值、评分要求与接口配置随任务固定；报告保留请求哈希、逐 Case 分数和理由、均分与差值。旧任务及历史结果不因配置变化而重新打分。外部评价 Agent 的用量独立记录，缺失时保留未知状态，不计入业务 Agent 的效率 Token，也不冒充 TE 已核算的模型用量。

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

Candidate 验证默认生成 2 个当次 Case、采样最多 3 个历史 Case。租户 `skills` 配置可通过 `evolution_current_case_count`（1–6，默认 2）和 `evolution_historical_case_count`（1–30，默认 3）调整数量，不改变题目生成或历史采样策略。当次 Case 由模型从至少 2 条不同 Session 中归纳典型共性任务模式，每个 Case 包含 6–8 条 Checklist，不逐条复制全部 Session；原始锚点归并后有效 Case 数可能少于生成数量。切分片段按 `source_session_keys` 合并计数，同一原始来源的多个片段不算多个独立来源。历史池包含进化数据集和标记 `metadata.enabled_for_evolution` 的回归集；普通实验集不进入验证。历史采样不重复、排除与当次相同的任务，优先覆盖不同数据集；不足配置数量时使用全部可用 Case。生成使用受理时冻结的配置，验证受理时冻结历史采样数量、数据集 revision、材料和采样结果，Worker 重试不重新采样。已有命令缺少新配置时继续使用原默认数量。

显式声明为离线导入批次的记录不建立真实会话连续关系。此类 Case 仍跨来源归纳 Checklist，但模型必须选择一个原始请求锚点，保持该请求及其自带历史的原文；不得把相邻记录拼成历史，或改变业务条件的首次／再次触发状态。生成结果缺少有效锚点、改写原请求或重复选择同一锚点时会失败，不以摘要兜底生成另一道题。原请求哈希随 Case 保存，并在 Test Dataset 冻结时复核。

业务 Prompt 或显式离线批次的合成输入另外携带完整目标 Evidence 与来源说明，包括原回答和声明的外部评价。评分原文及可解析的 JSON 并存，保留冲突和未知项；这些评分不是 TE Judge，也不直接变成 Checklist 分数。完整 Evidence 超出模型输入预算时使用无损字符串共享，仍超限则明确失败，不静默截断。普通 Skill Session 继续使用既有合成路径。

已验证来源的 `analysis_context` 可携带 Context-only 对照材料，帮助保留原有成功行为。它只在明确声明的离线批次中补充完整材料，不改变目标 Evidence、价值判断、来源计数或 Case 锚点；目标与对照去重，来源或正文冲突时明确失败。对照中的评分仍是原始外部评价。

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
