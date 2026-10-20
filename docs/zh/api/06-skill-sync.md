# Skill 拉取 API

Agent 在模型调用前拉取本租户团队 Skill 的完整 bundle。团队 Skill 的权威版本保存在 PostgreSQL，所有 API 副本返回相同内容。交付模式为 pull，服务端不向 Agent 推送。

代码入口：`team_skills/api/__init__.py:create_router`（`_register_agent_pull`）。

## 请求

```
GET /sync/skills?user_id=<user_id>
Authorization: Bearer tevt_...
If-None-Match: "<上次的 ETag>"
```

| 参数 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 租户内用户 ID，规则见 [协议 v2](../agent-integrations/06-protocol-v2.md#身份) |
| `If-None-Match` | 否 | 上次响应的 `ETag`，内容未变时返回 304 |

## 响应

```json
{
  "status": "ok",
  "source": "registry",
  "total": 1,
  "skills": [{
    "name": "cost-report",
    "skill_id": "...",
    "version": 3,
    "description": "...",
    "category": "general",
    "runtime_policy": {},
    "sha256": "...",
    "tree_sha256": "...",
    "files": [{"path": "SKILL.md", "content_b64": "..."}]
  }]
}
```

响应头：

| Header | 值 |
| --- | --- |
| `ETag` | 由 `(tenant_id, account_id, [(name, version, tree_sha256)...])` 计算的 sha256 |
| `Cache-Control` | `private, no-cache` |
| `Vary` | `Authorization` |

`If-None-Match` 与当前 `ETag` 相同（或为 `*`）时返回 `304 Not Modified`，无响应体。客户端可按 `tree_sha256` 判断单个 Skill 是否变化，只重写变化的目录。

## 错误

| 状态 | detail | 原因 |
| --- | --- | --- |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | `user_id` 缺失或非法 |
| 401 | `TENANT_TOKEN_REQUIRED` | 未使用有效租户 Key |
| 503 | `TENANT_ACCOUNT_REQUIRED` | 租户未配置 OpenViking Account |

## 示例

```bash
curl -i "http://localhost:52010/sync/skills?user_id=alice" \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"
```

## CLI

```bash
teamEvolver skills --tenant demo list
teamEvolver skills --tenant demo pull --user-id alice
```

`list` 使用 `TEAMEVOLVER_ROOT_API_KEY` 调用 `GET /api/skills`。`pull` 使用 `TEAMEVOLVER_TENANT_TOKEN` 调用 `/sync/skills`，并把 bundle 写入本机可重建缓存（`skills_runtime_cache_dir`）。

团队 Skill 的发布与回滚见 [团队 Skill 库](./09-skills-admin.md)。
