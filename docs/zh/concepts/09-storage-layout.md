# 存储空间与目录布局

teamEvolver 将 Session 状态与 Skill 资产分开配置。平台资产视图中的 Session 运行状态来自 PostgreSQL 或本地/NAS，Skill 进化产物来自本地/NAS；OpenViking 用于个人记忆、团队资源和面向 Agent 的 Skill 镜像。正式 Skill 仍可按部署配置选择本地/NAS 或 OpenViking。本文说明平台存储的逻辑键布局，以及 teamEvolver **账号如何映射到 OpenViking 空间**。

## 账号 ↔ OpenViking 空间映射

一个 teamEvolver 账号并不会在 OpenViking 侧独占一个租户，而是被解析成 **API Key（认证）+ URI 路径（定位）** 两件事。后端定义 8 个 OpenViking 作用域（Scope），控制台资产页只直接展示 `personal_workspace` 与 `team_workspace` 两个根作用域；平台资产不属于这些作用域。

映射规则定义在 `teamEvolver/proxy/openviking_workspace.py` 的 `_scope_map()` 中。

| 作用域 | OpenViking 根 URI | 空间 | 类型 | 普通用户可写 |
|--------|-------------------|------|------|--------------|
| `personal_memory` | `viking://user/{个人 user}/memories` | 个人 | 记忆 | ✅ |
| `personal_skills` | `viking://resources/team-skill-evolver/peers/{账号}/skills` | 个人 | 技能 | ✅ |
| `personal_resources` | `viking://user/{个人 user}/resources` | 个人 | 资源 | ✅ |
| `personal_workspace` | `viking://user` | 个人 | 个人记忆根 | ✅ |
| `team_memory` | `viking://resources/shared-knowledge` | 团队 | 记忆 | ❌ 仅管理员 |
| `team_skills` | `viking://resources/team-skill-evolver/skills` | 团队 | 技能 | ❌ 仅管理员 |
| `team_resources` | `viking://resources/team` | 团队 | 资源 | ❌ 仅管理员 |
| `team_workspace` | `viking://resources` | 团队 | 团队资源根 | ❌ 仅管理员 |

URI 中的变量：

| 变量 | 来源 | 默认值 |
|------|------|--------|
| `root_prefix` | `sharing.viking_root_prefix` | `team-skill-evolver`（数据契约常量，不可重命名） |
| `个人 user` | 用户 `personal_space.viking_user` → 账号 ID → `sharing.viking_personal_user` | 账号 ID |
| `团队 user` | `sharing.viking_user` | `team` |
| `账号` | 用户注册表中的 `id` | — |
| `shared_knowledge_prefix` | `aggregation.shared_knowledge_prefix` | `shared-knowledge` |

### 命名空间划分

- **个人记忆**走 `viking://user/{user}/` 命名空间，按人隔离。
- **团队记忆聚合产物**走 `viking://resources/{shared_knowledge_prefix}/`，由 Account 内共享检索。
- **团队技能镜像**走 `viking://resources/{root_prefix}/`；**团队 Resources** 单独映射到 `viking://resources/team/`。
- **个人技能**在共享命名空间内通过 `peers/{账号}/` 路径段做隔离，见 `teamEvolver/storage/base.py` 的 `peer_key_prefix()`。

### Root Key 与身份头

控制台访问 OpenViking 时统一使用租户配置中的 Trusted Root Key，不读取或保存用户级 Key。空间定位只由以下两项决定：

- `X-OpenViking-Account`：当前租户绑定的 Account
- `X-OpenViking-User`：个人记忆使用用户绑定的 Name，团队资源使用团队 Name

`X-OpenViking-Agent` 固定为 `team-skill-evolver`。Root Key 只保存在服务端并用于 `X-API-Key` 与 `Authorization`，普通用户不会收到明文凭据。`sharing.viking_team_api_key` 仅作为旧字段名兼容 Root Key；`sharing.viking_personal_api_key(s)` 不再参与 Workspace 鉴权。

## 平台资产目录全景

平台资产是服务自身的只读运行视图，不再读取 OpenViking 文件树。控制台通过 `platform://session/...` 展示 PostgreSQL（或本地/NAS）中的 Session 流水，通过 `platform://skill/...` 展示 NAS 中的 Skill 进化产物；只暴露 allowlist 中的平台目录，不会把 `skills/`、`peers/` 等 Agent 资产混入内部视图。面向 Agent 的 `skills/<name>/` 子树仍可异步镜像到 OpenViking，但该镜像不是平台资产的数据源。

### 1. 技能库（成品）— 本地/NAS 或 OpenViking

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `skills/` | 目录 | 团队正式 Skill 库，每个 Skill 包含当前 `SKILL.md` 与不可变的 `versions/vN/`。本地/NAS 模式可由 `team_skills/library/mirror.py:VikingSkillMirror` 将 Agent 读取子树异步镜像到 OpenViking | `team_skills/library/hub.py`、`team_skills/library/mirror.py` |
| `manifest.json` | 文件 | 技能清单索引：技能名 → 版本/哈希，用于判断本地与远端差异 | `team_skills/library/hub.py` |
| `evolve_skill_registry.json` | 文件 | 技能 ID 登记表，保证技能 ID 跨节点稳定 | `team_skills/library/registry.py` |

### 2. Skill 实验室与进化素材 — Skill 后端

对应「数据驱动进化」闭环：从历史会话挖掘数据集，配套生成测试集并验证效果。

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `skill_lab/` | 目录 | 技能实验室。`skill_lab/datasets/<id>/` 存数据集，`skill_lab/runs/<id>/` 存实验运行结果 | `team_replay/lab/service.py` |
| `skill_datasets/` | 目录 | 技能测试集，按 `skill_datasets/by-id/<dataset>` 组织；通过 `skills[]` 建立并列 Skill 关联 | `team_replay/datasets/store.py` |
| `evolution_datasets/` | 目录 | 从历史会话合成的进化数据集 | `team_replay/datasets/synthesis.py` |
| `skill_evidence/` | 目录 | 技能效果证据（`<skill>.json`）：注入次数、有效性等进化决策依据 | `team_skills/evolution/runtime/evidence.py` |
| `experience_library/` | 目录 | Skill 使用经验（`<skill>.json`）：按语义经验键聚合错误与优秀实践，并累计独立 Session 发生次数 | `team_skills/evolution/runtime/experience_library.py` |
| `skill_version_context/` | 目录 | 技能版本上下文（`<skill>/v<N>.json`），供真回放对比基线 | `team_skills/candidates/store.py` |

所有新写入的 Test Dataset 均使用 `team-replay.dataset.v2`：顶层统一为
`dataset_id`、并列的 `skills[]`、`source` 和 `cases[]`，每个 Replay Case 统一为
`query`、`skill_ids[]`、`checks[]`、`materials[]`、`provenance` 和 `replay`。Session 数据集的
原始 Session 单独存放，由 `provenance.snapshot_ref` 引用。旧版 progressive、
Skill Lab、Session collection 和 Benchmark JSONL 仅通过读取适配器兼容；修改后
会按 v2 重新写入。JSONL、Markdown 和 ZIP 只作为导入、导出或人读视图。
`skills[]` 中不存在主从角色；`cases[].skill_ids[]` 显式声明每个任务使用其中哪些
Skill。多 Skill Replay 会在两个分支中安装同一组 Skill，只替换本次实验实际修改的
Skill，因此数据集关联关系本身不表达主 Skill。

### 3. Session 流水（进化原料）— Session 后端

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `sessions/` | 目录 | 待消费会话队列（`<session_id>.json`），进化引擎消费后删除 | `teamEvolver/session_store.py` |
| `session_archive/` | 目录 | 会话永久归档 | `teamEvolver/session_store.py` |
| `session_filter_audit/` | 目录 | 会话过滤决策审计（为何入队/跳过） | `teamEvolver/session_store.py` |
| `session_ledger/` | 目录 | 会话总账，记录 queued→consumed 生命周期状态流转 | `team_skills/evolution/runtime/orchestrator.py` |
| `session_datasets/` | 目录 | 基于历史 Session 建立的 `team-replay.dataset.v2` 集合及独立快照 | `team_replay/datasets/collections.py` |
| `session_index.json` | 文件 | 会话元信息索引（标题、轮次、Token、状态），供控制台快速浏览 | `teamEvolver/session_store.py` |

### 4. 进化验证（True Replay 闭环）— Skill 后端

规则见 `team_skills/candidates/store.py`。

| 条目 | 类型 | 用途 |
|------|------|------|
| `candidate_skills/` | 目录 | 候选技能暂存区（`<job_id>/SKILL.md` + files），尚未进入正式 `skills/` |
| `validation_jobs/` | 目录 | 验证任务（`<job_id>.json`），由进化服务产出 |
| `validation_claims/` | 目录 | 任务认领锁（`<job_id>/<user_alias>.json`），防止重复验证 |
| `validation_results/` | 目录 | 各客户端独立验证结果（`<job_id>/<user_alias>.json`） |
| `validation_evaluations/` | 目录 | 多方结果聚合评估（`<job_id>.json`） |
| `validation_decisions/` | 目录 | 最终发布/拒绝裁决（`<job_id>.json`） |
| `validation_decision_index.json` | 文件 | 裁决总索引，供快速检索 |

### 5. 人工审核 — Skill 后端

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `human_review/` | 目录 | 人工复核任务队列（`<job_id>.json`）：自动裁决拿不准时升级给人审 | `team_skills/candidates/store.py` |

### 6. DreamCycle 团队记忆维护

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `memory-changes/` | 目录 | 记忆变更总账（`teamevolver.memory-change.v1`）：DreamCycle 去重/清理/整合记忆时记录，支持真回放验证记忆改动 | `team_memory/memory_changes.py` |

### 7. 隔离与底层结构

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `peers/` | 目录 | 按客户/用户隔离区（`peers/{账号}/...`）。个人技能即落在 `peers/{账号}/skills` | `teamEvolver/storage/base.py` |
| `knowledge/` | 目录 | OpenViking 自身的顶层数据类别（与 memories/resources/skills 并列），非 teamEvolver 业务代码创建 | — |
| `.abstract.md` | 文件 | OpenViking 自动生成的目录 **L0 摘要**（一句话概览） | — |
| `.overview.md` | 文件 | OpenViking 自动生成的目录 **L1 概览**（结构化说明） | — |

## 数据流

```
Agent 会话采集
   → sessions/ ──(记账)→ session_ledger/ ──(归档)→ session_archive/
                                │ 挖掘 / 合成
                    evolution_datasets/ + skill_datasets/ → skill_lab/（实验）
                                │ 产出候选
                    candidate_skills/ + skill_version_context/（基线）
                                │ 验证 (True Replay)
   validation_jobs/ → validation_claims/ → validation_results/
                    → validation_evaluations/ → validation_decisions/
                                │  (skill_evidence/ 记录效果)
                                │  (拿不准 → human_review/)
                                ▼ 通过
   skill_mutation_commits/ → skill_sync_outbox/ → skills/ + manifest.json

【并行】DreamCycle 维护团队记忆 → memory-changes/（变更账本）
【隔离】peers/{账号}/ 存放个人级数据（个人技能等）
【聚合】viking://user/<user>/memories/
      → viking://user/<merge-user>/resources/teamEvolver/<staging_dir>/<target-hash>/（私有中转）
      → viking://resources/<shared_knowledge_prefix>/（团队 Memory）
```

> 注：`skill_mutation_commits/` 与 `skill_sync_outbox/` 是技能变更流水——每次 publish/delete 先写提交存档，再投递到同步发件箱下发各运行时，最后更新 `skills/` 与 `manifest.json`。见 `team_skills/library/mutations.py`。

## 控制台可视化

控制台「资产中心 → 个人与团队资产」直接展示两个空间：个人记忆对应 `viking://user`，团队资源对应 `viking://resources`。页面提供浏览模式、编辑模式、多文件 Diff 和批量条件写；管理员仍可使用 OpenViking CLI，自建部署会显示 Studio 入口。Skill Lab 与 Memory Lab 不再作为独立页面提供，Skill 的数据集选择和 True Replay 调试保留在实验工作台。

OpenViking 不可用时（传输错误/超时/HTTP 5xx，与 `build_object_store` 判定一致），工作空间的浏览、读取、编辑、批量写、mkdir 与删除会在 `sharing_local_fallback_enabled=True`（默认）时回退到本地/NAS，落点与 Skill 后端回退目录完全一致（同一 `_effective_fallback_root` 身份哈希），因此故障期间控制台看到与写入的正是 NAS 里的那份 Skill。4xx（如鉴权/配置错误）不回退，直接上浮以免掩盖配置问题；L0/L1 摘要为 OpenViking 生成、无本地等价物，故障期间显示为空。批量写在 OpenViking 可读但 `batch-write` 端点异常时降级为逐个 `content/write`（仍写回同一在线 OpenViking，避免读写分裂）。实现见 `teamEvolver/proxy/openviking_workspace.py` 的 `_LocalWorkspaceStore` 与 `_scope_fallback_root`。个人/团队 Memory 本体读写仍直连 OpenViking，无本地回退。

「资产中心 → 平台资产」使用独立的存储可视化页面，只读展示 PostgreSQL 与 NAS 中的 Session、Candidate、Validation、Evidence 等内部对象，提供分区筛选、逻辑键树、内容预览与分类统计，不显示 OpenViking CLI。前端实现见 `web-ui/src/views/PlatformAssetsView.tsx`，后端实现见 `teamEvolver/proxy/platform_assets.py`。

## 代码入口

| 模块 | 路径 |
|------|------|
| 作用域映射与工作区 API | `teamEvolver/proxy/openviking_workspace.py` |
| 平台资产 PG/NAS 浏览 API | `teamEvolver/proxy/platform_assets.py` |
| 账号注册表与 Key 解析 | `teamEvolver/proxy/users_admin.py` |
| OpenViking 对象存储 | `teamEvolver/storage/viking.py` |
| 内置本地对象存储 | `teamEvolver/storage/local.py:LocalObjectStore` |
| Skill 库异步镜像（spool + flusher） | `team_skills/library/mirror.py:VikingSkillMirror` |
| 隔离前缀 `peers/` | `teamEvolver/storage/base.py` |
| 会话存储 | `teamEvolver/session_store.py` |
| 验证存储 | `team_skills/candidates/store.py` |
| 技能变更 | `team_skills/library/mutations.py` |
| DreamCycle 记忆变更 | `team_memory/memory_changes.py` |
| 跨 User 团队 Memory 聚合 | `team_memory/service.py` |
| 端点解析（云端/本地） | `teamEvolver/config.py` |

## 相关文档

- [架构总览](./01-architecture)：存储在整体架构中的位置
- [进化闭环](./02-evolution-loop)：目录如何驱动进化
- [Session 体系](./05-sessions)：会话流水的详细结构
- [True Replay](./06-true-replay)：验证目录的使用场景
- [Memory 体系](./04-memory)：记忆空间与 DreamCycle
