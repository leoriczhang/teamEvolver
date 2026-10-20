# Skill Pull API

Agents pull the full bundles of their tenant's team Skills before model calls. The authoritative team Skill versions are stored in PostgreSQL, so every API replica returns the same content. Delivery is pull-only; the server does not push to Agents.

Code entry point: `team_skills/api/__init__.py:create_router` (`_register_agent_pull`).

## Request

```
GET /sync/skills?user_id=<user_id>
Authorization: Bearer tevt_...
If-None-Match: "<previous ETag>"
```

| Parameter | Required | Description |
| --- | --- | --- |
| `user_id` | Yes | User ID within the tenant; rules in [Protocol v2](../agent-integrations/06-protocol-v2.md#identity) |
| `If-None-Match` | No | `ETag` of the previous response; returns 304 when unchanged |

## Response

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

Response headers:

| Header | Value |
| --- | --- |
| `ETag` | sha256 computed from `(tenant_id, account_id, [(name, version, tree_sha256)...])` |
| `Cache-Control` | `private, no-cache` |
| `Vary` | `Authorization` |

When `If-None-Match` equals the current `ETag` (or is `*`), the response is `304 Not Modified` with no body. Clients can compare `tree_sha256` per Skill and rewrite only changed directories.

## Errors

| Status | detail | Cause |
| --- | --- | --- |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | `user_id` missing or invalid |
| 401 | `TENANT_TOKEN_REQUIRED` | No valid tenant key |
| 503 | `TENANT_ACCOUNT_REQUIRED` | Tenant has no OpenViking Account configured |

## Example

```bash
curl -i "http://localhost:52010/sync/skills?user_id=alice" \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"
```

## CLI

```bash
teamEvolver skills --tenant demo list
teamEvolver skills --tenant demo pull --user-id alice
```

`list` calls `GET /api/skills` with `TEAMEVOLVER_ROOT_API_KEY`. `pull` calls `/sync/skills` with `TEAMEVOLVER_TENANT_TOKEN` and writes the bundles into the rebuildable local cache (`skills_runtime_cache_dir`).

Publishing and rollback of team Skills are covered in [Team Skill Library](./09-skills-admin.md).
