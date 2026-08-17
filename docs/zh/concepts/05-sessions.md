# Session 体系

Session 是一次 Agent 与用户围绕任务产生的完整交互记录。它不是聊天日志，而是包含对话、工具调用、产物、上下文使用情况和效率数据的结构化记录，是 teamEvolver 进化闭环的输入源。

## Session 作为完整交互记录

一个 Session 记录了 Agent 从接收任务到完成（或终止）的全过程，核心要素包括：

- **身份信息**：`session_id`（唯一标识）、`runtime`（运行时类型和 integration_id）、`runtime_context.external_subject`（用户标识）
- **轮次序列（turns）**：每一轮包含 `turn_num`、`prompt_text`/`instruction`（用户输入）、`response_text`/`response`（Agent 回复）、`messages`（完整消息序列）、`tool_calls`/`tool_results`（工具调用和结果）
- **效率指标（metrics）**：`interaction_turns`（交互轮次）、`tool_call_count`（工具调用数）、`total_tokens`（总 Token 消耗），以及 input/output/cache/reasoning tokens 明细
- **上下文使用（context_usage）**：记录该轮实际使用的上下文引用，包括 `context_snapshot_id`、`memory_refs`、`skill_refs`、`feedback`
- **源材料（source_materials）**：用户上传的文件，以 base64 内嵌或通过沙箱快照路径引用

Session Schema 的正式定义见 [agent-session-v1.schema.json](file:///home/zhangpengkun/teamEvolver/docs/schemas/agent-session-v1.schema.json)。

## Session 采集管线（Ingest Pipeline）

Agent 在每次会话结束后通过 `POST /ingest_session` 接口上报 Session 轨迹。采集管线执行以下步骤：

1. **身份校验**：验证 integration-scoped token，将 `integration_id + external_subject` 映射到 teamEvolver 用户
2. **Schema 校验**：检查必需字段（schema_version、protocol_version、session_id、runtime、runtime_context、turns）
3. **去重检测**：通过内容指纹（content_fingerprint）判断是否为已处理 Session 的重复提交。指纹结合轮次数和对话文本哈希计算，同一 Session 无新轮次时不重复入队
4. **双写存储**：同时写入队列（`sessions/`）和归档（`session_archive/`），队列用于进化引擎消费，归档用于审计和历史回溯
5. **索引更新**：维护 `session_index.json`，记录所有 Session 的元信息（标题、用户、轮次数、Token、工具调用数、价值判定结果）
6. **过滤审计**：写入 `session_filter_audit/`，记录过滤决策

队列中的 Session 被进化引擎消费后，从队列中移除，但归档永久保留。

## Session 过滤与价值分类

Session 进入进化队列前，由 `SessionValueClassifier` 进行价值判定，决定其是否进入 Skill Evolution 或 Memory Evolution 管线。

### 判定类别

| decision | 含义 | 去向 |
|----------|------|------|
| `valuable` | 包含可复用的团队级 Skill Evidence：执行过的工作流、具体成果、明确的 Skill 差距、领域流程、或用户对产出的反馈 | 进入 Skill Evolution 队列 |
| `memory_candidate` | 有用的 Evidence 是用户特定偏好或习惯（而非团队 SOP），且对该用户未来任务可能持续有用 | 路由到 Memory Evolution（DreamCycle） |
| `task_only` | 真实任务请求但尚无完成成果或可操作的进化 Evidence | 归档，等待更多 Evidence 累积 |
| `chitchat` | 社交、空对话或非任务交互 | 跳过，不进入进化 |

### 判定模式

分类器支持三种模式：

1. **确定性模式（deterministic）**：受控的 Candidate 审计 Session（含 `candidate_job_id` + `candidate_sha256` 且 `candidate_skill_gap_report` 工具调用成功），直接判定为 `valuable`，置信度 1.0
2. **模型模式（model）**：使用配置的 LLM 对 Session 摘要进行分类，输出 decision、confidence、reason、memory_candidates
3. **启发式模式（heuristic）**：LLM 不可用时的降级策略——无用户文本判定为 chitchat；有工具调用/Skill 使用/验证反馈判定为 valuable；长文本（≥80 字符）或多轮判定为 task_only；短交流判定为 chitchat

> **注意**：注入的 Skill（`injected_skills`）仅表示 Skill 对 Agent 可见，不构成 Evidence；实际使用的 Skill（`used_skills`）、工具调用、具体操作流程和任务成果才是更强的 Evidence 信号。不要将单次交付物的明确要求误判为用户 Memory。

### Session 摘要

分类器不使用完整 Session 文本，而是提取结构化摘要：
- 用户请求列表（最多 20 条）
- 使用的工具名列表（最多 20 个）
- 交互轮次摘要（每轮用户输入截断至 4000 字符，回复截断至 6000 字符，含工具调用数和已用 Skill）
- 已验证的团队 Skill 反馈（当 `context_usage.verified=true` 时提取 skill_refs 和 feedback）
- 效率指标（interaction_turns、tool_call_count、total_tokens）

## Evidence 累积

单个 Session 通常不足以触发进化。进化引擎将同类 Evidence 聚合到变更窗口，当 Evidence 积累到阈值（`evolve.evidence_change_debt_threshold=3`）时才生成 Candidate。

Session 的 `context_usage` 是 Evidence 溯源的关键：
- **`used_context_refs`**：Agent 在 Context Workspace 中实际读取的 context_ref 列表，由 Agent 在 Session commit 时上报，服务端解析为 OpenViking 的具体 Memory/Skill 引用
- **`verified` 标记**：当 Agent 确认使用了团队 Skill 并给出反馈（outcome、correction）时，该 Skill 引用被标记为已验证，是最有价值的 Evidence 来源
- **`context_snapshot_id`**：冻结的 Context 投影 ID，保证同一 Session 的上下文可重建

## Session Materials

Session 可能引用用户上传的文件作为任务输入。`collect_session_materials` 负责从 Session 中恢复这些文件，支持两种来源：

1. **内嵌材料（source_materials）**：跨主机 Agent 通过 base64 编码直接在 Session 载荷中携带文件内容
2. **沙箱快照（sandbox_snapshot_path）**：同主机部署的 Pi Agent 通过 tar.gz 快照路径恢复上传文件，无需膨胀 ingest 载荷

材料收集执行去重（SHA-256 去重）和大小限制：单文件 ≤20MB，总大小 ≤80MB，总文件数 ≤100。恢复后的材料用于 True Replay 时注入隔离沙箱的 workspace，确保 Replay Case 可以访问原始输入文件。

路径安全处理：只接受相对路径，拒绝绝对路径和包含 `..` 的路径，防止路径遍历。

## context_usage 追踪

Session 的每一轮（turn）都可以携带 `context_usage` 字段，追踪 Agent 实际使用了哪些上下文：

```json
{
  "context_usage": {
    "context_snapshot_id": "ctxsnap_...",
    "memory_refs": [...],
    "skill_refs": [...],
    "feedback": {
      "outcome": "success|partial|failure",
      "correction": "用户修正内容",
      "error_code": "..."
    }
  }
}
```

Agent 通过 Context Workspace 的 `sessions/commit` 接口上报 `used_context_refs`，服务端将这些不透明的 ref 解析为具体的 OpenViking 资源引用，并提交 OpenViking 的 `session.used` 记录。使用记录按载荷持久化，确保失败重试不会重复计数。

## metrics 字段

Session 的 metrics 字段记录效率数据，是 True Replay 效率比较的基线来源：

| 字段 | 类型 | 说明 |
|------|------|------|
| `interaction_turns` | int | Agent 与用户/工具的交互轮次数 |
| `tool_call_count` | int | 工具调用总次数 |
| `total_tokens` | int | 总 Token 消耗（input + output） |
| `input_tokens` | int | 输入 Token 数 |
| `output_tokens` | int | 输出 Token 数 |
| `cache_read_tokens` | int | 缓存读取 Token 数 |
| `cache_write_tokens` | int | 缓存写入 Token 数 |
| `reasoning_tokens` | int | 推理 Token 数 |

这些指标在 True Replay 中用于 Baseline 与 Candidate 的效率对比，优先级为：轮次 → 工具调用数 → 总 Token。

## Session 存储结构

在 OpenViking 后端中，Session 数据按以下前缀组织：

| 前缀 | 用途 |
|------|------|
| `sessions/{session_id}.json` | 待消费队列（消费后删除） |
| `session_archive/{session_id}.json` | 永久归档 |
| `session_filter_audit/{session_id}.json` | 过滤决策审计记录 |
| `session_index.json` | 所有 Session 的元信息索引（最多保留 10000 条） |

归档中保留 Session 的完整内容和状态（queued/consumed/skipped）。索引按 ingest 时间倒序排列，支持控制台快速浏览和筛选。

## 内容指纹与幂等

Session 存储使用内容指纹实现幂等写入：
- 指纹由每轮的 prompt_text、response_text、runtime 类型/integration_id、context_usage（snapshot_id、memory_refs、skill_refs、feedback）共同计算
- 同一 session_id 的重复上报，如果指纹不变（没有新轮次），则判定为重复，不重新入队
- 如果指纹变化（新增轮次），则更新归档和队列，视为会话继续

这避免了 Agent 因重试或延迟上报导致同一对话重复进入进化管线。

## 代码入口

| 模块 | 路径 |
|------|------|
| Session 存储与生命周期 | [session_store.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/session_store.py) |
| Session 价值分类器 | [session_filter.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/session_filter.py) |
| Session 材料收集 | [session_materials.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/session_materials.py) |
| Session Schema 定义 | [docs/schemas/agent-session-v1.schema.json](file:///home/zhangpengkun/teamEvolver/docs/schemas/agent-session-v1.schema.json) |
| Agent 接入协议（Session Ingest 部分） | [Protocol V1 规范](../agent-integrations/02-protocol-v1) |

## 相关文档

- [架构总览](./01-architecture)：Session Ingest 在架构中的位置
- [进化闭环](./02-evolution-loop)：Session 如何驱动进化闭环
- [True Replay](./06-true-replay)：Session 作为 True Replay 的 Case 来源
- [Memory 体系](./04-memory)：memory_candidate 类 Session 的去向
