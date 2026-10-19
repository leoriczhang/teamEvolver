# 发布与回滚

团队 Skill 的每次变更都写入 PostgreSQL 的追加式版本链。发布、更新、回滚和删除使用同一套事务规则（`team_skills/library/repository.py:SkillLibraryRepository`），任何 API 或 Worker 副本都可以执行，结果一致。

## 变更类型

| action | 来源 | 结果 |
| --- | --- | --- |
| `publish` | 首次写入某个 Skill | 版本 1 |
| `update` | Candidate 发布、`POST /api/skills`、`POST /api/skills/{name}/files`、文件删除 | 新版本；内容未变化时返回 `unchanged` |
| `rollback` | `POST /api/skills/{name}/rollback` | 以历史版本内容写一个新版本 |
| `delete` | `DELETE /api/skills/{name}` | 写 tombstone 版本，Skill 离开当前 manifest |

## 事务性提交

一次变更在调用方的租户事务内完成：

1. `SELECT ... FOR UPDATE` 锁定 `skills` head 行，同一 Skill 的并发变更跨 Pod 串行。
2. 加锁后查询 `skill_mutations`：同一 `mutation_id` 重放第一次的结果；同一 ID 携带不同请求返回冲突。
3. 写入前校验 bundle：`SKILL.md` 格式、路径安全和租户 bundle 上限。
4. 在同一事务内写入 `skill_versions`、`skill_version_files`、`skill_blobs`、`skills` head和 `skill_mutations`。

事务失败时什么都不写。发布后的 Skill 通过 `GET /sync/skills` 供 Agent 拉取。

## 幂等

| 入口 | mutation_id |
| --- | --- |
| 控制台 `/api/skills*` | `Idempotency-Key` 请求头；未提供时每次生成新 ID |
| Candidate 发布 | `candidate:<candidate_id>:r<revision>` |
| Skill 导入 | 导入命令 ID 派生 |

## 版本单调递增

版本号是每个 Skill 内单调递增的整数。版本只追加，历史版本不修改、不删除；`GET /api/skills/{name}/versions` 返回完整历史，`GET /api/skills/{name}/versions/{version}` 返回某个版本的内容。

## Tombstone

删除写一个 tombstone 版本：该版本没有文件，`skills` head 标记为已删除，Skill 不再出现在 `GET /api/skills` 和 `GET /sync/skills` 中。之前的版本仍可读取，也可以通过回滚重新发布。

## 回滚即新版本

```http
POST /api/skills/my-skill/rollback?target_version=3
Idempotency-Key: rollback-my-skill-v3
```

回滚把 v3 的完整内容写成当前最大版本 +1，响应包含 `restored_from` 与 `new_version`，v3 之后的全部历史和审计链保持不变。

## 审计链

| 记录 | 内容 |
| --- | --- |
| `skill_mutations` | mutation_id、action、版本、请求 hash、actor、source、metadata |
| `skill_candidate_events` | Candidate 每次状态变化、编辑和反馈 |
| `audit_log` | 平台级审计 |

## 代码入口

| 模块 | 路径 |
|------|------|
| 库仓库与提交 | [repository.py](../../../team_skills/library/repository.py) |
| 发布原语 | [publication.py](../../../team_skills/library/publication.py) |
| Schema | [003_skills.sql](../../../teamEvolver/database/schema/003_skills.sql) |

## 相关文档

- [Skill 体系](./03-skills.md)
- [进化闭环](./02-evolution-loop.md)
- [Skill 管理 API](../api/09-skills-admin.md)
