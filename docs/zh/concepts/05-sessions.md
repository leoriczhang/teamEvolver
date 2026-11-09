# Session 体系

Session 是一次 Agent 与用户围绕任务产生的完整交互记录，包含对话、工具调用、产物、上下文使用情况和效率数据。Session 是所有 Evidence 的原始来源。

## Session 作为完整交互记录

一个 v2 Session 至少包含 `session_id`、`runtime.type`、`runtime_context.user_id` 和非空 `turns`。每个 turn 可以携带：

- 用户输入与 Agent 回复（`prompt_text`、`response_text` 或完整 `messages`）
- 工具调用与结果
- `context_usage`：本轮实际使用的 Context 引用
- 产物和效率指标

正式定义见 [agent-session-v2.schema.json](../../schemas/agent-session-v2.schema.json) 与 [Agent 接入协议 v2](../agent-integrations/06-protocol-v2.md)。

## 接入与强制分析

两种入口进入同一条持久化管线：

| 入口 | 受理 | 首个任务 |
| --- | --- | --- |
| Agent 推送 `POST /ingest_session` | 校验 v2 envelope、租户 Key 和 `user_id`，校验 `context_usage` 引用 | `session.normalize` |
| 数据源拉取 `POST /api/datasource/pull` 或定时拉取 | 校验 Adapter 已绑定到固定 revision | `session.pull`，按条受理并入队 `session.normalize` |

API 在一个短事务内写入 `ingestions`、原始 payload（PostgreSQL ObjectStore）和 `worker_tasks`，返回 `202` 与 `job_id`。任务 payload 固定接入配置的 `config_revision` 与 `adapter_revision`。Session Analyze 当前按执行时的 `skills` 模块配置解析模型与阶段设置；接入 revision 不代表每次历史模型请求的配置快照。

Worker 依次保存五个阶段的 `session_stage_results`，结果为不可变 payload：

1. `normalize`：校验 `turns`，生成标准 Session。
2. `split`：保存语义切分计划并按需要派生子任务；切分模型不可用或失败时保留完整 Session。
3. `analyze`：一次模型调用完成价值分类、摘要和四维评分（`team_skills/evolution/stages/analyze.py:analyze_session`）。
4. `trace_analyze`：逐条 Trace 分析（`team_skills/evolution/stages/trace_analyze.py:analyze_session_traces`）。
5. `archive`：只有前述阶段都成功后，写入正式 `sessions` 行。

缺少模型、调用失败或输出不合法时阶段失败，按 Worker 重试规则重试；不生成规则替代结果，也不写入正式 Session。

默认 Session Analyze 用同一次模型调用请求 `response_format={"type":"json_object"}`，返回单个 JSON 对象：`classification` 与 `judge` 是对象，`summary` 是字符串。默认 Prompt 在 Prompt Studio 中显示同一输出合同。整段回复必须是合法 JSON；不修复括号、不提取残缺 JSON，也不补齐分数。分类、摘要与原四维评分校验全部通过后才写入分析结果。现有标签式 Analyze Prompt override 继续使用 `<classification>`、`<summary>`、`<judge>` 合同。编辑新默认文本保存时，保留显式标题 `## 输出格式（严格返回单个 JSON 对象）` 即声明 JSON-object 协议；格式只由可信 Prompt 配置声明决定，不从 Session 或模型回复猜测，无效 JSON 不回退标签解析。分类准则、评分权重、模型和采样参数保持既有配置。

## 持续 Session 与来源修订

一次上游 Session 可以跨多个时间窗口持续增长。来源核验成功时，`source_session_key` 由租户、稳定 `source_instance_id`、`upstream_namespace` 和上游 Session ID 共同确定，与本地拉取批次或语义分段无关。实例取 Adapter 明确声明或 provider 与规范化 endpoint；namespace 取明确声明的项目或上游返回的 `projectId` / `project_id`，不从凭据猜测。namespace 缺失或冲突时 `identity_verified=false`，旧 Adapter 保持原接入方式，不承诺跨批次来源去重。

每个稳定 Trace ID 都有事实 `revision` 和前文 `context_revision`。Langfuse 原始 ID 保存在 `source_trace_id`，Mapper 的展示 `trace_id` 不覆盖它。事实修订包含原始交互、工具、实际请求与 Skill 使用等字段，排除抓取时间、分析时间、摘要和评分。相同文本但不同 Trace ID 是不同事件，不因文本相同而合并。

- 完全相同的同源快照复用已受理任务与旧分析，不受后续分析配置变化影响，也不自动发起重分析；原任务可能仍在处理或重试。
- 从 6 轮增长到 7 轮时，保留完整 7 轮，`target_trace_refs` 只有新轮，前 6 轮为 `context_trace_refs`。Session Analyze 可读取完整轨迹；Trace Analyze 只分析目标，完整旧前文仍作为 `analysis_context` 输入。
- 晚到 Trace 插入前文或旧轮纠正会更新受影响 Trace 的上下文修订。部分响应中缺少的旧轮保留，不推断删除；已知较旧的 `source_updated_at` 不覆盖较新事实。修订顺序无法判明时记录 `revision_conflicts`，不把冲突静默当成确认事实。
- 分段共享来源根，`provenance.full_snapshot_ref` 引用完整不可变源快照。上下文轮次不能增加 Evidence 数量或独立来源数量；分段不会增加独立上游来源。

分析输入保留 `request_context` 原始请求副本。`generation_input` / `trace_input` 的完整性为 `unknown`；仅有对话时为 `conversation_only`。Agent 自报请求为 `agent_reported`，不能自行声明服务端核验状态。它们均不能自动证明完整最终模型输入。详见 [上报与数据源 API](../api/03-session-ingest.md)。

同一 ingestion 的 Session Analyze 成功后写检查点；后续 Trace Analyze 失败重试时，仅在有效 Prompt、阶段设置与模型配置等真实依赖匹配时复用。Trace 结果缓存同时绑定目标修订、完整分析上下文、摘要与依赖版本。依赖变化、失败或未配置可验证依赖时可再次调用模型，系统不承诺模型只调用一次。旧正式 Session、历史 Evidence 和审计不删除、不改写。

## 价值分类

`analyze` 输出的 `value_judge.decision` 决定 Session 在 `sessions.decision` 中的取值：

| 模型判定 | `sessions.decision` | 去向 |
|----------|------|------|
| `valuable`：包含可复用的团队级 Skill Evidence | `valuable` | 可被 Skill Evolution 消费 |
| `memory_candidate`、`task_only`、`chitchat` 等 | `skipped` | 归档，不进入 Skill Evolution |

完整判定写入 `filter_audit`，四维评分写入 `judge`。注入的 Skill（`injected_skills`）只表示 Skill 对 Agent 可见，不构成 Evidence；实际使用的 Skill、工具调用、操作流程和任务成果才是 Evidence。

## Evidence

Skill Evolution 消费 `decision=valuable` 的 Session，并在 `skill_evidence` 中记录每个 Skill 跨周期累积的 Evidence。每个分支至少需要 `min_group_sessions` 个 Session 和 `min_group_users` 个用户，见 [进化闭环](./02-evolution-loop.md)。

## context_usage 追踪

每个 turn 可以携带 `context_usage`：

```json
{
  "context_usage": {
    "context_snapshot_id": "ctxsnap_...",
    "memory_refs": [{"context_ref": "...", "operation": "retrieved"}],
    "skill_refs": [{"context_ref": "...", "operation": "injected"}],
    "feedback": {"outcome": "success", "correction": ""}
  }
}
```

推送受理时 `teamEvolver/context/usage.py:verify_usage` 校验：快照必须属于当前租户和用户；每个 `context_ref` 必须由 Context 接口签发、未过期、类型匹配并属于该快照；`operation` 只能是 `retrieved`、`injected`、`read`、`selected`；每类最多 200 个。校验失败返回 400。

## Session Materials

Session 可以引用用户上传的文件。`team_replay/datasets/materials.py:collect_session_materials` 从 Session 恢复材料，用于 Test Dataset 和 True Replay：

1. **内嵌材料**（`source_materials`）：跨主机 Agent 以 base64 携带文件内容。
2. **沙箱快照**（`runtime_context.sandbox_snapshot_path`）：同主机部署通过 tar.gz 快照恢复。

按 SHA-256 去重，单文件 ≤ 20 MiB、总大小 ≤ 80 MiB、最多 100 个文件；只接受相对路径，拒绝绝对路径和 `..`。

## metrics 字段

| 字段 | 说明 |
|------|------|
| `interaction_turns` | 交互轮次数 |
| `tool_call_count` | 工具调用总次数 |
| `total_tokens` | 总 Token |
| `input_tokens`、`output_tokens` | 输入、输出 Token |
| `cache_read_tokens`、`cache_write_tokens`、`reasoning_tokens` | 缓存与推理 Token |

这些指标是 True Replay 效率比较的来源，缺失时记为 unavailable，不补 0。

## Session 存储结构

| 位置 | 内容 |
| --- | --- |
| `ingestions` | 受理记录：来源、外部 ID、用户、Account、`dedupe_key`、任务 ID、Adapter/配置 revision、payload 引用和 hash、业务时间 |
| `session_stage_results` | 每个阶段的状态、结果引用和错误码 |
| `session_source_heads` | 同源最新已受理事实快照；不表示分析已完成 |
| `session_source_snapshots` | 来源根与事实快照修订到 ingestion 的幂等映射 |
| `session_analysis_cache` | 依赖与目标上下文匹配的 Trace 分析结果引用 |
| `sessions` | 正式 Session：`decision`、`judge`、`filter_audit`、`meta`、`content_ref`、`content_hash`、`business_time`、`ingested_at` |
| `storage_objects` | 原始 payload（`ingest/<tenant>/<ingestion>.json`）与每阶段结果（`session/<hash>/<stage>/<digest>.json`） |

控制台同时展示 `business_time`（业务发生时间）和 `ingested_at`（入库时间）。所有表启用 RLS。

## 幂等

- Agent 推送的 `dedupe_key` 为 `push:<session_id>`，同一租户内重复推送返回第一次受理的 `ingestion_id` 和 `job_id`，响应 `dedupe=true`。
- 手动拉取每次创建新扫描；携带 `Idempotency-Key` 时，同租户同 Adapter 的相同 Key 复用请求，不同请求体返回 409。定时拉取仍按日期窗口去重。
- 已核验来源的逐条接入按 `source_session_key` 与事实快照修订去重，事务锁使并发接受收敛到同一 ingestion。
- 正式 Session ID 为 `session_<ingestion_id>`，保留上游外部 ID；来源身份和本地归档 ID 分开，旧 Session 不回写新的来源身份。

## 代码入口

| 模块 | 路径 |
|------|------|
| HTTP 受理 | [api/__init__.py](../../../session_ingestion/api/__init__.py) |
| 数据源控制面 | [api/datasource.py](../../../session_ingestion/api/datasource.py) |
| Worker Handler | [handlers/](../../../session_ingestion/handlers/) |
| 强制阶段 | [stages.py](../../../session_ingestion/stages.py) |
| v2 envelope | [push/protocol.py](../../../session_ingestion/push/protocol.py) |
| 仓库与查询 | [repository.py](../../../session_ingestion/repository.py)、[queries.py](../../../session_ingestion/queries.py) |
| 不可变 payload | [payloads.py](../../../session_ingestion/payloads.py) |
| 来源修订与受理 | [source_revisions.py](../../../session_ingestion/source_revisions.py)、[source_acceptance.py](../../../session_ingestion/source_acceptance.py)、[source_repository.py](../../../session_ingestion/source_repository.py) |

## 相关文档

- [Session 上报 API](../api/03-session-ingest.md)
- [Session 查询 API](../api/08-sessions-api.md)
- [租户数据源接入](../guides/10-datasource-onboarding.md)
- [进化闭环](./02-evolution-loop.md)
