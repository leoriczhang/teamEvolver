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

API 在一个短事务内写入 `ingestions`、原始 payload（PostgreSQL ObjectStore）和 `worker_tasks`，返回 `202` 与 `job_id`。任务 payload 固定 `config_revision` 与 `adapter_revision`，之后的配置或 Adapter 修改不影响在途任务。

Worker 依次执行四个强制阶段，每个阶段写一行 `session_stage_results`，结果为不可变 payload：

1. `normalize`：校验 `turns`，生成标准 Session。
2. `analyze`：一次模型调用完成价值分类、摘要和四维评分（`team_skills/evolution/stages/analyze.py:analyze_session`）。
3. `trace_analyze`：逐条 Trace 分析（`team_skills/evolution/stages/trace_analyze.py:analyze_session_traces`）。
4. `archive`：只有前三个阶段都成功后，写入正式 `sessions` 行。

缺少模型、调用失败或输出不合法时阶段失败，按 Worker 重试规则重试；不生成规则替代结果，也不写入正式 Session。

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
| `sessions` | 正式 Session：`decision`、`judge`、`filter_audit`、`meta`、`content_ref`、`content_hash`、`business_time`、`ingested_at` |
| `storage_objects` | 原始 payload（`ingest/<tenant>/<ingestion>.json`）与每阶段结果（`session/<hash>/<stage>/<digest>.json`） |

控制台同时展示 `business_time`（业务发生时间）和 `ingested_at`（入库时间）。所有表启用 RLS。

## 幂等

- Agent 推送的 `dedupe_key` 为 `push:<session_id>`，同一租户内重复推送返回第一次受理的 `ingestion_id` 和 `job_id`，响应 `dedupe=true`。
- 拉取的 `dedupe_key` 由请求窗口和过滤条件计算；定时拉取按日期窗口去重。
- 正式 Session ID 由 `(tenant_id, user_id, session_id)` 计算（`session_ingestion/identifiers.py:tenant_user_session_id`），不同租户或用户的同名 Session 不冲突。

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

## 相关文档

- [Session 上报 API](../api/03-session-ingest.md)
- [Session 查询 API](../api/08-sessions-api.md)
- [租户数据源接入](../guides/10-datasource-onboarding.md)
- [进化闭环](./02-evolution-loop.md)
