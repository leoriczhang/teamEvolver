# Session Ingest API

## 1. API Implementation Overview

The Session ingest interface is used by Agents to submit complete session trajectory data (conversation turns, tool calls, skill usage, metrics, etc.) to teamEvolver. Ingested Sessions are filtered by a value classifier; valuable sessions enter the evolution queue to drive automatic optimization and evolution of Skills.

Under the V1 protocol, Session ingest uses Agent access token authentication, and must provide `runtime_context.external_subject` for subject mapping. The token must have the `session.ingest` scope.

For Agents already using Context Workspace, the `context_usage` field in turns is validated server-side to ensure the validity and ownership of context_refs.

Code implementation: `teamEvolver/proxy/routes.py:2971` (`ingest_session`)
Shared inbound pipeline: `teamEvolver/proxy/routes.py:1556` (`_ingest_session_dict`)
Protocol validation: `teamEvolver/integrations/agent_protocol.py:176` (`normalize_session_envelope`)
Context usage verification: `teamEvolver/integrations/context_workspace.py:514` (`verify_context_usage`)

## 2. Interface and Parameter Specification

### Request

```
POST /ingest_session
Authorization: Bearer <agent_access_token>
Content-Type: application/json
```

### Request Body (`teamevolver.agent-session.v1` schema)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | Yes | Must be `teamevolver.agent-session.v1` |
| `protocol_version` | string | Yes | Protocol version, `1.0` |
| `session_id` | string | Yes | Session unique identifier, maximum 160 characters |
| `runtime` | object | Yes | Runtime information |
| `runtime.type` | string | Yes | Runtime type (matches runtime_type at registration) |
| `runtime.integration_id` | string | Yes | Integration ID (must match agent_id bound to token) |
| `runtime.version` | string | No | Runtime version |
| `runtime.protocol_version` | string | No | Protocol version, defaults to `1.0` |
| `runtime_context` | object | Yes | Runtime context |
| `runtime_context.external_subject` | string | Yes | Agent-side user identifier, used for subject mapping |
| `runtime_context.username` | string | No | Username (legacy compatibility) |
| `runtime_context.team_evolver_user_id` | string | No | Specify teamEvolver user ID |
| `turns` | array | Yes | Conversation turns array, at least 1 turn |
| `turns[].turn_num` | integer | Yes | Turn sequence number, starting from 1 |
| `turns[].prompt_text` | string | No | User input text |
| `turns[].response_text` | string | No | Agent response text |
| `turns[].messages` | array | No | Complete message list (system/user/assistant/tool) |
| `turns[].tool_calls` | array | No | Tool call records |
| `turns[].tool_results` | array | No | Tool return results |
| `turns[].injected_skills` | array | No | Skills injected in this turn |
| `turns[].used_skills` | array | No | Skills actually used in this turn |
| `turns[].modified_skills` | array | No | Skills modified in this turn |
| `turns[].metrics` | object | No | Per-turn metrics (token consumption, etc.) |
| `turns[].context_usage` | object | No | Context usage information |
| `turns[].context_usage.context_snapshot_id` | string | No | Context snapshot ID |
| `turns[].context_usage.memory_refs` | array | No | Memory reference list used |
| `turns[].context_usage.skill_refs` | array | No | Skill reference list used |
| `turns[].context_usage.feedback` | object | No | User feedback |
| `metrics` | object | No | Session-level metrics |
| `metrics.interaction_turns` | integer | No | Total interaction turns |
| `metrics.tool_call_count` | integer | No | Total tool call count |
| `metrics.total_tokens` | integer | No | Total token consumption |
| `metrics.input_tokens` | integer | No | Input tokens |
| `metrics.output_tokens` | integer | No | Output tokens |
| `source_materials` | array | No | Source material list (code repositories, documents, etc.) |
| `system_prompt` | string | No | System prompt |
| `title` | string | No | Session title |
| `force_reprocess` | boolean | No | Force reprocessing of already processed Sessions |
| `defer_evolution_trigger` | boolean | No | Defer evolution cycle trigger (used for batch ingest) |

### Request Body Size Limit

Default maximum 32MB, adjustable via the `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` environment variable (minimum 1KB).

### Response

| Field | Type | Description |
|-------|------|-------------|
| `status` | string | Processing status: `queued` (enqueued), `duplicate` (duplicate skipped), `skipped` (no value skipped) |
| `session_id` | string | Session ID |
| `queued` | boolean | Whether entered the evolution queue |
| `key` | string | Queue key (returned only when queued) |
| `trigger_scheduled` | boolean | Whether evolution trigger is scheduled (returned only when queued) |
| `value_judge` | object | Value classification result (returned when skipped/queued) |

## 3. Usage Examples

```bash
curl -X POST "http://localhost:52010/ingest_session" \
  -H "Authorization: Bearer tev1_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-session.v1",
    "protocol_version": "1.0",
    "session_id": "sess-20240115-001",
    "runtime": {
      "type": "my-agent",
      "integration_id": "my-agent:prod",
      "version": "1.0.0",
      "protocol_version": "1.0"
    },
    "runtime_context": {
      "external_subject": "user-001"
    },
    "title": "Database connection troubleshooting",
    "system_prompt": "You are a helpful coding assistant.",
    "turns": [
      {
        "turn_num": 1,
        "prompt_text": "My database connection keeps timing out, how do I troubleshoot?",
        "response_text": "Database connection timeouts can be investigated from the following aspects...",
        "messages": [
          {"role": "user", "content": "My database connection keeps timing out, how do I troubleshoot?"},
          {"role": "assistant", "content": "Database connection timeouts can be investigated from the following aspects..."}
        ],
        "tool_calls": [],
        "tool_results": [],
        "injected_skills": ["database-debugging"],
        "used_skills": ["database-debugging"],
        "metrics": {
          "input_tokens": 520,
          "output_tokens": 890
        }
      }
    ],
    "metrics": {
      "interaction_turns": 1,
      "tool_call_count": 0,
      "total_tokens": 1410
    },
    "source_materials": []
  }'
```

## 4. Response Contract and Error Handling

### Success Response Example (Enqueued)

```json
{
  "status": "queued",
  "session_id": "sess-20240115-001",
  "queued": true,
  "key": "sess-20240115-001",
  "trigger_scheduled": true,
  "value_judge": {
    "decision": "valuable",
    "confidence": 0.92,
    "reason": "Contains specific technical problem and effective solution"
  }
}
```

### Success Response Example (Duplicate Skipped)

```json
{
  "status": "duplicate",
  "session_id": "sess-20240115-001",
  "queued": false
}
```

### Success Response Example (No Value Skipped)

```json
{
  "status": "skipped",
  "session_id": "sess-20240115-001",
  "queued": false,
  "value_judge": {
    "decision": "frivolous",
    "confidence": 0.85,
    "reason": "Casual conversation, no technical value"
  }
}
```

### Error Codes

| HTTP Status | Error Message | Cause |
|------------|--------------|-------|
| 401 | `invalid or insufficient Agent access token` | Invalid access token or missing session.ingest scope |
| 403 | `session runtime.integration_id does not match access token` | runtime.integration_id does not match agent_id bound to token |
| 403 | `SUBJECT_NOT_MAPPED` | runtime_context.external_subject not mapped to teamEvolver user |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED: <version>` | Protocol version major version is not 1 |
| 400 | `unsupported session schema: <schema>` | schema_version is not `teamevolver.agent-session.v1` |
| 400 | `V1 session session_id is required` | Missing session_id |
| 400 | `V1 session runtime.type is required` | Missing runtime.type |
| 400 | `V1 session runtime.integration_id is required` | Missing runtime.integration_id |
| 400 | `V1 session turns must be a non-empty list` | turns is empty or not an array |
| 400 | `invalid context_usage: <reason>` | References in context_usage are invalid or expired |
| 400 | `session body must be valid JSON` | Request body is not valid JSON |
| 400 | `session body must be an object` | Request body is not a JSON object |
| 413 | `session body exceeds <limit> bytes` | Request body exceeds size limit |
| 503 | `session storage is not configured` | Session storage not configured |

### Legacy Compatibility Mode

When the request body does not contain `schema_version` or is not identified as `teamevolver.agent-session.v1`, the system falls back to legacy compatibility mode, using a shared secret (`EVOLVE_INGEST_API_KEY`) for authentication, and mapping usernames via `user_alias`/`runtime_context.username`. This mode exists only for compatibility with legacy Agent integrations; new integrations should use the V1 protocol.
