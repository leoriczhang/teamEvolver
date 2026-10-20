# 团队 Skill 库 API

团队 Skill 库保存在 PostgreSQL：`skills` 记录当前版本，`skill_versions` 记录每个不可变版本，bundle 文件按内容寻址存储。每次写入在一个事务内生成新版本，所有 API 副本读取同一份数据。写入提交后会尽力刷新本机运行缓存，缓存失败只在响应的 `runtime_cache` 中报告，不影响已提交版本。

代码入口：`team_skills/api/__init__.py:create_router`、`team_skills/library/repository.py:SkillLibraryRepository`。

Agent 拉取见 [Skill 拉取](./06-skill-sync.md)。个人 Skill（`/api/users/{user_id}/skills*`）保存在 OpenViking 个人空间，见 [控制台身份与租户权限](./20-console-rbac.md#个人-skill-与发布申请)。

## 权限与通用规则

- 读取接口要求控制台登录（租户成员）或 Root Key；写入接口要求租户管理员。
- Skill 名称只允许字母、数字、`.`、`-`、`_`。
- Bundle 上限来自租户 `skills` 模块配置：`max_bundle_files`（默认 1024）、`max_bundle_bytes`（默认 64 MiB）、`max_file_bytes`（默认 16 MiB）。超限返回 413。
- 写入接口支持 `Idempotency-Key`：相同 Key 重试返回第一次的结果，不再生成版本。
- 写入请求体上限 96 MiB。

## 读取

### GET /api/skills

```json
{
  "skill_backend": "postgres",
  "delivery_mode": "pull",
  "skills": [{"name": "cost-report", "skill_id": "...", "version": 3, "description": "...", "category": "general",
              "sha256": "...", "tree_sha256": "...", "files": [], "file_count": 2, "runtime_policy": {},
              "updated_by": "admin", "updated_at": "..."}]
}
```

### GET /api/skills/{name}

当前版本，含 `body`（正文）、`skill_md`（完整 `SKILL.md`）、`files`、`file_count`、`version`、`updated_at`。

### GET /api/skills/{name}/versions

```json
{"name": "cost-report", "skill_id": "...", "current_version": 3, "versions": [1, 2, 3],
 "history": [{"version": 3, "action": "rollback", "timestamp": "...", "tree_sha256": "...", "actor": "admin", "restored_from": 1}]}
```

`versions` 不含删除产生的墓碑版本，`history` 包含全部版本。

### GET /api/skills/{name}/versions/{version}

指定版本的 `content`、`raw_md`、`files`（`path`、`sha256`、`size`）、`tree_sha256`、`is_current`，以及 `evolution`：该版本由哪个进化周期或 Candidate 产生。

## 写入

### POST /api/skills

创建或更新 `SKILL.md`，保留已有附件。

| 字段 | 说明 |
| --- | --- |
| `name` | 必填 |
| `skill_md` | 完整 `SKILL.md`；提供时忽略下列结构化字段 |
| `description` | 结构化编辑时必填 |
| `category` | 默认 `general` |
| `body` | 正文 |
| `base_version` | 可选；当前版本已变化返回 409 |

结构化编辑会保留现有 frontmatter 中的其他键。响应包含 `version`、`files`、`file_count`、`created`、`runtime_cache`。

### POST /api/skills/{name}/files

```json
{"files": [{"path": "scripts/run.py", "content_b64": "..."}]}
```

新增或替换附件。路径必须是 bundle 内的安全相对路径；`SKILL.md` 只能通过 `POST /api/skills` 修改。

### DELETE /api/skills/{name}/files/{rel_path}

删除一个附件，不能删除 `SKILL.md`。

### POST /api/skills/{name}/rollback?target_version=N

把版本 N 的内容发布为新的当前版本，返回 `restored_from` 与 `new_version`。

### DELETE /api/skills/{name}

写入墓碑版本，Skill 从列表和 Agent 拉取中消失，历史版本保留。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | 名称非法、字段缺失、路径不安全、base64 非法、尝试通过附件接口修改 `SKILL.md` |
| 403 | 非租户管理员 |
| 404 | Skill 或版本不存在 |
| 409 | `base_version` 冲突，或 `Idempotency-Key` 被用于不同请求 |
| 413 | Bundle 或请求体超限 |
| 503 | 租户 `skills` 模块配置缺失 |
