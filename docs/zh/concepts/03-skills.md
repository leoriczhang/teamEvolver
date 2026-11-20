# Skill 体系

Skill 是**可执行、可复用的任务方法**，明确适用场景、操作步骤、约束与配套资源。MA Skill 使用云端原生版本链，TE 经云端接口查看、编辑与发布；其他团队 Skill 在 PostgreSQL 发布，由 Agent 通过 `GET /sync/skills` 拉取。

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

MA 连接的 Skill 以 MA 原生 `latest_version` 和文件包为准。TE 不把它们导入下面的团队库版本链；`ma_skill_snapshots` 按连接、Skill ID 和云端版本保存经过树哈希校验的历史包，供验证与回放冻结基线。云端更新时，旧候选和旧 Evidence 退出自动流程，历史保留。

非 MA 团队 Skill 的权威源是 PostgreSQL（`003_skills.sql`）：

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
| 存储 | OpenViking `viking://resources/<root_prefix>/peers/<user>/skills/` | MA 原生云端版本链或非 MA 的 PostgreSQL 版本链 |
| 编辑 | 用户本人或管理员，通过「个人与团队资产」 | 租户管理员，通过 Candidate Review 或 `/api/skills` |
| Agent 读取 | Context `personal_skills` 作用域 | `GET /sync/skills`，以及 Context `team_skills` 作用域 |
| 进入团队 | 用户提交发布申请，管理员审批 | — |

## Skill 状态

| 状态 | 说明 | 是否影响 Agent |
|------|------|---------------|
| 已发布 | `skills` 中的当前版本 | Agent 拉取此版本 |
| Candidate | `open`、`validating`、`published`、`rejected`、`archived` 五种状态 | 发布前不影响 |
| 历史 | `skill_versions` 中被新版本取代的版本 | 可通过 Rollback 恢复 |

管理员实际更新团队 Skill（编辑、文件变更、实验台保存、导入替换、回滚或删除）时，
系统记录该 Skill 的自动进化重置时间，并归档正在使用的旧 Candidate。重复保存相同文件树
和幂等重试不会触发重置；Candidate Review 发布也不会触发手动重置。

重置之前的 Evidence、经验 Pattern、Session 和历史 Test Dataset 不再进入该 Skill 的自动
生成与评估。晚到的 Session 按业务发生时间判断，缺少业务时间时使用入库时间。新候选从
当前团队库的完整文件树开始，旧候选整包不再沿用。规划期间的库变更和旧回放的晚到结果
不能覆盖新的 Skill。历史候选、版本、Case、材料与回放结果保留，历史 Dataset 仍可在实验台
手动回放。重置仅作用于被更新的 Skill。

版本号为单调递增整数。

## Skill 生命周期

```text
Evidence → skill.evolve → Skill Candidate (open)
                              │ evaluate / validate
                              ▼
                       validating：静态检查 + Candidate Validation
                              ├─ accept（auto）或 force → published → 新版本
                              ├─ reject → rejected
                              └─ inconclusive → 回到 open，等待人工处理
管理员直接编辑 /api/skills ─────────────────────► 新版本
```

默认由 Worker 根据具体差异和当前 Evidence 选择经验审阅或候选执行。纯工具路由、使用条件
与依赖缺失处理等补充可通过有引用的经验审阅采纳，不要求 MA Runtime 验收。需要确认执行
行为时仅选择一个相关场景执行 Candidate，一次给出已知要求，最多两次交互。阶段验证
明确记录范围，完整交付须满足全部要求。至少一个候选场景完成即可通过基础验证，
A/B 效率独立记录；只有选择完整 A/B 时才执行双分支和原 Runtime 矩阵。

控制台「完整 A/B」以及 evaluate / validate 的 `validation_method: "full_ab"` 显式选择
效率比较；默认值是 `"auto"`。不再默认追加三个历史 Case。完成证据只在同候选版本、
文件树、任务材料、验收范围与执行配置下复用。外部阻塞停止该 Case；裁判不可用时保留
已取得的输出与产物，下一次验证仅重试现有产物的验收。

环境、Agent/工具或 Skill 绑定变化时，原 Case 与相关 Evidence 退出自动流程，归档记录
保存时间、原因和原执行指纹。历史原文、Session、产物与结论不变，实验台仍可手动回放，
生成新的实验记录，不自动恢复归档 Case。

## Agent 拉取

```http
GET /sync/skills?user_id=alice
Authorization: Bearer tevt_...
If-None-Match: "<上次的 ETag>"
```

响应包含每个 Skill 的名称、版本、`tree_sha256` 和 base64 文件内容；ETag 由租户、Account 和每个 Skill 的名称、版本、`tree_sha256` 计算，未变化时返回 `304`。详见 [Skill pull API](../api/06-skill-sync.md)。

## 分发给 Agent

MA Skill 发布由 `ma.skill` Worker 调用原生版本接口、更新目标成员及祖先引用并回读确认；回滚以历史内容创建新云端版本。discover/reconcile 只观察云端当前状态，不把 TE 的旧版本推回 MA。非 MA Agent 用租户凭证 + `user_id` 调用 `GET /sync/skills` 拉取团队库快照（带 ETag），发布不创建投递 outbox。

## Skill 与 Memory 的边界

| 维度 | Skill | Memory |
|------|-------|--------|
| 内容性质 | 可执行的任务方法（步骤、流程） | 可检索的事实和背景 |
| 是否规定执行流程 | 是 | 否 |
| 验证方式 | 经验审阅、候选执行或 True Replay | Memory Replay；聚合内容由聚合 Skill 约束 |
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
