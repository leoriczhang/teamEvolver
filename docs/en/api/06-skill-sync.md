# Skill Sync API

## 1. API Implementation Overview

Skill Sync is used to synchronize team Skills published/rolled back by teamEvolver to registered Agent runtimes in real-time. Two modes are supported:

1. **Pull Mode:** Agents actively pull the skill inventory via `GET /internal/agents/context/skills`, or obtain a complete bundle snapshot via `GET /sync/skills`.
2. **Push Mode:** When registering, Agents provide a `skill_sync_url`; teamEvolver sends webhook callbacks to this URL when Skills are published/rolled back/deleted, and requires Agents to return version verification confirmation.

Push mode supports idempotent delivery (`Idempotency-Key` header), failure retry, and acknowledgment verification. Agents must return version numbers and hash verification results for each Skill in the response; teamEvolver marks sync as successful only after verifying matches.

Code implementation: `teamEvolver/integrations/skill_sync_adapters.py`
Skill change delivery: `teamEvolver/skills/mutations.py` (SkillMutationService)
Lightweight snapshot endpoint: `teamEvolver/proxy/skills_admin.py:511` (`/sync/skills`)

## 2. Interface and Parameter Specification

### 2.1 Pull Mode: Get Skill Inventory

```
GET /internal/agents/context/skills?external_subject=<user>&scope=team
Authorization: Bearer <agent_access_token>
```

See the `GET /internal/agents/context/skills` section in [Context Workspace API](./04-context-workspace.md) for detailed interface documentation.

This interface returns each Skill's `name`, `qualified_skill_id`, and `context_ref`, which can be used for subsequent `read` to get full content.

### 2.2 Pull Mode: Complete Bundle Snapshot (Lightweight Agents)

```
GET /sync/skills
```

No authentication required (suitable for lightweight deployments like Hermes, deployed in internal networks). Returns complete file bundles (base64-encoded) for all team Skills.

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | `ok` or `error` |
| `source` | string | `shared` (pulled from OpenViking) or `local` (local skills directory) |
| `skills` | array | Skill list |
| `skills[].name` | string | Skill name |
| `skills[].version` | integer | Version number |
| `skills[].skill_id` | string | Skill ID |
| `skills[].files` | array | File list |
| `skills[].files[].path` | string | Relative path (e.g., `SKILL.md`) |
| `skills[].files[].content_b64` | string | File content (base64-encoded) |
| `total` | integer | Total Skill count |
| `error` | string | Error message (when status=error) |

Code: `teamEvolver/proxy/skills_admin.py:304` (`_sync_bundle_payload`)

### 2.3 Push Mode: Webhook Callback

When a Skill is published, rolled back, or deleted, teamEvolver sends a POST request to the Agent's registered `skill_sync_url`.

**Request Direction:**

```
teamEvolver --> POST https://<agent-skill-sync-url>
```

**Request Headers:**

| Header | Value |
|--------|-------|
| `Content-Type` | `application/json` |
| `Idempotency-Key` | `<event_id>:<agent_id>` (idempotency key) |
| `Authorization` | `Bearer <skill-sync-api-key>` (if auth_profile is configured) |

Skill Sync API Key is configured via environment variable: `TEAMEVOLVER_AGENT_<AUTH_PROFILE>_SKILL_SYNC_API_KEY` (auth_profile converted to UPPER_SNAKE_CASE). Earlier Pi Agent builds used the `validation_agentshub_api_key` configuration for compatibility.

Code: `teamEvolver/integrations/skill_sync_adapters.py:18` (`_sync_api_key`)

**Request Body (`teamevolver.skill-changed.v1`):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | Yes | `teamevolver.skill-changed.v1` |
| `protocol_version` | string | Yes | `1.0` |
| `event_id` | string | Yes | Event unique ID (`skill_evt_<hash>`) |
| `action` | string | Yes | Action type: `publish` (publish/update), `delete` (delete) |
| `job_id` | string | Yes | Change task ID (mutation_id) |
| `skills` | array | Yes | Changed Skill list |
| `skills[].name` | string | Yes | Skill name |
| `skills[].version` | integer | Yes | New version number |
| `skills[].sha256` | string | Yes | SKILL.md content SHA-256 |
| `skills[].tree_sha256` | string | No | Complete file tree SHA-256 |
| `skills[].action` | string | No | Same as top-level action |
| `tenant_ids` | array[string] | Yes | Target tenant ID list (multi-tenant filtering) |
| `expected_skills` | array | No | Same as skills (legacy compatibility field) |

**Multi-tenant Filtering:** If an Agent specifies a tenant ID in `metadata.tenant_id` during registration, teamEvolver only sends callbacks when that tenant's Skills change.

Code: `teamEvolver/integrations/skill_sync_adapters.py:41` (`_target_tenant_ids`)

### 2.4 Push Acknowledgment Response

After receiving the webhook and processing Skill updates, Agents must return an acknowledgment response:

**Response Fields:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `ok` | boolean | Yes | Must be `true` to indicate successful receipt |
| `results` | object | Yes | Verification results grouped by tenant ID |
| `results.<tenant_id>.verification` | object | Yes | Verification information |
| `results.<tenant_id>.verification.skills` | array | Yes | Verification result for each Skill |
| `results.<tenant_id>.verification.skills[].name` | string | Yes | Skill name |
| `results.<tenant_id>.verification.skills[].matched` | boolean | Yes | Whether name matched |
| `results.<tenant_id>.verification.skills[].actual_version` | integer | Required when action=publish | Local actual version number |
| `results.<tenant_id>.verification.skills[].actual_sha256` | string | Required when action=publish | Local SKILL.md SHA-256 |
| `results.<tenant_id>.verification.skills[].actual_tree_sha256` | string | No | Local file tree SHA-256 |
| `results.<tenant_id>.verification.skills[].removed` | boolean | Required when action=delete | Whether deleted |

teamEvolver verifies:
1. `ok` must be `true`;
2. `results` must contain verification results for each target tenant;
3. For publish: `matched=true`, `actual_version` equals expected version, `actual_sha256` matches;
4. For delete: `matched=true` and `removed=true`.

Failed verification marks sync as failed and enters the retry queue.

Code: `teamEvolver/integrations/skill_sync_adapters.py:63` (`_ack_matches`)

### 2.5 Retry Mechanism

- Failed sync events enter the outbox queue and are retried periodically;
- Retry eligibility is determined based on `next_retry_at` timestamp;
- When an Agent is disabled or the `skill.sync.v1` capability is removed, pending delivery events are marked `cancelled`;
- When an Agent is deregistered, related events are marked `cancelled`.

Code: `teamEvolver/integrations/skill_sync_adapters.py:115` (`_delivery_due`)

## 3. Usage Examples

### Pull Complete Bundle Snapshot (Hermes Mode)

```bash
curl -s "http://localhost:52010/sync/skills" | jq '.skills[] | {name, version, file_count: (.files | length)}'
```

Example response:

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

### Webhook Callback Handling Example (Python/Flask)

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

## 4. Response Contract and Error Handling

### Success Response

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

### Failure Scenarios

| Scenario | Handling |
|---------|---------|
| Agent did not declare `skill.sync.v1` capability | Skip, do not send |
| `skill_sync_url` not configured | Mark as `failed`, reason `skill_sync_url is not configured` |
| HTTP request failed/timeout | Mark as `failed`, enter retry queue |
| Agent returned `ok != true` | Mark as `failed`, reason `callback did not return ok=true` |
| Version/hash mismatch | Mark as `failed`, reason includes specific mismatch items |
| Agent disabled | Mark as `cancelled` |
| Agent deregistered | Mark as `cancelled` |

### Console Management Interfaces

| Method | Path | Description |
|--------|------|-------------|
| GET | `/api/agent-integrations` | List all registered Agents and their sync status |
| POST | `/api/agent-integrations/skill-sync/{event_id}/retry` | Retry failed sync event (admin) |
| POST | `/api/agent-integrations/skill-sync/{event_id}/discard` | Discard failed sync event (admin) |

Code: `teamEvolver/proxy/routes.py:3405` (`/api/agent-integrations`)
