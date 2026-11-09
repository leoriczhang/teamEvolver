# 候选与进化 API

Skill 进化支持管理员手动触发或 Worker 定时自动触发：Worker 读取有价值的 Session，按 Skill 分组规划修改并生成 Skill Candidate。Candidate 可自动提交 True Replay；验证通过后按租户策略自动发布，或等待管理员人工审核发布。流程见 [进化循环](../concepts/02-evolution-loop.md)。

代码入口：

- Candidate 与进化：`team_skills/api/candidates.py:create_router`
- Prompt 与进化设置：`team_skills/api/prompts.py:create_router`
- 评估与发布：`team_skills/evolution/candidate_pipeline.py:run_validation`
- Candidate 状态：`team_skills/candidates/repository.py:CandidateRepository`

读取接口要求租户成员；写入、触发与 Prompt 修改要求租户管理员。受理接口返回 `{"status": "accepted", "job_id", "status_url"}`，支持 `Idempotency-Key`。

## 进化触发

### POST /api/skill-evolution/trigger

```json
{"session_ids": ["session_..."], "allowed_modules": ["skill", "prompt"], "execution_profile_id": "business-agent"}
```

`session_ids` 最多 200 个；省略时读取受理时刻之前入库、尚未被进化消费的 `valuable` Session，最多 `evolution_max_sessions` 个（上限 200）。请求不能覆盖身份或配置。创建 `skill.evolve` 任务，固定当前 `skills` 模块配置 revision；任务首次运行时冻结输入 Session 集合，恢复执行使用同一集合。

`allowed_modules` 为非空多选数组，仅支持 `skill`、`prompt`；省略时使用当前计划，历史任务缺失字段按 Skill-only 解释。`execution_profile_id` 对应已登记的业务 Agent 固定组合，选择 Prompt 时在受理时冻结。单次选择不写回计划设置。业务 Prompt、注入组件、外部复核、共享回归和整体回滚接口见 [组合进化](../concepts/12-execution-evolution.md)；TE 的分析、生成及裁判 Prompt 仍由进化引擎管理。

### GET /api/skill-evolution/jobs/{job_id}/result

```json
{"job_id": "evolve:...", "result": {}}
```

### GET /history

进化周期历史，见 [Session 查询](./08-sessions-api.md#进化历史)。

## Skill Candidate

状态：`open`（待审阅）→ `validating`（评估或验证中）→ `published` / `rejected`；验证结果不确定时回到 `open`。

### GET /api/skill-candidates

| 参数 | 说明 |
| --- | --- |
| `scope` | `open`（默认，含 `validating`）、`processed`、`all` |
| `limit` | 1–200，默认 20 |
| `offset` | ≥0 |
| `compact` | `true` 时省略大字段 |

返回 `{"candidates", "total", "limit", "offset", "has_more"}`。

`validating` 候选包含 `replay_progress`：`total`、`completed`、`succeeded`、`failed`。进度来自逐 Case 检查点；`succeeded` 表示双方完成 Checklist，不等于候选的效率结论为 `accept`。

### GET /api/skill-candidates/{candidate_id}/detail

Candidate 详情：Skill 名称、基线版本、revision、bundle、变更说明、证据 Session、最近一次评估结果与事件历史。

### PUT /api/skill-candidates/{candidate_id}/content

```json
{"description": "...", "category": "general", "content": "...", "candidate_revision": 2}
```

修改 Candidate 的 `SKILL.md`，生成新的 `candidate_revision`。不能改名；`candidate_revision` 与当前不一致返回 409。

### PATCH /api/skill-candidates/{candidate_id}/feedback

```json
{"reviewed": true}
```

标记是否已审阅，只接受 `reviewed`。

### POST /api/skill-candidates/{candidate_id}/evaluate

只评估不发布：创建 `skill.validate` 任务，运行静态检查与 True Replay，写入 verdict（`accept`、`reject`、`inconclusive`）后 Candidate 回到 `open`。

同一轮 Checklist 裁判使用 `checklist-format-retry-v1` 策略，最多进行 2 次 provider 调用。只有已证实的 JSON 解析、schema 校验或输出截断等格式失败，才允许补 1 次；补调用使用相同的 Checklist、证据、模型和请求参数，不重新发送 Agent 请求。有效的负面判定、provider 错误、超时、预算不足、输入无效及没有格式失败证据的通用不可用结果均不触发此重试。

每次实际调用的用量都计入累计成本，缺失用量不能按 0 处理。结果仍须通过严格校验，不修补 JSON 或放宽 Checklist 标准；补调用仍失败则裁判不可用，验证保持 fail closed。历史验证结果不回写。

### POST /api/skill-candidates/{candidate_id}/validate

```json
{"mode": "auto", "candidate_revision": 2}
```

| mode | 行为 |
| --- | --- |
| `auto` | 静态检查通过、Replay 运行正常且 verdict 为 `accept` 时发布；verdict 为 `reject` 时拒绝；`inconclusive` 时回到 `open` |
| `force` | 静态检查通过且 Replay 运行正常即发布，不要求 `accept` |

验证固定当前绑定的 Replay Adapter revision 与 `replay` 模块配置。只有 `open` 状态的 Candidate 可以提交。

上表描述 Skill-only 路径。包含业务执行计划的 Candidate 先 `/evaluate`，再由 `/validate` 发布该次不可变结果，不另跑模型。新验证默认 `standard-v1`：沿用 Skill 的静态、运行和原生主回放判断，保留 Prompt 实际加载、组合及依赖版本检查，不强制锁定集、A/A、重复探测或独立复核。显式 `environment.validation.strategy="enhanced-v1"` 仍要求原增强检查和绑定精确结果的 v2 独立复核；旧冻结计划缺少策略标记时仍按原增强规则执行，采用新默认需重新评估。`auto`、`force` 均不能绕过 Prompt 主回放或冻结策略门禁。配置/资产变化返回冲突，受理后变化由 Worker 阻断；历史结果和费用不回写。详见 [组合进化](../concepts/12-execution-evolution.md)。

### POST /api/skill-candidates/{candidate_id}/validation-budget

```json
{"candidate_revision": 18, "max_total_tokens": null, "reason": "管理员记录的明确取消累计 Token 上限授权"}
```

仅管理员可修改开放且包含执行计划的 Candidate，返回更新后的详情（200）。必须提供这三个字段；`candidate_revision` 为当前正整数 revision，通过 CAS 写入，不一致返回 409。`max_total_tokens` 为比当前有限预算更大的整数（最多 10000000），或显式 JSON `null`，表示取消该 Candidate 的累计 Token 上限；布尔、非正数、字符串、越界数和省略字段返回 400。`reason` 必须非空且不超过 2000 字符。当前已经无上限时拒绝再次修改，也拒绝意外写回有限值。

修改产生新 revision 与审计事件（取消为 `validation_budget_unlimited`），要求重新评估，并按冻结策略决定是否需要精确结果复核。全部历史费用保留；执行计划和模型参数不变。无上限仅取消累计 Token 阈值，历史未知用量作为统计提示；Checklist、加载观测和冻结策略要求的发布门禁保持，显式有限预算和旧任务规则不变。见 [组合进化](../concepts/12-execution-evolution.md)。

### POST /api/skill-candidates/{candidate_id}/reject

人工拒绝，立即变为 `rejected`。关联的成功经验 Pattern 会记录该结果。

## Prompt 与进化设置

Prompt 覆盖与阶段模型设置保存在租户 `skills` 模块配置的 `prompt_overrides` 与 `stage_settings`，所有写入使用 revision 乐观锁。

| 阶段 ID | 用途 |
| --- | --- |
| `analyze_session` | Session Analyze（价值分类、摘要、评分） |
| `analyze_trace_badcase` | Trace Analyze |
| `evolve_skill` | 修改已有 Skill |
| `create_skill` | 创建新 Skill |
| `merge` | 合并修改 |
| `dataset_synthesis` | 合成 Replay 数据集 |
| `replay_checklist` | Replay Checklist 生成与裁判 |

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/skill-evolution/pipeline` | 进化链路图 |
| GET | `/api/skill-evolution/prompts` | 全部阶段的当前 Prompt |
| GET | `/api/skill-evolution/prompts/{stage_id}` | 单个阶段，未知阶段返回 404 |
| PUT | `/api/skill-evolution/prompts/{stage_id}` | 修改 `prompt`、`model`、`base_url`、`temperature`、`max_tokens`、`api_key`、`clear_api_key`，可带 `expected_revision` |
| POST | `/api/skill-evolution/prompts/{stage_id}/reset` | 删除该阶段的覆盖 |
| POST | `/api/skill-evolution/prompts/{stage_id}/test` | `{"session_id", "prompt"?}` 用一个 Session 测试 Prompt，返回 202 |
| GET | `/api/skill-evolution/session-samples?limit=50` | 可用于测试的有价值 Session（最多 200） |
| GET | `/api/skill-evolution/settings` | 进化参数与 `revision` |
| PUT | `/api/skill-evolution/settings` | `{"evolve": {...}, "revision": N}` |

阶段 `api_key` 以 Secret Ref 保存，需要 `TEAMEVOLVER_SECRET_MASTER_KEY`（未设置时回退使用 `TEAMEVOLVER_ROOT_API_KEY` 作为主密钥）；两者都未设置返回 503。

`settings.evolve` 与 `settings.validation` 可修改的字段：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `evolution_max_sessions` | 100 | 单次进化最多读取的 Session 数 |
| `evolution_current_case_count` | 2 | 当次 Replay Case 生成数量，整数 1–6；使用受理时冻结的配置，原始锚点归并后实际数量可能更少 |
| `evolution_historical_case_count` | 3 | 历史 Replay Case 采样上限，整数 1–30；沿用去重及优先跨数据集的策略，验证受理时冻结 |
| `min_group_sessions` | 2 | 一个 Skill 分组至少需要的 Session 数 |
| `min_group_users` | 2 | 一个 Skill 分组至少涉及的用户数 |
| `evolution_trigger_mode` | manual | 手动或定时自动触发进化 |
| `evolution_interval_seconds` | 600 | 自动触发间隔，60–604800 秒 |
| `bundle_allow_delete` | true | 允许进化删除 bundle 文件 |
| `bundle_edit_max_bytes` | 262144 | 单个可编辑文件上限 |
| `bundle_prompt_max_bytes` | 786432 | 放入 Prompt 的 bundle 上限 |
| `auto_evaluate` | true | Candidate 生成后自动提交评估 |
| `publish_mode` | manual | 验证通过后自动发布或等待人工审核 |

Prompt Studio 的使用方法见 [Prompt Studio 指南](../guides/08-prompt-studio.md)。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | 字段非法、`session_ids` 超过 200、未知 Prompt 设置 |
| 403 | 非租户管理员 |
| 404 | Candidate、命令或阶段不存在 |
| 409 | `candidate_revision` 冲突、Candidate 不在 `open` 状态、`Idempotency-Key` 被用于不同请求、配置 revision 冲突 |
| 503 | 租户 `skills` 模块配置缺失，或存储凭证时缺少主密钥 |
