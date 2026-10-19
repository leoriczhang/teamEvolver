# 进化闭环

进化闭环把 Agent 的真实工作转化为团队 Skill 版本：Session 接入 → 强制分析 → Skill Evolution 生成 Skill Candidate → True Replay 验证 → Candidate Review 与 Publish → Agent 拉取。每一步都是持久化的 Worker 任务，任何 API 或 Worker 副本都可以接手。

## 闭环总览

```text
 Agent push / 数据源 pull
        │ 202 + session.normalize
        ▼
 normalize → split → analyze → trace_analyze → archive ──► 正式 Session + Evidence
                                                     │ POST /api/skill-evolution/trigger
                                                     ▼
                                     skill.evolve：按 Skill 分组规划 → Skill Candidate
                                                     │ auto_evaluate 或手动 evaluate/validate
                                                     ▼
                                     skill.validate：静态检查 + True Replay
                                                     │ accept / reject / inconclusive
                                                     ▼
                         Candidate Review ──► Publish（新版本）──► GET /sync/skills
```

## 阶段详解

### 1. Session 接入

Agent 通过 `POST /ingest_session` 推送 v2 Session，或管理员通过数据源拉取。API 在一个事务内写入 `ingestions`、原始 payload 和 `session.normalize`（或 `session.pull`）任务，返回 `202`。相同 `session_id` 的重复推送映射到第一次受理。详见 [Session 体系](./05-sessions.md)。

### 2. 强制分析

Worker 依次执行 `normalize`、`split`、`analyze`、`trace_analyze`、`archive`，每个阶段写一行 `session_stage_results`。`split` 用一次模型调用识别连续的话题边界；多话题 Session 会先生成带来源元数据的子 ingestion，每个子段再独立执行完整链路。切分计划在 fan-out 前持久化，因此重试会复用同一组边界。只有五个阶段全部成功后，`archive` 才写入正式 `sessions` 行；任何阶段失败都不产生正式 Session 或 Evidence。

`analyze` 由 `team_skills/evolution/stages/analyze.py:analyze_session` 在一次模型调用中完成价值分类、摘要和四维评分：

| 维度 | 权重 | 含义 |
|------|------|------|
| `task_completion` | 0.55 | 用户目标是否完成 |
| `response_quality` | 0.30 | 最终结果的正确性、完整性与清晰度 |
| `tool_usage` | 0.10 | 工具使用是否恰当且有效 |
| `efficiency` | 0.05 | 执行路径是否避免了不必要的重试或绕路 |

系统提示词为 `_ANALYZE_SYSTEM`，可在 Prompt Studio 中按租户覆盖。

### 3. Skill Evolution

管理员调用 `POST /api/skill-evolution/trigger`（可选 `session_ids`，最多 200 个），API 写入 `skill.evolve` 命令并固定当时的 `skills` 模块配置 revision。Worker 执行 `team_skills/evolution/cycle.py`：

- **冻结输入**：首次执行时按 `evolution_max_sessions`（默认 100，上限 200）选出受理时刻之前入库、尚未被消费的 Session，并保存输入集合；中断后续跑使用同一批 Session。
- **分组**：按关联 Skill 分组，每组是独立分支；无关联 Skill 的 Session 进入新建分支。
- **团队证据门槛**：每个分支的证据至少覆盖 `min_group_sessions`（默认 2）个 Session 和 `min_group_users`（默认 2）个用户，否则本轮跳过该分支。
- **规划**：模型基于分组 Session、跨周期 Evidence 和成功经验模式给出 `skip`、修订或新建决策。证据路由规则为 `team_skills/evolution/stages/execute.py` 中的 `_EVIDENCE_ROUTING_RULES`，只有 `team_skill` 证据可以修改共享 Skill。
- **并行生成测试集**：满足证据门槛后，规划与 Test Dataset 生成同时启动，使用同一批 Evidence；测试集不读取本轮候选内容或候选分类结果。两边完成后才写入 Candidate 并提交验证。规划跳过时取消尚未完成的测试集生成；分支失败不消费本轮 Evidence，重试仍使用冻结输入。
- **生成 Candidate**：写入 `skill_candidates` 及其文件，记录 Evidence Classification、编辑摘要、文件变更、bundle diff 和静态检查结果；不影响已发布版本。规划期间若有人修改了同一 Skill 的 Candidate，本次提案作废。

### 4. Test Dataset

Test Dataset 与 Skill 规划并行、独立生成。每个 Candidate 携带由同源 Evidence 归纳的两个 Replay Case，每个 Case 有 `query`、`skill_ids`、6–8 项 `checks`（Checklist）和 `materials`。新 Skill 的名称在规划结束后绑定到 Case；只有两边均完成才进入 True Replay。也可以在「数据集」页面从 Session 选择或合成数据集，见 [True Replay](./06-true-replay.md)。

### 5. 验证

`skills` 配置的 `auto_evaluate`（默认 `true`）会在 Candidate 生成后自动提交 `evaluate`；管理员也可以调用：

| 接口 | mode | 结果 |
| --- | --- | --- |
| `POST /api/skill-candidates/{id}/evaluate` | `evaluate` | 只评估，不发布 |
| `POST /api/skill-candidates/{id}/validate` `{"mode":"auto"}` | `auto` | 静态检查通过、运行时门禁通过且 verdict 为 accept 时发布；reject 时拒绝 |
| `POST /api/skill-candidates/{id}/validate` `{"mode":"force"}` | `force` | 静态检查和运行时门禁通过时发布，不要求 accept |

`skill.validate` 固定 Candidate revision、Baseline 版本、`replay` 配置 revision 和租户绑定的 Replay Adapter revision，然后执行静态检查和 True Replay。静态检查不通过时 verdict 一律为 reject；运行时门禁未通过时 accept 降为 inconclusive。

### 6. Candidate Review 与 Publish

管理员在控制台查看 Evidence、diff、静态检查和 True Replay 结果，可以：

- 编辑内容：`PUT /api/skill-candidates/{id}/content`（携带 `candidate_revision` 做乐观锁）
- 标记反馈：`PATCH /api/skill-candidates/{id}/feedback`
- 拒绝：`POST /api/skill-candidates/{id}/reject`
- 发布：`validate` 的 `auto` 或 `force` 模式

Publish 在同一事务内写入新的 `skill_versions`、文件和 `skill_mutations`，详见 [发布与回滚](./08-publish-rollback.md)。

### 7. 分发

Agent 在模型调用前请求 `GET /sync/skills?user_id=...`，携带 `If-None-Match` 时未变化返回 `304`。Skill 分发只有这一种 pull 方式，发布不向 Agent 推送。

### 8. Rollback

`POST /api/skills/{name}/rollback` 以新版本恢复历史内容，保留全部旧版本和审计链。

## 进化触发

| 方式 | 说明 |
|------|------|
| 手动触发 | 控制台或 `POST /api/skill-evolution/trigger`，可指定 `session_ids` |
| 自动触发 | `evolution_trigger_mode=automatic` 时，Worker 按 `evolution_interval_seconds` 检查并消费未进化的 Session |
| 自动评估 | `auto_evaluate=true` 时，每个新 Candidate 自动进入 `evaluate` |
| 成功经验 | Worker 周期扫描已分析 Session，维护经验库与经验模式，并作为后续规划上下文，见 [成功经验同步](../guides/16-successful-experience-sync.md) |

自动触发由 PostgreSQL advisory lock 和持久化任务去重，多 Worker 不会为同一租户重复受理；手动触发始终可用。

## 进化设置

`GET/PUT /api/skill-evolution/settings` 读写 `skills` 模块配置中的下列字段（`revision` 乐观锁）：

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `evolution_max_sessions` | 100 | 单次进化最多消费的 Session 数（上限 200） |
| `min_group_sessions` | 2 | 分支最少 Session 数，0 表示不检查 |
| `min_group_users` | 2 | 分支最少用户数，0 表示不检查 |
| `evolution_trigger_mode` | manual | `manual` 手动触发或 `automatic` 定时自动触发 |
| `evolution_interval_seconds` | 600 | 自动触发检查间隔，60–604800 秒 |
| `auto_evaluate` | true | 新 Candidate 是否自动评估 |
| `publish_mode` | manual | `manual` 验证通过后等待人工审核，`automatic` 验证通过后自动发布 |
| `bundle_allow_delete` | true | 进化是否允许删除 bundle 文件 |
| `bundle_edit_max_bytes` | 262144 | 单文件可编辑上限 |
| `bundle_prompt_max_bytes` | 786432 | 送入模型的 bundle 上限 |

## 相关文档

- [Skill 体系](./03-skills.md)：Skill 的结构、版本、生命周期
- [True Replay](./06-true-replay.md)：验证机制
- [Checklist 门禁](./07-checklist.md)：完成性判定规则
- [发布与回滚](./08-publish-rollback.md)：版本管理和审计
- [验证与 Candidate API](../api/10-validation.md)
