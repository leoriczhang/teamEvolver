# 存储空间与目录布局

teamEvolver 的所有持久化数据都存放在 OpenViking 后端。本文说明两件事：teamEvolver **账号如何映射到 OpenViking 空间**，以及**团队 Workspace 根目录下每个目录/文件的用途**。它是理解进化闭环数据流的基础。

## 账号 ↔ OpenViking 空间映射

一个 teamEvolver 账号并不会在 OpenViking 侧独占一个租户，而是被解析成 **API Key（认证）+ URI 路径（定位）** 两件事。每个账号被映射到 6 个作用域（Scope），分为个人（personal）与团队（team）两类。

映射规则定义在 `teamEvolver/proxy/openviking_workspace.py` 的 `_scope_map()` 中。

| 作用域 | OpenViking 根 URI | 空间 | 类型 | 普通用户可写 |
|--------|-------------------|------|------|--------------|
| `personal_memory` | `viking://user/{个人 user}/memories` | 个人 | 记忆 | ✅ |
| `personal_skills` | `viking://resources/team-skill-evolver/peers/{账号}/skills` | 个人 | 技能 | ✅ |
| `personal_workspace` | `viking://user/{个人 user}` | 个人 | 工作区根 | ✅ |
| `team_memory` | `viking://user/{团队 user}/memories` | 团队 | 记忆 | ❌ 仅管理员 |
| `team_skills` | `viking://resources/team-skill-evolver/skills` | 团队 | 技能 | ❌ 仅管理员 |
| `team_workspace` | `viking://resources/team-skill-evolver` | 团队 | 工作区根 | ❌ 仅管理员 |

URI 中的变量：

| 变量 | 来源 | 默认值 |
|------|------|--------|
| `root_prefix` | `sharing.viking_root_prefix` | `team-skill-evolver`（数据契约常量，不可重命名） |
| `个人 user` | 用户 `personal_space.viking_user` → 账号 ID → `sharing.viking_personal_user` | 账号 ID |
| `团队 user` | `sharing.viking_user` | `default` |
| `账号` | 用户注册表中的 `id` | — |

### 命名空间划分

- **记忆**走 `viking://user/{user}/` 命名空间，按人隔离。
- **技能与共享资源**走 `viking://resources/{root_prefix}/` 命名空间，团队共享。
- **个人技能**在共享命名空间内通过 `peers/{账号}/` 路径段做隔离，见 `teamEvolver/storage/base.py` 的 `peer_key_prefix()`。

### API Key 与身份头

调用 OpenViking 时的凭据在 `teamEvolver/proxy/openviking_workspace.py` 的 `_workspace_headers()` 中解析：

| 空间 | API Key 解析顺序 |
|------|------------------|
| 团队 | 用户 `team_space` Key → 管理员团队 Key（继承）→ `sharing.viking_team_api_key` → `sharing.viking_api_key` |
| 个人 | 用户 `personal_space` Key → `sharing.viking_personal_api_key` → `sharing.viking_api_key` |

三个身份头始终发送：`X-OpenViking-Account`（默认 `default`）、`X-OpenViking-User`（个人=账号 ID，团队=`default`）、`X-OpenViking-Agent`（`team-skill-evolver`）。**当 API Key 为空时（如本地自托管 OpenViking），`X-API-Key` 与 `Authorization` 头不发送**，仅靠上述三个身份头隔离；本地/云端的 URI 映射规则完全一致，仅端点不同（见 `teamEvolver/config.py` 的 `resolve_viking_endpoint()`）。

普通用户无需配置团队 Key，会自动继承第一个管理员配置的团队 Key，见 `teamEvolver/proxy/users_admin.py` 的 `_effective_team_key()`。

## 团队 Workspace 目录全景

团队 Workspace（`viking://resources/team-skill-evolver/`）是整个进化闭环的共享数据库。根目录下的条目按 7 个功能组划分。

### 1. 技能库（成品）

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `skills/` | 目录 | 团队正式技能库，每个技能一个子目录（`skills/<name>/SKILL.md` + 版本）。Pi Agent / Hermes 从此读取团队技能 | `teamEvolver/skills/hub.py` |
| `manifest.json` | 文件 | 技能清单索引：技能名 → 版本/哈希，用于判断本地与远端差异 | `teamEvolver/skills/hub.py` |
| `evolve_skill_registry.json` | 文件 | 技能 ID 登记表，保证技能 ID 跨节点稳定 | `teamEvolver/skills/registry.py` |

### 2. 技能实验室与进化素材

对应「数据驱动进化」闭环：从历史会话挖掘数据集，配套生成测试集并验证效果。

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `skill_lab/` | 目录 | 技能实验室。`skill_lab/datasets/<id>/` 存数据集，`skill_lab/runs/<id>/` 存实验运行结果 | `teamEvolver/skill_lab.py` |
| `skill_datasets/` | 目录 | 技能测试集，按 `skill_datasets/<skill>/<dataset>` 组织 | `teamEvolver/dataset_store.py` |
| `evolution_datasets/` | 目录 | 从历史会话合成的进化数据集 | `teamEvolver/dataset_synthesizer.py` |
| `skill_evidence/` | 目录 | 技能效果证据（`<skill>.json`）：注入次数、有效性等进化决策依据 | `teamEvolver/evolve/runtime/evidence.py` |
| `skill_version_context/` | 目录 | 技能版本上下文（`<skill>/v<N>.json`），供真回放对比基线 | `teamEvolver/validation/store.py` |

### 3. 会话流水（进化原料）

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `sessions/` | 目录 | 待消费会话队列（`<session_id>.json`），进化引擎消费后删除 | `teamEvolver/session_store.py` |
| `session_archive/` | 目录 | 会话永久归档 | `teamEvolver/session_store.py` |
| `session_filter_audit/` | 目录 | 会话过滤决策审计（为何入队/跳过） | `teamEvolver/session_store.py` |
| `session_ledger/` | 目录 | 会话总账，记录 queued→consumed 生命周期状态流转 | `teamEvolver/evolve/runtime/orchestrator.py` |
| `session_index.json` | 文件 | 会话元信息索引（标题、轮次、Token、状态），供控制台快速浏览 | `teamEvolver/session_store.py` |

### 4. 进化验证（True Replay 闭环）

规则见 `teamEvolver/validation/store.py`。

| 条目 | 类型 | 用途 |
|------|------|------|
| `candidate_skills/` | 目录 | 候选技能暂存区（`<job_id>/SKILL.md` + files），尚未进入正式 `skills/` |
| `validation_jobs/` | 目录 | 验证任务（`<job_id>.json`），由进化服务产出 |
| `validation_claims/` | 目录 | 任务认领锁（`<job_id>/<user_alias>.json`），防止重复验证 |
| `validation_results/` | 目录 | 各客户端独立验证结果（`<job_id>/<user_alias>.json`） |
| `validation_evaluations/` | 目录 | 多方结果聚合评估（`<job_id>.json`） |
| `validation_decisions/` | 目录 | 最终发布/拒绝裁决（`<job_id>.json`） |
| `validation_decision_index.json` | 文件 | 裁决总索引，供快速检索 |

### 5. 人工审核

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `human_review/` | 目录 | 人工复核任务队列（`<job_id>.json`）：自动裁决拿不准时升级给人审 | `teamEvolver/validation/store.py` |

### 6. DreamCycle 团队记忆维护

| 条目 | 类型 | 用途 | 代码入口 |
|------|------|------|----------|
| `memory-changes/` | 目录 | 记忆变更总账（`teamevolver.memory-change.v1`）：DreamCycle 去重/清理/整合记忆时记录，支持真回放验证记忆改动 | `teamEvolver/dreamcycle/memory_changes.py` |

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
```

> 注：`skill_mutation_commits/` 与 `skill_sync_outbox/` 是技能变更流水——每次 publish/delete 先写提交存档，再投递到同步发件箱下发各运行时，最后更新 `skills/` 与 `manifest.json`。见 `teamEvolver/skills/mutations.py`。

## 控制台可视化

在控制台「资产中心 → 上下文空间」中切换到**团队 Workspace**，文件树会为上述已知目录内联显示中文用途说明，浏览时即见即懂。前端实现见 `web-ui/src/views/OpenVikingWorkspaceShell.tsx`。管理员还可在同一界面通过内置 OpenViking CLI 与 Studio 入口直接查数据。

## 代码入口

| 模块 | 路径 |
|------|------|
| 作用域映射与工作区 API | `teamEvolver/proxy/openviking_workspace.py` |
| 账号注册表与 Key 解析 | `teamEvolver/proxy/users_admin.py` |
| OpenViking 对象存储 | `teamEvolver/storage/viking.py` |
| 隔离前缀 `peers/` | `teamEvolver/storage/base.py` |
| 会话存储 | `teamEvolver/session_store.py` |
| 验证存储 | `teamEvolver/validation/store.py` |
| 技能变更 | `teamEvolver/skills/mutations.py` |
| DreamCycle 记忆变更 | `teamEvolver/dreamcycle/memory_changes.py` |
| 端点解析（云端/本地） | `teamEvolver/config.py` |

## 相关文档

- [架构总览](./01-architecture)：存储在整体架构中的位置
- [进化闭环](./02-evolution-loop)：目录如何驱动进化
- [Session 体系](./05-sessions)：会话流水的详细结构
- [True Replay](./06-true-replay)：验证目录的使用场景
- [Memory 体系](./04-memory)：记忆空间与 DreamCycle
