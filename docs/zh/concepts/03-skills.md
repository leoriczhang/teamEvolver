# Skill 体系

Skill 是**可执行、可复用的任务方法**，明确适用场景、操作步骤、约束与配套资源。团队 Skill 的版本链保存在 PostgreSQL，经 Candidate Review 或管理员变更进入新版本，再由 Agent 通过 `GET /sync/skills` 拉取。

## Skill 结构

一个 Skill Bundle 是一组文件，`SKILL.md` 必选：

```text
my-skill/
├── SKILL.md          # 入口：YAML frontmatter + 正文
├── references/       # 参考文档（可选）
├── scripts/          # 辅助脚本（可选）
└── assets/           # 配套资源（可选）
```

### SKILL.md Frontmatter

```yaml
---
name: my-skill           # 唯一标识，与目录名一致
description: 简要描述这个 Skill 的用途和触发条件
category: general        # 分类，默认 general
---
```

`name`、`description` 必填，其余 frontmatter 字段原样保留。解析与渲染见 `team_skills/library/frontmatter.py`。

### Bundle 上限

租户 `skills` 模块配置限制 bundle 规模，写入前校验：

| 字段 | 默认 |
| --- | --- |
| `max_bundle_files` | 1024 |
| `max_bundle_bytes` | 64 MiB |
| `max_file_bytes` | 16 MiB |

## 团队 Skill 库

PostgreSQL 是团队 Skill 库的唯一权威源（`003_skills.sql`）：

| 表 | 内容 |
| --- | --- |
| `skills` | 当前 manifest head：名称、`skill_id`、当前版本、描述、分类、`runtime_policy`、`sha256`、`tree_sha256` |
| `skill_versions` | 追加写的版本历史 |
| `skill_version_files` / `skill_blobs` | 每个版本的文件清单与按内容寻址的文件字节 |
| `skill_mutations` | 变更幂等账本 |
| `skill_candidates` / `skill_candidate_files` / `skill_candidate_events` | Candidate、文件和事件 |

写入规则（`team_skills/library/repository.py:SkillLibraryRepository`）：

- 每次变更在租户事务内先锁定 Skill head 行，跨 Pod 串行，不依赖进程内锁。
- 变更 ID 在加锁后查询：重复请求返回第一次的结果，同一 ID 携带不同请求返回冲突。控制台写接口通过 `Idempotency-Key` 头传入。
- 版本行、文件、blob、head 和 mutation 在同一事务内写入。
- 版本只追加：回滚和删除都写新版本，删除是 tombstone 版本，历史不改写。

本机 `skills_runtime_cache_dir` 只是从这些行物化的可重建缓存，刷新失败不影响已提交状态。

## 个人 Skill 与团队 Skill

| 维度 | 个人 Skill | 团队 Skill |
|------|------------|------------|
| 存储 | OpenViking `viking://resources/<root_prefix>/peers/<user>/skills/` | PostgreSQL 版本链 |
| 编辑 | 用户本人或管理员，通过「个人与团队资产」 | 租户管理员，通过 Candidate Review 或 `/api/skills` |
| Agent 读取 | Context `personal_skills` 作用域 | `GET /sync/skills`，以及 Context `team_skills` 作用域 |
| 进入团队 | 用户提交发布申请，管理员审批 | — |

## Skill 状态

| 状态 | 说明 | 是否影响 Agent |
|------|------|---------------|
| 已发布 | `skills` 中的当前版本 | Agent 拉取此版本 |
| Candidate | `open`、`validating`、`published`、`rejected` 四种状态 | 发布前不影响 |
| 历史 | `skill_versions` 中被新版本取代的版本 | 可通过 Rollback 恢复 |

版本号为单调递增整数。

## Skill 生命周期

```text
Evidence → skill.evolve → Skill Candidate (open)
                              │ evaluate / validate
                              ▼
                       validating：静态检查 + True Replay
                              ├─ accept（auto）或 force → published → 新版本
                              ├─ reject → rejected
                              └─ inconclusive → 回到 open，等待人工处理
管理员直接编辑 /api/skills ─────────────────────► 新版本
```

## Agent 拉取

```http
GET /sync/skills?user_id=alice
Authorization: Bearer tevt_...
If-None-Match: "<上次的 ETag>"
```

响应包含每个 Skill 的名称、版本、`tree_sha256` 和 base64 文件内容；ETag 由租户、Account 和每个 Skill 的名称、版本、`tree_sha256` 计算，未变化时返回 `304`。详见 [Skill pull API](../api/06-skill-sync.md)。

## 分发给 Agent

Skill 统一走 pull 模式：Agent 用租户凭证 + `user_id` 调用 `GET /sync/skills` 拉取当前所有 Skill 的快照（带 ETag）。发布只写版本与 tombstone，不创建投递 outbox，任何 API 副本返回一致。

## Skill 与 Memory 的边界

| 维度 | Skill | Memory |
|------|-------|--------|
| 内容性质 | 可执行的任务方法（步骤、流程） | 可检索的事实和背景 |
| 是否规定执行流程 | 是 | 否 |
| 验证方式 | True Replay 对比 | Memory Replay；聚合内容由聚合 Skill 约束 |
| 写入 | Candidate Review、管理员编辑、发布申请 | Agent 只写个人 Memory；团队 Memory 由聚合与 DreamCycle 写入 |
| 典型例子 | “如何做 Code Review” | “团队使用 pnpm” |

## 代码入口

| 模块 | 路径 |
|------|------|
| 库仓库 | [repository.py](../../../team_skills/library/repository.py) |
| Bundle 模型 | [bundle.py](../../../team_skills/library/bundle.py) |
| Frontmatter | [frontmatter.py](../../../team_skills/library/frontmatter.py) |
| Candidate 仓库 | [candidates/repository.py](../../../team_skills/candidates/repository.py) |
| 静态检查 | [static_checks.py](../../../team_skills/candidates/static_checks.py) |
| HTTP | [api/](../../../team_skills/api/) |
