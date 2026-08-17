# Session Query API

## 1. API Implementation Overview

Session query interfaces are used to view pending Sessions in the queue and processed conversation history. These interfaces are primarily used by the web console and require console Session Cookie authentication (obtained after login). The `/sessions` and `/conversations` endpoints can also be accessed without authentication (designed for internal network deployment), but response data may be restricted.

Code implementation: `teamEvolver/proxy/routes.py` (`dashboard_sessions`, `dashboard_conversations`, `dashboard_conversation_detail`)
Session storage: `teamEvolver/session_store.py`

## 2. Interface and Parameter Specification

---

### GET /sessions

List Sessions waiting in the queue for evolution processing.

**Authentication:** Console Cookie (recommended), also accessible without authentication on internal networks

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | integer | No | Items per page, 1-200, default 20 |
| `offset` | integer | No | Pagination offset, default 0 |
| `refresh` | boolean | No | Whether to force refresh cache, default false |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `reachable` | boolean | Whether Session storage is reachable |
| `sessions` | array | Current page Session list |
| `pending` | integer | Total pending count |
| `total` | integer | Total queue count |
| `limit` | integer | Current page size |
| `offset` | integer | Current offset |
| `has_more` | boolean | Whether more data exists |

**Caching:** Queue list cached for 5 seconds.

---

### GET /conversations

List processed conversation history (archived conversations).

**Authentication:** Console Cookie (recommended), also accessible without authentication on internal networks

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | integer | No | Items per page, 1-200, default 20 |
| `offset` | integer | No | Pagination offset, default 0 |
| `refresh` | boolean | No | Whether to force refresh cache, default false |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `reachable` | boolean | Whether storage is reachable |
| `conversations` | array | Current page conversation list |
| `total` | integer | Total conversation count |
| `limit` | integer | Current page size |
| `offset` | integer | Current offset |
| `has_more` | boolean | Whether more data exists |
| `reason` | string | Unreachability reason |

**Caching:** Conversation list cached for 15 seconds.

---

### GET /conversations/{session_id}

Get detailed information about a single conversation.

**Authentication:** Console Cookie (recommended)

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | Session ID |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `meta.title` | string | Conversation title |
| `meta.user_alias` | string | User alias |
| `meta.status` | string | Processing status |
| `meta.num_turns` | integer | Number of turns |
| `turns_available` | boolean | Whether turn details are available |
| `turns_source` | string | Turns source (`archive`) |
| `system_prompt` | string | System prompt |
| `injected_skills` | array | Injected Skill list |
| `used_skills` | array | Used Skill list |
| `metrics` | object | Conversation metrics |
| `turns` | array | Turn details |
| `value_judge` | object | Value classification result |

---

### GET /conversations/{session_id}/process

Get evolution processing history (cycle records) for a specified conversation.

**Authentication:** Console Cookie (recommended)

**Path Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `session_id` | string | Yes | Session ID |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `cycles` | array | Evolution cycle record list |

---

### POST /conversations/status

Batch query processing status of multiple Sessions.

**Authentication:** Console Cookie

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `session_ids` | array[string] | Yes | Session ID list, maximum 500 entries |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `reachable` | boolean | Whether storage is reachable |
| `statuses` | object | Session ID -> status mapping |
| `reason` | string | Unreachability reason |

---

### GET /history

Get evolution cycle history records (read from `evolve_history.jsonl` or archived Sessions).

**Authentication:** None (internal interface)

**Query Parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `limit` | integer | No | Return count, default 50 |
| `session_id` | string | No | Filter records for specified Session |

**Response Fields:**

| Field | Type | Description |
|-------|------|-------------|
| `cycles` | array | Evolution cycle list |

## 3. Usage Examples

### View Pending Queue

```bash
curl "http://localhost:52010/sessions?limit=5"
```

Example response:

```json
{
  "reachable": true,
  "sessions": [
    {
      "session_id": "sess-20240115-001",
      "user_alias": "alice",
      "status": "queued",
      "ingested_at": "2024-01-15T10:30:00Z",
      "value_judge": {"decision": "valuable", "confidence": 0.92}
    }
  ],
  "pending": 3,
  "total": 3,
  "limit": 5,
  "offset": 0,
  "has_more": false
}
```

### View Conversation History

```bash
curl "http://localhost:52010/conversations?limit=10&offset=0"
```

### View Conversation Details

```bash
curl "http://localhost:52010/conversations/sess-20240115-001"
```

### View Evolution Processing History

```bash
curl "http://localhost:52010/conversations/sess-20240115-001/process"
```

Example response:

```json
{
  "cycles": [
    {
      "timestamp": "2024-01-15T10:35:00Z",
      "session_ids": ["sess-20240115-001"],
      "sessions": 1,
      "judge": {
        "overall_score": 0.85,
        "decision": "accept",
        "rationale": "Skill optimization improves efficiency"
      },
      "evolutions": [
        {
          "skill_name": "database-debugging",
          "action": "update",
          "version": 4
        }
      ],
      "status": "published"
    }
  ]
}
```

## 4. Response Contract and Error Handling

### Error Codes

| HTTP Status | Error Message | Cause |
|------------|--------------|-------|
| 400 | `session_id is required` | session_id parameter empty or invalid characters |
| 401 | `login required` | Accessing interfaces requiring console authentication without login (`/api/*` paths) |
| 404 | `session not found` | Specified session_id does not exist |
| 503 | Storage error message | Session storage unavailable |

### Pagination Conventions

- `limit` range 1-200, values outside range are automatically truncated;
- `offset` starts from 0;
- `has_more: true` indicates more pages available;
- `total` is the total count of matching records, usable for calculating total pages.

### Important Notes

1. Conversation detail interfaces require console login first (to obtain Cookie). `/sessions` and `/conversations` are under non-`/api/` paths and allow unauthenticated access to simplify internal network deployment, but adding an authentication layer via reverse proxy is recommended in production.
2. Session IDs only allow letters, numbers, underscores, dots, and hyphens; other characters are replaced with `-`.
3. Processing history is read preferentially from `evolve_history.jsonl`; if the file does not exist, falls back to archived Session data.
