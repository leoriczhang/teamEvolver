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
{"session_ids": ["session_..."]}
```

`session_ids` 最多 200 个；省略时读取受理时刻之前入库、尚未被进化消费的 `valuable` Session，最多 `evolution_max_sessions` 个（上限 200）。请求不能覆盖身份或配置。创建 `skill.evolve` 任务，固定当前 `skills` 模块配置 revision；任务首次运行时冻结输入 Session 集合，恢复执行使用同一集合。

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

### POST /api/skill-candidates/{candidate_id}/validate

```json
{"mode": "auto", "candidate_revision": 2}
```

| mode | 行为 |
| --- | --- |
| `auto` | 静态检查通过、Replay 运行正常且 verdict 为 `accept` 时发布；verdict 为 `reject` 时拒绝；`inconclusive` 时回到 `open` |
| `force` | 静态检查通过且 Replay 运行正常即发布，不要求 `accept` |

验证固定当前绑定的 Replay Adapter revision 与 `replay` 模块配置。只有 `open` 状态的 Candidate 可以提交。

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
