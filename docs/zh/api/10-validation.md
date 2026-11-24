# 候选与进化 API

Skill 进化支持管理员手动触发或 Worker 定时自动触发：Worker 读取有价值的 Session，按 Skill 分组规划修改并生成 Skill Candidate。Candidate 可自动提交候选验证；验证通过后按租户策略自动发布，或等待管理员人工审核发布。流程见 [进化循环](../concepts/02-evolution-loop.md)。

进化规划同时读取同租户的待发布 Candidate 目录，包括名称、revision、来源 Agent 范围和验证反馈。规划器通过 `read_candidate` 按需读取完整正文；同能力的新 Evidence 可以续写尚无已发布版本的 Candidate。多个重叠提案可选择 `merge_skill`，指定 `target_candidate_id` 与 `source_candidate_ids`，由独立 Merge 阶段生成合并结果。仅改名不能作为新建同能力 Skill 的理由。

新建前必须成功读取同来源 Agent 范围的现有 Candidate；读取失败只能纠正标识或跳过，不能作为换名新建的理由。上下文压缩后仍保留原样的 Candidate 标识和原始系统契约。规划轮次、Candidate 工具读取结果和决策校验错误保存在 `agent_audit` 中供核查。

合并保持目标名称和 Candidate ID，递增 revision 并清除旧验证结论；成功后来源 Candidate 归档，保留文件、验证结果及 `decision.merged_into`。目标写入与来源归档在同一事务内完成，所有输入的状态、revision、文件树哈希和基线均受并发校验保护。不同来源 Agent 范围、正在验证的 Candidate，以及其他已发布 Skill 的更新候选不能作为合并来源。非冲突配套文件保留，同路径内容冲突必须显式解决。

分析与规划携带原始 Agent 系统契约和输入模态的可见性说明。文本分析未取得视频、图片或音频内容时，不得把未验证的视觉/OCR 事实直接判为编造。Test Dataset 继续独立基于 Session Evidence 合成，不读取待发布候选正文或本轮 Merge 结果。手动 `reprocess` 复用已入库分析；原始契约用于核对历史摘要，不代表重新运行 Session Judge。

验证受理中的 MA 连接、执行指纹、来源快照及目标成员查询复用当前事务连接，避免持有事务时再次借用连接而耗尽连接池。独立调用这些只读查询时仍使用自己的短事务。

代码入口：

- Candidate 与进化：`team_skills/api/candidates.py:create_router`
- Prompt 与进化设置：`team_skills/api/prompts.py:create_router`
- 评估与发布：`team_skills/evolution/candidate_pipeline.py:run_validation`
- Candidate 状态：`team_skills/candidates/repository.py:CandidateRepository`

读取接口要求租户成员；写入、触发与 Prompt 修改要求租户管理员。受理接口返回 `{"status": "accepted", "job_id", "status_url"}`，支持 `Idempotency-Key`。

## 进化触发

### POST /api/skill-evolution/trigger

```json
{"session_ids": ["session_..."]}
```

`session_ids` 最多 200 个；省略时读取受理时刻之前入库、尚未被进化消费的 `valuable` Session，最多 `evolution_max_sessions` 个（上限 200）。请求不能覆盖身份或配置。创建 `skill.evolve` 任务，固定当前 `skills` 模块配置 revision；任务首次运行时冻结输入 Session 集合，恢复执行使用同一集合。

显式传入 `{"session_ids":["session_..."],"reprocess":true}` 可重新使用已消费或手动更新前的指定
Session。必须提供非空 ID 列表，仍要求已有强制分析和内容哈希一致。只为本次候选固定指定
Evidence 和同源 Case，不恢复已归档的旧候选、Evidence 或自动数据集；新候选使用当前有效
文件包，规划期间的版本、重置边界和候选 revision 并发保护仍生效。

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

控制台列表使用 `compact=true`。已完成评估的 `evaluation.summary_only=true`，保留 verdict、效率指标、Checklist 完成数量和首个回放错误，省略 Case 正文、Session 快照、产物和重复数据集结果；完整证据通过详情接口读取。轮询摘要不会覆盖已加载的同 revision 完整评估。

`validating` 候选包含 `replay_progress`：`total`、`completed`、`succeeded`、`failed`。进度来自逐 Case 检查点；`succeeded` 表示双方完成 Checklist，不等于候选的效率结论为 `accept`。

### GET /api/skill-candidates/{candidate_id}/detail

Candidate 详情：Skill 名称、基线版本、revision、bundle、变更说明、证据 Session、最近一次评估结果与事件历史。

`include_replay` 默认 `true`，返回完整回放。控制台首次打开使用 `include_replay=false`，保留技能正文、差异、Checklist 逐项判定和客观结论，省略完整回放轨迹；`evaluation.replay_details_omitted=true` 表示可按需加载完整回放。详情中的存量评估标记为 `cached=true`。轮询仅复用 revision 与 `updated_at` 均一致的详情；详情加载中或请求失败时显示明确状态。

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

只评估不发布：创建 `skill.validate` 任务，运行静态检查与候选验证，写入 verdict（`accept`、`reject`、`inconclusive`）后 Candidate 回到 `open`。

请求可为空，或传 `{"validation_method":"auto"}`。方式为 `auto`（默认，由 Worker 的 LLM 根据
具体差异与当前 Evidence 选择）、`experience_review`、`candidate_execution`、`full_ab`。
审阅不足以证明执行行为时不通过。经验审阅返回 Evidence 引用与范围，不要求 Runtime 通过。
候选执行选择一个相关 Case，仅运行 Candidate；分阶段与完整交付明确区分。
`full_ab` 保留隔离双分支与效率比较规则。所有模型与执行工作仍在 Worker 中运行，受理返回 202。

`evaluation.replay` 保存 `validation_method`、`validation_scope`、`basic_validation`、
`candidate_completed`、`ab_completed`、`ab_verdict` 和 `assessment`。
`candidate_completed` 与原 `succeeded`（双方完成）分开。基础通过不改写历史 A/B 结论。

### POST /api/skill-candidates/{candidate_id}/validate

```json
{"mode": "auto", "candidate_revision": 2, "validation_method": "auto"}
```

| mode | 行为 |
| --- | --- |
| `auto` | 静态检查与所选验证方式的门禁通过且 verdict 为 `accept` 时发布；`reject` 时拒绝；`inconclusive` 时回到 `open` |
| `force` | 管理员直接采纳并发布当前候选，不重新调用评估模型、不创建候选执行或 A/B Session，也不要求执行门禁通过。仍检查权限、候选 revision、静态内容及基线版本，并等待云端发布回读。原验证结论保持不变；越过未通过或未运行的验证时记录 `validation_overridden=true`，其中经验审阅同时记录 `review_overridden=true`。界面显示发布进度，不显示回放用例进度 |

验证固定当前 Evidence、候选文件树、Replay Adapter revision 与 `replay` 模块配置。执行配置
缺失不阻止受理经验审阅，只有执行方式才消费 MA 资源门禁。发布默认沿用已评估的验证方式，
相同候选与执行指纹的完成记录直接复用。只有 `open` 状态的 Candidate 可以提交。

强制采纳经验审阅时复用当前候选 revision 的已保存审阅，不重复调用模型。发布为独立后台操作，只有版本实际写入并回读后才显示发布成功；受门禁阻止时保留详情并显示原因。

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
| `min_group_sessions` | 2 | 一个 Skill 分组至少需要的 Session 数 |
| `min_group_users` | 2 | 一个 Skill 分组至少涉及的用户数 |
| `evolution_trigger_mode` | manual | 手动或定时自动触发进化 |
| `evolution_interval_seconds` | 600 | 自动触发间隔，60–604800 秒 |
| `bundle_allow_delete` | true | 允许进化删除 bundle 文件 |
| `bundle_edit_max_bytes` | 262144 | 单个可编辑文件上限 |
| `bundle_prompt_max_bytes` | 786432 | 放入 Prompt 的 bundle 上限 |
| `auto_evaluate` | true | Candidate 生成后自动提交评估 |
| `assessment_timeout_seconds` | 600 | `validation` 下的模型审阅总时限，整数 30–1800 秒 |
| `publish_mode` | manual | 验证通过后自动发布或等待人工审核 |

`assessment_timeout_seconds` 按租户保存，随验证命令的配置 revision 固定；修改仅影响新受理任务。
预算包含模型排队、HTTP 请求和重试。每次 HTTP 请求使用扣除排队与先前尝试后的剩余预算，并预留最多 5 秒清理时间。
同步 HTTP 线程不能被协程取消强制终止，超时后仍占用并发槽直到请求实际结束；不会提前释放槽位。
此参数只控制策略选择与经验审阅，不改变 Candidate Execution 或 True Replay 的执行时限。
审阅输入中的相同原始契约仅保留一份，Evidence 通过 `source_contract_ref` 引用 `source_contracts`；
各 Evidence 的 ID、摘要、不同契约和素材可见性仍分别保留。

Prompt Studio 的使用方法见 [Prompt Studio 指南](../guides/08-prompt-studio.md)。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | 字段非法、`session_ids` 超过 200、未知 Prompt 设置 |
| 403 | 非租户管理员 |
| 404 | Candidate、命令或阶段不存在 |
| 409 | `candidate_revision` 冲突、Candidate 不在 `open` 状态、`Idempotency-Key` 被用于不同请求、配置 revision 冲突 |
| 503 | 租户 `skills` 模块配置缺失，或存储凭证时缺少主密钥 |
