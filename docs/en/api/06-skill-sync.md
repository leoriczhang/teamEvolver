# Skill pull API

`GET /sync/skills?user_id=alice` requires `Authorization: Bearer tevt_...`.
The server resolves the principal, then returns that tenant's shared bundle. Skills are not filtered by user.
Anonymous/invalid credentials and invalid user IDs are rejected. Client tenant declarations cannot change the scope.

The sections below keep the full Skill Sync (pull + push) reference for the compatibility window.

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
GET /internal/agents/context/skills?external_subject=<user>&scope=team&integration_id=<agent_id>
Authorization: Bearer <tenant machine credential tevt_<random>>
```

`integration_id` is required and must be a registered, `active` Agent of the current tenant; authentication accepts only the tenant machine credential (`tevt_`).

See the `GET /internal/agents/context/skills` section in [Context Workspace API](./04-context-workspace.md) for detailed interface documentation.

This interface returns each Skill's `name`, `qualified_skill_id`, and `context_ref`, which can be used for subsequent `read` to get full content.

### 2.2 Pull Mode: Complete Bundle Snapshot (Lightweight Agents)

```
GET /sync/skills?user_id=<user>
Authorization: Bearer <tenant machine credential tevt_<random>>
```

Anonymous or invalid credentials and invalid user IDs are rejected (lightweight deployments like Hermes typically run in internal networks). Returns complete file bundles (base64-encoded) for the resolved tenant's team Skills.

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

Code: `teamEvolver/integrations/skill_sync_adapters.py:_sync_api_key`

**Request Body (`teamevolver.skill-changed.v1`):**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | Yes | `teamevolver.skill-changed.v1` |
| `protocol_version` | string | Yes | `1.0` |
| `event_id` | string | Yes | Event unique ID (`skill_evt_<hash>`) |
| `action` | string | Yes | Action type: `publish` (publish), `update` (update), `rollback` (rollback), `delete` (delete) |
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

Code: `teamEvolver/integrations/skill_sync_adapters.py:_target_tenant_ids`

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

Code: `teamEvolver/integrations/skill_sync_adapters.py:_ack_matches`

### 2.5 Retry Mechanism

- Failed sync events enter the outbox queue and are retried periodically;
- Retry eligibility is determined based on `next_retry_at` timestamp;
- When an Agent is disabled or the `skill.sync.v1` capability is removed, pending delivery events are marked `cancelled`;
- When an Agent is deregistered, related events are marked `cancelled`;
- After retries are exhausted, events enter the terminal `dead_letter` state and can be repaired via `SkillMutationService.reconcile()` or the management interfaces;
- Retry and discard support per-integration granularity (`integration_id`): discard writes `cancelled_at`/`cancelled_by`/`cancel_reason` on the delivery record and appends an entry to the event's `audit` list.

Code: `teamEvolver/integrations/skill_sync_adapters.py:_delivery_due`

## 3. Usage Examples

### Pull Complete Bundle Snapshot (Hermes Mode)

```bash
curl --fail "$TEAMEVOLVER_URL/sync/skills?user_id=alice"   -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"
```

The response includes `skills`, files, versions, hashes and an ETag. Send `If-None-Match` on
subsequent requests; unchanged content returns 304. Hermes caches
`{name: {version, sha256, tree_sha256}}` and leaves unchanged Skills untouched.
Failed pulls preserve the cache; changed identity/service configuration obtains the corresponding cache.

Publication uses `SkillMutationService`. With `delivery_mode=pull`, status is `published`;
no delivery outbox is created and missing recipients never become `synced`.
Pending push deliveries are cancelled with reason `delivery_mode_changed_to_pull`.
Push remains a rollback option during compatibility; final cleanup removes its worker and admin routes.

The Hermes `pre_llm_call` hook has a minimum request interval of 15 seconds.
Delivery means **visible before the next model call eligible for refresh**, not an automatic push within 15 seconds.
See [Hermes](../agent-integrations/03-hermes.md) and [Context Skill refs](./04-context-workspace.md).
Implementation: [client](../../../teamEvolver/integrations/hermes_skill_sync/sync_skills.py),
[mutations](../../../team_skills/library/mutations.py).
