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
| `execution_profile_id` | No | Pull a registered business Agent composition bound to this `user_id` |
| `execution_capability` | For compositions | `teamevolver.execution-plan.v1`; unsupported clients receive 409 |

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

Composition pull adds `execution`, containing `release_id`, `composition_hash`, `profile_id`, `document` and pinned `skills`. New clients must use `execution.skills` together with `execution.document.execution_plan`; outer `skills` retains the latest library view for older clients. Manual Skill changes do not replace a published composition's pinned dependency. ETags also cover composition changes, including Prompt-only releases.

Pure Prompt profiles may have zero Skills: `execution.skills` and `execution.document.skill_dependencies` are both `[]`, while Prompt/Context text remains in `execution.document.execution_plan.components`. An empty Skill list does not mean there is no update to load. Clients must validate the complete composition and load the actual business text without creating a placeholder `SKILL.md` or increasing a Skill version for a Prompt-only release.

`POST /sync/skills/execution-receipt?user_id=...` uses the same tenant Key. Its exact body fields are `profile_id`, `release_id`, `composition_hash`, `request_id`, and `state` (`downloaded` or `activated`). A request ID cannot be reused with different content. Receipts are `agent_reported`, not proof of actual model consumption. Prepare and validate the whole composition before atomically switching its active pointer; ongoing sessions pin the old composition.

See [Skill and business Prompt execution evolution](../concepts/12-execution-evolution.md) for integration and composition rollback. Publishing and rollback of team Skills are covered in [Team Skill Library](./09-skills-admin.md).
