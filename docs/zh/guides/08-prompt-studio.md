# Prompt Studio 指南

Prompt Studio 是控制台「进化链路 → Skills 自进化」页面，用于查看 Skill 进化流水线、编辑各 LLM 阶段的 System Prompt 与模型参数，并用真实 Session 测试阶段输出。所有设置按租户保存在 `skills` 模块配置中，Worker 执行任务时使用受理时固定的配置 revision。

代码入口：`team_skills/api/prompts.py:create_router`（接口）、`team_skills/evolution/prompt_studio.py`（阶段目录与 Prompt 解析）、`team_skills/evolution/prompt_test.py`（阶段测试）、`web-ui/src/features/skills/PromptStudioView.tsx`（页面）。

## 流水线

```text
ingest → analyze_session → group
                           ├─ evolve_skill / create_skill ─ (按需 merge) ─┐
                           └─ dataset_synthesis ──────────────────────────┤
                                                                         ▼
                                          validate → replay_checklist → publish
analyze_session → analyze_trace_badcase
```

技能进化与测试集生成使用同一批冻结的 Session 和跨周期 Evidence 并行启动。测试集不读取本轮候选内容；候选技能和测试集均完成后才进入验证。页面将改进/新建标为条件路径，将验证标为汇合点。

| 节点 | 类型 | 说明 |
|------|------|------|
| `ingest` | 输入 | Session 经接入接口或数据源拉取进入 |
| `analyze_session` | LLM | 一次调用完成价值分类、轨迹摘要与会话评分 |
| `analyze_trace_badcase` | LLM | 对 Session 的全部 Trace 输出问题分析 |
| `group` | 逻辑 | 按使用的 Skill 分组，无 Skill 的 Session 归入 no-skill 组 |
| `evolve_skill` | LLM | 对已有 Skill 决定 improve、optimize_description、create 或 skip |
| `create_skill` | LLM | 从 no-skill 组判断是否存在可复用模式并生成新 Skill |
| `merge` | LLM | 同名 Skill 的两个进化版本冲突时合并 |
| `dataset_synthesis` | LLM | 与技能进化并行，从同源 Evidence 独立生成带 Checklist 的 Test Dataset |
| `validate` | 门禁 | 等待候选技能与测试集均完成后执行 True Replay，比较轮次、工具调用与 Token |
| `replay_checklist` | LLM | 依据回复、工具轨迹与工作区产物逐条判定 Checklist |
| `publish` | 输出 | 通过门禁的 Candidate 发布到 SkillHub |

进化流程见 [进化闭环](../concepts/02-evolution-loop.md)，门禁规则见 [Checklist](../concepts/07-checklist.md)。

## 可编辑阶段

| 阶段 ID | 默认 temperature | 默认 max_tokens |
|---------|------------------|-----------------|
| `analyze_session` | 0.1 | 32768 |
| `analyze_trace_badcase` | 0.1 | 8192 |
| `evolve_skill` | 0.4 | 16384 |
| `create_skill` | 0.4 | 16384 |
| `merge` | 0.3 | 8192 |
| `dataset_synthesis` | 0.3 | 16384 |
| `replay_checklist` | 0.0 | 8192 |

`evolve_skill`、`create_skill` 与 `merge` 以 Agent 循环运行：模型先规划，再按需调用工具加载证据，最后提交结果。

### 共享块

`evolve_skill` 与 `create_skill` 的模板包含共享块占位符，运行时展开为统一规则：

| 占位符 | 内容 |
|--------|------|
| `__GENERALIZATION_RULES__` | 泛化规则，避免把单次 Session 的细节写入 Skill |
| `__USER_OVERRIDE_RULE__` | 用户显式要求优先 |
| `__EVIDENCE_ROUTING_RULES__` | 证据加载规则 |
| `__OUTPUT_LANGUAGE_RULE__` | 输出语言规则 |

编辑器显示未展开的模板，保存后的覆盖同样在运行时展开。删除占位符会使该阶段不再包含对应规则。

## 设置项

每个阶段可单独覆盖：

| 字段 | 说明 |
|------|------|
| `prompt` | System Prompt 覆盖，不能为空 |
| `model` / `base_url` | 阶段专用模型，留空继承租户默认模型 |
| `temperature` / `max_tokens` | 采样参数 |
| `api_key` | 阶段专用 Key，加密保存为 Secret Ref，接口不回显；`clear_api_key=true` 清除 |

保存 `api_key` 需要服务配置 `TEAMEVOLVER_SECRET_MASTER_KEY`（或 `TEAMEVOLVER_ROOT_API_KEY`），否则返回 503。重置会同时删除该阶段的 Prompt 覆盖与模型设置。

流程参数在同页「过程参数」中修改：

| 字段 | 默认 | 说明 |
|------|------|------|
| `evolution_max_sessions` | 100 | 每轮进化最多消费的 Session 数 |
| `min_group_sessions` | 2 | Skill 分组进入进化的最少 Session 数 |
| `min_group_users` | 2 | Skill 分组进入进化的最少用户数 |
| `evolution_trigger_mode` | manual | 手动触发或定时自动触发 |
| `evolution_interval_seconds` | 600 | 自动触发间隔 |
| `bundle_allow_delete` | true | 是否允许进化删除 Bundle 文件 |
| `bundle_prompt_max_bytes` | 786432 | 进化时注入 Bundle 内容的总预算 |
| `bundle_edit_max_bytes` | 262144 | 单个文件编辑上限 |
| `auto_evaluate` | true | Candidate 生成后是否自动提交验证 |
| `publish_mode` | manual | 验证通过后自动发布或等待人工审核 |

## 测试阶段

1. 在左侧选择阶段，编辑 Prompt（可不保存）。
2. 从 Session 样本列表选择一条真实 Session。
3. 点击测试。请求以 `skill.evolve` 任务（操作 `prompt_test`）受理，返回 `job_id`。
4. Worker 用当前编辑的 Prompt 与阶段模型设置执行，结果包含 System Prompt、User Message 与模型输出；Agent 循环阶段最多运行 6 轮，并返回逐轮记录。

测试使用真实模型，不写入 Candidate，也不修改 Skill。启用链路观测时，测试调用以 `team_skills.evolution.prompt_test.<stage_id>` 上报。

## 接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/skill-evolution/pipeline` | 流水线节点与边 |
| GET | `/api/skill-evolution/prompts` | 所有阶段摘要，含是否覆盖、当前模型参数 |
| GET | `/api/skill-evolution/prompts/{stage_id}` | 阶段详情：默认模板、当前模板、展开后文本、变量 |
| PUT | `/api/skill-evolution/prompts/{stage_id}` | 保存阶段设置，可带 `expected_revision` |
| POST | `/api/skill-evolution/prompts/{stage_id}/reset` | 恢复默认 |
| GET | `/api/skill-evolution/settings` | 过程参数与 `revision` |
| PUT | `/api/skill-evolution/settings` | `{"evolve": {...}, "revision"}` 保存过程参数 |
| GET | `/api/skill-evolution/session-samples?limit=` | 可用于测试的 Session（1–200） |
| POST | `/api/skill-evolution/prompts/{stage_id}/test` | `{"session_id", "prompt"?}` 受理测试，返回 202 |

写操作要求租户管理员。配置写入使用 revision 比较，并发修改返回冲突，需刷新后重试。未知阶段返回 404，不支持的字段返回 400。

## 使用建议

- 先在测试面板对比修改前后的输出，再保存
- 一次只调整一个阶段，观察下一轮进化的 Candidate 与 True Replay 结果
- `replay_checklist` 是验证裁判，提高 temperature 会降低判定稳定性
- 修改 `analyze_session` 输出结构前确认下游解析字段不变

## 相关文档

- [进化闭环](../concepts/02-evolution-loop.md)
- [链路观测指南](./04-observability.md)
- [Web 控制台用户指南](./03-console.md)
