# Skill 管理 API

## 1. API 实现介绍

Skill 管理 API 提供本地 Skill 库的 CRUD 操作、版本管理、发布回滚和云端同步功能。这些接口供 Web 控制台使用，需要管理员权限（控制台 Session Cookie + admin 角色）。所有变更会自动同步到共享云存储（OpenViking）并触发 Skill Sync webhook 通知已注册的 Agent。

代码实现：`teamEvolver/proxy/skills_admin.py`（`SkillsAdminMixin`）
Skill 编辑器：`teamEvolver/skills/editor.py`
云端同步：`teamEvolver/skills/mutations.py`（`SkillMutationService`）、`teamEvolver/skills/hub.py`（`SkillHub`）

## 2. 接口和参数说明

所有 `/api/skills/*` 接口需要控制台管理员认证。

---

### GET /api/skills

列出本地 Skill 库中的所有 Skill。

**认证：** 控制台 Cookie（无需 admin，登录即可）

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `sharing_enabled` | boolean | 是否启用云端共享 |
| `skills` | array | Skill 摘要列表 |

**Skill 摘要字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Skill 名称 |
| `description` | string | 描述 |
| `category` | string | 分类 |
| `version` | integer | 本地版本号 |
| `files` | array[string] | 包含的文件列表 |

---

### GET /api/skills/{name}

获取单个 Skill 的详细信息。

**认证：** 控制台 Cookie

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |

**响应：** Skill 完整详情，包含 frontmatter 解析结果和文件列表。Skill 不存在返回 404。

---

### POST /api/skills

创建或更新一个 Skill。

**认证：** 控制台 Cookie（必须 admin）

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |
| `description` | string | 是 | 描述 |
| `category` | string | 否 | 分类，默认 `general` |
| `body` | string | 是 | SKILL.md 正文内容 |
| `skill_md` | string | 否 | 原始 SKILL.md 内容（raw 编辑模式，提供时覆盖 body） |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Skill 名称 |
| `created` | boolean | 是否新创建 |
| `path` | string | 本地目录路径 |
| `loaded_skills` | integer | 重新加载后的 Skill 总数 |
| `cloud` | object | 云端同步结果 |
| `cloud.synced` | boolean | 同步是否成功 |
| `cloud.event_id` | string | 同步事件 ID |

---

### DELETE /api/skills/{name}

删除一个 Skill。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |

**响应：** 删除结果，包含云端同步状态。

---

### POST /api/skills/{name}/files

向 Skill 添加或替换 bundle 文件。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `files` | array | 是 | 文件列表 |
| `files[].path` | string | 是 | 相对路径 |
| `files[].content_b64` | string | 是 | 文件内容（base64 编码） |

**响应：** 更新结果，包含云端同步状态。

---

### DELETE /api/skills/{name}/files/{rel_path}

删除 Skill 中的一个 bundle 文件。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |
| `rel_path` | string | 是 | 文件相对路径 |

---

### POST /api/skills/import-zip

导入一个 ZIP 打包的 Skill。

**认证：** 控制台 Cookie（必须 admin）

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `zip_b64` | string | 是 | ZIP 文件内容（base64 编码） |
| `name` | string | 否 | 覆盖 Skill 名称 |

---

### GET /api/skills/{name}/versions

列出 Skill 的云端版本历史。

**认证：** 控制台 Cookie

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Skill 名称 |
| `current_version` | integer | 当前版本 |
| `history` | array | 版本历史列表 |
| `history[].version` | integer | 版本号 |
| `history[].created_at` | string | 创建时间 |
| `history[].message` | string | 版本说明 |

**缓存：** 版本列表缓存 15 秒。

---

### GET /api/skills/{name}/versions/{version}

获取指定版本的详细内容和进化上下文。

**认证：** 控制台 Cookie

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |
| `version` | integer | 是 | 版本号 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Skill 名称 |
| `version` | integer | 版本号 |
| `content` | string | SKILL.md 内容 |
| `files` | object | bundle 文件 |
| `evolution` | object | 进化上下文（job 信息、评估结果、diff） |
| `evolution.job_id` | string | 进化任务 ID |
| `evolution.proposed_action` | string | 变更类型 |
| `evolution.rationale` | string | 优化理由 |
| `evolution.evaluation` | object | 评估结果 |
| `evolution.skill_diff` | string | 与上一版本的 unified diff |

---

### POST /api/skills/{name}/publish

发布 Skill（触发云端同步和 Skill Sync webhook）。此接口通常由进化流程自动调用，管理员也可手动触发。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |

---

### POST /api/skills/{name}/rollback

回滚 Skill 到指定版本。会将目标版本内容重新发布为新版本，并同步到云端和本地。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `target_version` | integer | 是 | 回滚目标版本号 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `name` | string | Skill 名称 |
| `new_version` | integer | 新版本号（回滚后的版本） |
| `loaded_skills` | integer | 重新加载后的 Skill 总数 |
| `event_id` | string | 同步事件 ID |

---

### DELETE /api/skills/{name}/versions/{ver}

删除指定版本的历史记录（仅云端，不影响当前版本）。

**认证：** 控制台 Cookie（必须 admin）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `name` | string | 是 | Skill 名称 |
| `ver` | integer | 是 | 版本号 |

---

### GET /sync/skills

获取完整 Skill Bundle 快照（轻量 Agent 拉取端点，无需管理员认证）。

**认证：** 无（内网端点）

详细文档见 [Skill 同步 API](./06-skill-sync.md)。

---

### GET /skills-ui

单文件 Skill 管理 UI 页面。

**认证：** 无（HTML 页面）

## 3. 使用示例

### 列出所有 Skill

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/skills"
```

响应示例：

```json
{
  "sharing_enabled": true,
  "skills": [
    {
      "name": "database-debugging",
      "description": "Database troubleshooting guide",
      "category": "backend",
      "version": 3,
      "files": ["SKILL.md", "references/mysql-troubleshooting.md"]
    }
  ]
}
```

### 创建/更新 Skill

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/skills" \
  -d '{
    "name": "code-review",
    "description": "Code review best practices",
    "category": "general",
    "body": "# Code Review\n\n## Checklist\n- [ ] Function names are clear\n- [ ] Error handling is present\n"
  }'
```

### 回滚到指定版本

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/skills/database-debugging/rollback?target_version=2"
```

### 添加 bundle 文件

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/skills/database-debugging/files" \
  -d '{
    "files": [
      {
        "path": "references/postgres-tuning.md",
        "content_b64": "IyBQb3N0Z3JlUyBUdW5pbmcK..."
      }
    ]
  }'
```

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 400 | 字段校验错误 | 名称为空、内容无效、ZIP 格式错误等 |
| 401 | `login required` | 未登录 |
| 401 | `setup required` | 系统尚未初始化管理员账号 |
| 403 | `only admin users can perform this operation` | 非管理员用户执行写操作 |
| 404 | Skill 不存在 | 指定名称的 Skill 不存在 |
| 404 | `version unavailable` | 指定版本不存在或无法获取 |

### 缓存说明

| 端点 | 缓存时间 |
|------|---------|
| `GET /api/skills` | 无缓存（直接读本地文件） |
| `GET /api/skills/{name}/versions` | 15 秒 |
| `GET /api/skills/{name}/versions/{version}` | 30 秒 |

任何写操作（POST/PUT/DELETE/rollback）会清除相关缓存并触发 Skill Manager 重新加载，确保注入的 Skill 立即可用。

### 云端同步行为

- 写操作（create/update/delete/rollback）自动触发云端同步；
- 同步失败不影响本地写入，响应中 `cloud.synced: false` 并包含失败原因；
- 成功的云端同步会触发 Skill Sync webhook，通知已注册的 Agent 更新本地缓存。
