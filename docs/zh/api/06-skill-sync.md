# Skill 同步 API

## 1. API 实现介绍

Skill Sync 用于将 teamEvolver 发布/回滚的团队 Skill 实时同步到已注册的 Agent 运行时。支持两种模式：

1. **拉取模式（Pull）**：Agent 通过 `GET /internal/agents/context/skills` 主动拉取技能清单，或通过 `GET /sync/skills` 获取完整 bundle 快照。
2. **推送模式（Push）**：Agent 注册时提供 `skill_sync_url`，teamEvolver 在 Skill 发布/回滚/删除时向该 URL 发送 webhook 回调，并要求 Agent 返回版本验证确认。

推送模式支持幂等投递（`Idempotency-Key` header）、失败重试和确认验证。Agent 必须在响应中返回每个 Skill 的版本号和哈希校验结果，teamEvolver 验证匹配后才标记为同步成功。

代码实现：`teamEvolver/integrations/skill_sync_adapters.py`
Skill 变更投递：`teamEvolver/skills/mutations.py`（SkillMutationService）
轻量快照端点：`teamEvolver/proxy/skills_admin.py:511` (`/sync/skills`)

## 2. 接口和参数说明

### 2.1 拉取模式：获取技能清单

```
GET /internal/agents/context/skills?external_subject=<user>&scope=team
Authorization: Bearer <agent_access_token>
```

详细接口文档见 [Context Workspace API](./04-context-workspace.md) 中的 `GET /internal/agents/context/skills` 部分。

此接口返回每个 Skill 的 `name`、`qualified_skill_id`、`context_ref`，可用于后续 `read` 获取完整内容。

### 2.2 拉取模式：完整 Bundle 快照（轻量 Agent）

```
GET /sync/skills
```

无需认证（适用于如 Hermes 等轻量部署，部署在内网环境）。返回所有团队 Skill 的完整文件 bundle（base64 编码）。

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `ok` 或 `error` |
| `source` | string | `shared`（从 OpenViking 拉取）或 `local`（本地 skills 目录） |
| `skills` | array | Skill 列表 |
| `skills[].name` | string | Skill 名称 |
| `skills[].version` | integer | 版本号 |
| `skills[].skill_id` | string | Skill ID |
| `skills[].files` | array | 文件列表 |
| `skills[].files[].path` | string | 相对路径（如 `SKILL.md`） |
| `skills[].files[].content_b64` | string | 文件内容（base64 编码） |
| `total` | integer | Skill 总数 |
| `error` | string | 错误信息（status=error 时） |

代码：`teamEvolver/proxy/skills_admin.py:304` (`_sync_bundle_payload`)

### 2.3 推送模式：Webhook 回调

当 Skill 发布、回滚或删除时，teamEvolver 向 Agent 注册的 `skill_sync_url` 发送 POST 请求。

**请求方向：**

```
teamEvolver --> POST https://<agent-skill-sync-url>
```

**请求头：**

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |
| `Idempotency-Key` | `<event_id>:<agent_id>`（幂等键） |
| `Authorization` | `Bearer <skill-sync-api-key>`（如配置了 auth_profile） |

Skill Sync API Key 通过环境变量配置：`TEAMEVOLVER_AGENT_<AUTH_PROFILE>_SKILL_SYNC_API_KEY`（auth_profile 转为大写下划线格式）。早期 Pi Agent 版本兼容使用 `validation_agentshub_api_key` 配置。

代码：`teamEvolver/integrations/skill_sync_adapters.py:18` (`_sync_api_key`)

**请求体（`teamevolver.skill-changed.v1`）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | `teamevolver.skill-changed.v1` |
| `protocol_version` | string | 是 | `1.0` |
| `event_id` | string | 是 | 事件唯一 ID（`skill_evt_<hash>`） |
| `action` | string | 是 | 操作类型：`publish`（发布/更新）、`delete`（删除） |
| `job_id` | string | 是 | 变更任务 ID（mutation_id） |
| `skills` | array | 是 | 变更的 Skill 列表 |
| `skills[].name` | string | 是 | Skill 名称 |
| `skills[].version` | integer | 是 | 新版本号 |
| `skills[].sha256` | string | 是 | SKILL.md 内容 SHA-256 |
| `skills[].tree_sha256` | string | 否 | 完整文件树 SHA-256 |
| `skills[].action` | string | 否 | 同顶层 action |
| `tenant_ids` | array[string] | 是 | 目标租户 ID 列表（多租户过滤） |
| `expected_skills` | array | 否 | 同 skills（遗留兼容字段） |

**多租户过滤：** 如果 Agent 注册时在 `metadata.tenant_id` 中指定了租户 ID，teamEvolver 仅在该租户的 Skill 变更时向其发送回调。

代码：`teamEvolver/integrations/skill_sync_adapters.py:41` (`_target_tenant_ids`)

### 2.4 推送确认响应

Agent 收到 webhook 后，处理完 Skill 更新，必须返回确认响应：

**响应字段：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `ok` | boolean | 是 | 必须为 `true` 表示接收成功 |
| `results` | object | 是 | 按租户 ID 分组的验证结果 |
| `results.<tenant_id>.verification` | object | 是 | 验证信息 |
| `results.<tenant_id>.verification.skills` | array | 是 | 每个 Skill 的验证结果 |
| `results.<tenant_id>.verification.skills[].name` | string | 是 | Skill 名称 |
| `results.<tenant_id>.verification.skills[].matched` | boolean | 是 | 名称是否匹配 |
| `results.<tenant_id>.verification.skills[].actual_version` | integer | action=publish 时必填 | 本地实际版本号 |
| `results.<tenant_id>.verification.skills[].actual_sha256` | string | action=publish 时必填 | 本地 SKILL.md SHA-256 |
| `results.<tenant_id>.verification.skills[].actual_tree_sha256` | string | 否 | 本地文件树 SHA-256 |
| `results.<tenant_id>.verification.skills[].removed` | boolean | action=delete 时必填 | 是否已删除 |

teamEvolver 会验证：
1. `ok` 必须为 `true`；
2. `results` 必须包含每个目标租户的验证结果；
3. publish 时：`matched=true`、`actual_version` 等于期望版本、`actual_sha256` 匹配；
4. delete 时：`matched=true` 且 `removed=true`。

验证失败会标记为同步失败并进入重试队列。

代码：`teamEvolver/integrations/skill_sync_adapters.py:63` (`_ack_matches`)

### 2.5 重试机制

- 同步失败的事件会进入 outbox 队列，定期重试；
- 基于 `next_retry_at` 时间戳判断是否到期重试；
- Agent 被禁用或取消 `skill.sync.v1` capability 时，待投递事件标记为 `cancelled`；
- Agent 注销时，相关事件标记为 `cancelled`。

代码：`teamEvolver/integrations/skill_sync_adapters.py:115` (`_delivery_due`)

## 3. 使用示例

### 拉取完整 Bundle 快照（Hermes 模式）

```bash
curl -s "http://localhost:52010/sync/skills" | jq '.skills[] | {name, version, file_count: (.files | length)}'
```

响应示例：

```json
{
  "status": "ok",
  "source": "shared",
  "skills": [
    {
      "name": "database-debugging",
      "version": 3,
      "skill_id": "database-debugging",
      "files": [
        {"path": "SKILL.md", "content_b64": "IyBEYXRhYmFzZSBEZWJ1Z2dpbmcK..."},
        {"path": "references/mysql-troubleshooting.md", "content_b64": "IyBNeVNRTCBUcm91Ymxlc2hvb3RpbmcK..."}
      ]
    }
  ],
  "total": 1
}
```

### Webhook 回调处理示例（Python/Flask）

```python
@app.post("/api/teamevolver/skill-sync")
def handle_skill_sync():
    body = request.json
    event_id = body["event_id"]
    action = body["action"]
    skills = body["skills"]
    tenant_ids = body.get("tenant_ids", [])

    results = {}
    for tid in tenant_ids:
        verifications = []
        for skill in skills:
            name = skill["name"]
            if action == "publish":
                local = download_and_apply_skill(name, skill["version"], skill.get("tree_sha256"))
                verifications.append({
                    "name": name,
                    "matched": local["matched"],
                    "actual_version": local["version"],
                    "actual_sha256": local["sha256"],
                    "actual_tree_sha256": local.get("tree_sha256", "")
                })
            elif action == "delete":
                removed = remove_skill(name)
                verifications.append({
                    "name": name,
                    "matched": True,
                    "removed": removed
                })
        results[tid] = {"verification": {"skills": verifications}}

    return jsonify({"ok": True, "results": results})
```

## 4. 响应契约与错误处理

### 成功响应

```json
{
  "event_id": "skill_evt_a1b2c3d4...",
  "results": {
    "tenant-a": {
      "status": "synced",
      "ack": {"ok": true, "results": {...}},
      "attempted": true
    }
  },
  "status": "synced"
}
```

### 失败场景

| 场景 | 处理 |
|------|------|
| Agent 未声明 `skill.sync.v1` capability | 跳过，不发送 |
| `skill_sync_url` 未配置 | 标记为 `failed`，原因 `skill_sync_url is not configured` |
| HTTP 请求失败/超时 | 标记为 `failed`，进入重试队列 |
| Agent 返回 `ok != true` | 标记为 `failed`，原因 `callback did not return ok=true` |
| 版本/哈希不匹配 | 标记为 `failed`，原因包含具体不匹配项 |
| Agent 已禁用 | 标记为 `cancelled` |
| Agent 已注销 | 标记为 `cancelled` |

### 控制台管理接口

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/agent-integrations` | 列出所有已注册 Agent 及其同步状态 |
| POST | `/api/agent-integrations/skill-sync/{event_id}/retry` | 重试失败的同步事件（管理员） |
| POST | `/api/agent-integrations/skill-sync/{event_id}/discard` | 丢弃失败的同步事件（管理员） |

代码：`teamEvolver/proxy/routes.py:3405` (`/api/agent-integrations`)
