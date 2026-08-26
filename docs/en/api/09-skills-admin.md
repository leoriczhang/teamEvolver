# Skill Management API

## 1. API Implementation Overview

The Skill Management API provides team-Skill CRUD, version rollback, and cloud sync, plus personal-Skill editing, personal/team copying, and publish requests. These endpoints require a console login; team-Skill writes and publish-request decisions require administrator access. Team-Skill changes sync to OpenViking and notify registered Agents through the Skill Sync outbox.

Code implementation: `teamEvolver/proxy/skills_admin.py` (`SkillsAdminMixin`)
Skill editor: `teamEvolver/skills/editor.py`
Cloud sync: `teamEvolver/skills/mutations.py` (`SkillMutationService`), `teamEvolver/skills/hub.py` (`SkillHub`)

## 2. Interface and Parameter Specification

All endpoints require a console login. Team-Skill writes, rollback, and publish-request decisions require administrator access; regular users may manage their own personal Skills.

---

### GET /api/skills

List all Skills in the local Skill library.

**Authentication:** Console Cookie (login required, no admin needed)

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `sharing_enabled` | boolean | Whether cloud sharing is enabled |
| `skills` | array | Skill summary list |

**Skill Summary Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Skill name |
| `description` | string | Description |
| `category` | string | Category |
| `files` | array[string] | Included file list |

---

### GET /api/skills/{name}

Get detailed information about a single Skill.

**Authentication:** Console Cookie

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name |

**Response:** Complete Skill details, including frontmatter parsing results and file list. Returns 404 if Skill does not exist.

---

### POST /api/skills

Create or update a Skill.

**Authentication:** Console Cookie (admin required)

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `name` | string | Yes | Skill name |
| `description` | string | Yes | Description |
| `category` | string | No | Category, default `general` |
| `body` | string | Yes | SKILL.md body content |
| `skill_md` | string | No | Raw SKILL.md content (raw edit mode, overrides body when provided) |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Skill name |
| `created` | boolean | Whether newly created |
| `dir` | string | Local directory path |
| `loaded_skills` | integer | Total Skill count after reload |
| `cloud` | object | Cloud sync result |
| `cloud.synced` | boolean | Whether sync succeeded |
| `cloud.event_id` | string | Sync event ID |

---

### DELETE /api/skills/{name}

Delete a Skill.

**Authentication:** Console Cookie (admin required)

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name |

**Response:** Deletion result, including cloud sync status.

---

### POST /api/skills/{name}/files

Add or replace bundle files to a Skill.

**Authentication:** Console Cookie (admin required)

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name |

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `files` | array | Yes | File list |
| `files[].path` | string | Yes | Relative path |
| `files[].content_b64` | string | Yes | File content (base64-encoded) |

**Response:** Update result, including cloud sync status.

---

### DELETE /api/skills/{name}/files/{rel_path}

Delete a bundle file from a Skill.

**Authentication:** Console Cookie (admin required)

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name |
| `rel_path` | string | Yes | File relative path |

---

### POST /api/skills/import-zip

Import a ZIP-packaged Skill.

**Authentication:** Console Cookie (admin required)

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `zip_b64` | string | Yes | ZIP file content (base64-encoded) |
| `name` | string | No | Override Skill name |

---

### GET /api/skills/{name}/versions

List cloud version history for a Skill.

**Authentication:** Console Cookie

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Skill name |
| `current_version` | integer | Current version |
| `history` | array | Version history list |
| `history[].version` | integer | Version number |
| `history[].created_at` | string | Creation time |
| `history[].message` | string | Version description |

**Caching:** Version list cached for 15 seconds.

---

### GET /api/skills/{name}/versions/{version}

Get detailed content and evolution context for a specified version.

**Authentication:** Console Cookie

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name |
| `version` | integer | Yes | Version number |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Skill name |
| `version` | integer | Version number |
| `content` | string | SKILL.md content |
| `files` | object | Bundle files |
| `evolution` | object | Evolution context (job info, evaluation results, diff) |
| `evolution.job_id` | string | Evolution job ID |
| `evolution.proposed_action` | string | Change type |
| `evolution.rationale` | string | Optimization rationale |
| `evolution.evaluation` | object | Evaluation result |
| `evolution.skill_diff` | string | Unified diff from previous version |

---

### POST /api/skills/{name}/rollback

Roll back a Skill to a specified version. This re-publishes the target version content as a new version and syncs to cloud and local.

**Authentication:** Console Cookie (admin required)

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `name` | string | Yes | Skill name |

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `target_version` | integer | Yes | Rollback target version number |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `name` | string | Skill name |
| `new_version` | integer | New version number (after rollback) |
| `loaded_skills` | integer | Total Skill count after reload |
| `event_id` | string | Sync event ID |

---

### Personal Skills and Team Publication

The following endpoints are implemented by `teamEvolver/proxy/users_admin.py`:

| Method and path | Permission | Purpose |
|-----------------|------------|---------|
| `GET /api/users/{user_id}/skills?space=personal|team` | User or administrator | List Skills in one space |
| `GET /api/users/{user_id}/skills/{name}?space=personal|team` | User or administrator | Read one Skill |
| `POST /api/users/{user_id}/skills` | User or administrator; team space is admin-only | Create or update a personal/team Skill |
| `DELETE /api/users/{user_id}/skills/{name}?space=personal|team` | User or administrator; team space is admin-only | Delete a Skill |
| `POST /api/users/{user_id}/share` | User or administrator; personal→team is admin-only | Copy selected Skills between personal and team spaces |
| `GET /api/skill-publish-requests` | Logged-in user | Users see their own requests; administrators see all requests |
| `POST /api/users/{user_id}/publish-requests` | User or administrator | Request publication of personal Skills to the team space |
| `POST /api/skill-publish-requests/{request_id}/approve` | Administrator | Approve and copy Skills into the team space |
| `POST /api/skill-publish-requests/{request_id}/reject` | Administrator | Reject a publish request |

A personal-Skill save body uses `space`, `name`, and full `skill_md`; alternatively provide `description`, `category`, and `body` and let the server build `SKILL.md`. Regular users cannot write the team space directly and must submit a publish request.

### GET /sync/skills

Get complete Skill Bundle snapshot (lightweight Agent pull endpoint, no admin authentication required).

**Authentication:** None (internal network endpoint)

See [Skill Sync API](./06-skill-sync.md) for detailed documentation.

---

### GET /skills-ui

Single-file Skill management UI page.

**Authentication:** None (HTML page)

## 3. Usage Examples

### List All Skills

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/skills"
```

Example response:

```json
{
  "sharing_enabled": true,
  "skills": [
    {
      "name": "database-debugging",
      "description": "Database troubleshooting guide",
      "category": "backend",
      "files": ["SKILL.md", "references/mysql-troubleshooting.md"]
    }
  ]
}
```

### Create/Update Skill

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

### Rollback to Specified Version

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/skills/database-debugging/rollback?target_version=2"
```

### Add Bundle File

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

## 4. Response Contract and Error Handling

### Error Codes

| HTTP Status | Error Message | Cause |
|------------|--------------|-------|
| 400 | Field validation error | Empty name, invalid content, ZIP format error, etc. |
| 401 | `login required` | Not logged in |
| 401 | `setup required` | Admin account not yet initialized |
| 403 | `only admin users can perform this operation` | Non-admin user performing write operations |
| 404 | Skill not found | Skill with specified name does not exist |
| 404 | `version unavailable` | Specified version does not exist or cannot be retrieved |

### Caching Notes

| Endpoint | Cache Duration |
|---------|---------------|
| `GET /api/skills` | No caching (reads local files directly) |
| `GET /api/skills/{name}/versions` | 15 seconds |
| `GET /api/skills/{name}/versions/{version}` | 30 seconds |

Any write operation (POST/PUT/DELETE/rollback) clears related caches and triggers Skill Manager reload, ensuring injected Skills are immediately available.

### Cloud Sync Behavior

- Write operations (create/update/delete/rollback) automatically trigger cloud sync;
- Sync failure does not affect local writes; response contains `cloud.synced: false` with failure reason;
- Successful cloud sync triggers Skill Sync webhook, notifying registered Agents to update local caches.
