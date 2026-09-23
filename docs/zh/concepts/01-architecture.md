# 架构总览

teamEvolver 是一个单体 FastAPI 服务（默认端口 52010），内嵌进化引擎、团队 Memory 聚合、DreamCycle、验证 Worker、SkillMiner 和 React 控制台。Session 队列/索引/审计等自身账本默认存放在内置本地对象存储（`teamEvolver/storage/local.py:LocalObjectStore`）；OpenViking 保存异步镜像的 Skill 库、团队 Memory 与聚合产物。YAML 配置、控制台 Session 和部分运行状态保存在 `~/.teamEvolver/`。

当 OpenViking 不可用时，存储自动回退到本地 `LocalObjectStore`（通过连通性探测 + TTL 缓存判定，回退期间写入仅落本地）；Skill 库以本地为主，通过持久化 spool 由后台 flusher 异步镜像到 OpenViking（`team_skills/library/mirror.py:VikingSkillMirror`）。

## 系统架构图

```
┌─────────────────────────────────────────────────────────────┐
│                    External Agent Runtimes                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐ │
│  │ Hermes   │  │ Pi (AH)  │  │ Codex    │  │ Custom Agent │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘ │
│       │     Hook/SDK│     SDK    │     Protocol V1         │
└───────┼─────────────┼────────────┼──────────────┼───────────┘
        │             │            │              │
        ▼             ▼            ▼              ▼
┌─────────────────────────────────────────────────────────────┐
│                  teamEvolver Service (:52010)                │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Proxy Server │  │ Agent Proto  │  │ Context Workspace │  │
│  │ (FastAPI)    │◄─┤ V1 Handler   │◄─┤ (token-scoped)    │  │
│  └──────┬───────┘  └──────────────┘  └───────────────────┘  │
│         │                                                    │
│  ┌──────▼───────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Session      │  │ Evolve Kernel│  │ Skill Mutation    │  │
│  │ Ingest/Filter│─►│ (11-stage    │─►│ Service (commit/  │  │
│  │              │  │  pipeline)   │  │  tombstone/outbox)│  │
│  └──────────────┘  └──────┬───────┘  └─────────┬─────────┘  │
│                           │                     │            │
│  ┌──────────────┐  ┌──────▼───────┐  ┌──────────▼────────┐  │
│  │ DreamCycle   │  │ True Replay  │  │ Validation Worker │  │
│  │ Scheduler    │  │ (baseline vs │◄─┤ (async, isolated) │  │
│  │ (Memory job) │  │  candidate)  │  │                   │  │
│  └──────┬───────┘  └──────────────┘  └───────────────────┘  │
│         │                                                    │
│  ┌──────▼───────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Langfuse     │  │ Dataset      │  │ Web Console       │  │
│  │ (tracing)    │  │ Synthesizer  │  │ (React, embedded) │  │
│  └──────────────┘  └──────────────┘  └───────────────────┘  │
│         │                                                    │
└─────────┼────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│         Local Object Store  +  OpenViking (storage)          │
│  Sessions/ledgers -> local object store (default)            │
│  Skills(mirrored) │  Memory  │  Snapshots  │  Resources     │
└─────────────────────────────────────────────────────────────┘
```

## 核心组件

### Proxy Server ([server.py](../../../teamEvolver/proxy/server.py))

FastAPI 应用组装入口，负责：
- 注册所有 HTTP 路由（控制台静态文件、健康检查、Agent 协议接口、管理接口）
- 初始化 Langfuse 追踪、DreamCycle、团队 Memory 聚合和后台验证 Worker
- 挂载内嵌的静态前端（来自 `teamEvolver/web/dist/`）

### Session Ingestion ([session_ingestion](../../../session_ingestion/README.md))

统一拥有两种 Session 接入方式：Agent 主动上报与租户数据源拉取。两种方式先分别完成身份校验或上游转换，再进入同一套去重、Evidence Classification、归档、入队和 Evolution 触发管线。

### Agent Protocol V1 ([agent_protocol.py](../../../teamEvolver/integrations/agent_protocol.py))

外部 Agent 的接入协议层，定义能力常量：

| 能力常量 | 说明 |
|---------|------|
| `session.ingest.v1` | Session 轨迹上报 |
| `context.workspace.v1` | 上下文读写（Memory/Skill） |
| `replay.branch.v1` | True Replay 分支执行 |
| `skill.sync.v1` | 团队 Skill 下发同步 |

### Context Workspace ([agent_context.py](../../../teamEvolver/proxy/agent_context.py))

面向 Agent 的上下文接口，使用租户 Key 与声明的 user_id，返回按 tenant/user 绑定的不透明 context_ref，OpenViking 凭证保留在服务端。

### Evolve Kernel ([evolve/](../../../team_skills/evolution/))

11 阶段进化管线，从 Session 到发布 Skill：

1. **Ingest** — 接收并校验 Session 载荷
2. **Filter** — 根据来源、长度、异常值过滤
3. **Summarize** — 提取轮次摘要和关键决策
4. **Judge** — 证据分类（Skill/Memory/任务要求/运行时问题/不足）
5. **Group** — 将同类 Evidence 聚合为变更窗口
6. **Evolve** — 生成 Skill Candidate 或 Memory Change 提案
7. **Create** — 创建 Candidate 版本（不影响已发布版本）
8. **Merge** — 合并上下文依赖
9. **Dataset Synthesis** — 从同源 Evidence 生成测试数据集
10. **Validate** — True Replay 验证（Baseline vs Candidate）
11. **Publish** — 通过门禁后发布新版本，同步给 Agent

### True Replay ([replay/engine.py](../../../team_replay/engine.py))

在隔离的真实 Agent Runtime 中并行执行 Baseline 和 Candidate 分支：
- Baseline：不加载 Candidate 的对照组
- Candidate：加载待验证 Skill 的实验组
- 使用冻结的 Context 投影（Snapshot Hash）
- Checklist 作为完成性门禁，效率按轮次→工具调用→Token 排序

### Skill Mutation Service ([mutations.py](../../../team_skills/library/mutations.py))

所有团队 Skill 的增删改必须经过此服务：
- 事务性变更：commit 记录 + tombstone
- 持久化同步 outbox，确保下发可靠性
- 版本号单调递增，保留完整审计链

### DreamCycle ([dreamcycle/](../../../team_memory/legacy_dreamcycle/))

团队 Memory 持续进化：
- `team_overview` — 维护团队概况
- `dedup` — 语义去重
- `cleanup` — 清理过期和低价值 Memory
- `consolidate` — 合并相关 Memory 条目
- `onboarding_check` — 新人可发现性检查
- 默认在凌晨 0-6 点窗口运行

### 团队 Memory 聚合（[aggregation/](../../../team_memory/)）

管理员从控制台选择 OpenViking Account 用户后，聚合服务以受控并发生成不执行 Skill 的 per-user 确定性快照，再通过有界 tree-reduce compile 写入 `viking://resources/<shared_knowledge_prefix>/`。原始快照与中间产物位于 merge 身份的私有 Resources，并使用指纹状态支持增量跳过。

### Validation Worker ([validation/worker.py](../../../team_skills/candidates/worker.py))

异步验证 Worker，后台消费 Candidate 队列，执行 True Replay 并收集结果。

### Langfuse ([observability/langfuse.py](../../../teamEvolver/observability/langfuse.py))

- **入站**：从 Langfuse 拉取 Agent Session 轨迹
- **出站**：将进化 Pipeline 中的所有 LLM 调用上报 Langfuse 做可观测性
- 所有 Prompt 和模型参数均可在控制台白盒配置

## 端口约定

teamEvolver 统一使用 **52010** 单端口承载所有能力：

| 路径 | 方法 | 说明 |
|------|------|------|
| `/health`, `/healthz` | GET | 健康检查 |
| `/status` | GET | 服务状态、排队数、技能数 |
| `/console` | GET | Web 控制台 |
| `/ingest_session` | POST | Agent Session 上报 |
| `/internal/agents/*` | * | Agent Protocol V1 内部接口 |
| `/trigger` | POST | 手动触发进化周期 |
| `/api/aggregation/*` | * | 团队 Memory 聚合、任务与设置 |
| `/api/*` | * | 其他控制台管理 API |

## 代码入口

| 组件 | 代码路径 |
|------|---------|
| 服务启动 | [launcher.py](../../../teamEvolver/launcher.py) |
| CLI 入口 | [cli/](../../../teamEvolver/cli/) |
| HTTP 路由 | [proxy/routes.py](../../../teamEvolver/proxy/routes.py) |
| 进化核心 | [evolve/](../../../team_skills/evolution/) |
| Skill 管理 | [skills/](../../../team_skills/library/) |
| Agent 集成 | [integrations/](../../../teamEvolver/integrations/) |
| DreamCycle | [dreamcycle/](../../../team_memory/legacy_dreamcycle/) |
| 团队 Memory 聚合 | [aggregation/](../../../team_memory/) |
| 配置存储 | [config_store/](../../../teamEvolver/config_store/) |
| 前端源码 | [web-ui/src/](../../../web-ui/src/) |
