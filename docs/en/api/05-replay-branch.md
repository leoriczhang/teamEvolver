# Replay Branch Execution API

## 1. API Implementation Overview

The Replay Branch Execution API is a callback interface that teamEvolver initiates to registered Agents. Unlike other Agent APIs, Replay requests are **actively sent by teamEvolver to the Agent's `replay_url`**, not the other way around. When teamEvolver validates candidate Skills, it concurrently sends replay requests for both baseline and candidate branches to the Agent, comparing their execution results.

Replay requests include frozen context projections, task instructions, and execution limits (timeout, maximum interaction turns). Agents must execute in an isolated sandbox without producing external side effects. Successful results must include efficiency metrics (interaction_turns, tool_call_count, total_tokens) and `context_input_hash` (hash of actually injected context).

Code implementation: `teamEvolver/integrations/replay_adapters.py`
Protocol validation: `teamEvolver/integrations/agent_protocol.py:259` (`normalize_replay_request`, `normalize_replay_result`)
True Replay engine: `teamEvolver/true_replay.py`

## 2. Interface and Parameter Specification

### Request Direction

```
teamEvolver --> POST https://<agent-replay-url>
```

### Request Headers

| Header | Value |
|--------|-------|
| `Content-Type` | `application/json` |
| `Authorization` | `Bearer <replay-api-key>` (if auth_profile is configured) |

Replay API Key is configured via environment variables, with naming convention `TEAMEVOLVER_AGENT_<AUTH_PROFILE>_REPLAY_API_KEY` (auth_profile converted to UPPER_SNAKE_CASE). For example, when auth_profile is `my_agent`, the environment variable is `TEAMEVOLVER_AGENT_MY_AGENT_REPLAY_API_KEY`.

Code: `teamEvolver/integrations/replay_adapters.py:27` (`resolve_replay_api_key`)

### Request Body (`teamevolver.replay-branch-request.v1`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | Yes | `teamevolver.replay-branch-request.v1` |
| `protocol_version` | string | Yes | `1.0` |
| `request_id` | string | Yes | Request unique ID (format `replay_<sha256-hash>`), returned as-is in response |
| `job_id` | string | Yes | Validation job ID |
| `branch` | string | Yes | Branch type: `baseline` (current Skill) or `candidate` (candidate Skill) |
| `case` | object | Yes | Test case |
| `case.query` | string | Yes | Task instruction/user query |
| `case.instruction` | string | No | Same as query (compatibility field) |
| `case.materials` | array | No | Source material list |
| `limits` | object | Yes | Execution limits |
| `limits.timeout_seconds` | integer | Yes | Timeout in seconds, 30-3600, default 600 |
| `limits.max_interactions` | integer | Yes | Maximum interaction turns, 1-20 |
| `context_snapshot` | object | No | Frozen context projection (resolve result snapshot) |
| `frozen_context` | object | No | Frozen context (same as context_snapshot) |
| `skill` | object | No | Candidate Skill content (when branch=candidate) |
| `current_skill` | object | No | Current Skill content (when branch=baseline) |
| `target_skill_name` | string | No | Target Skill name |
| `source_session` | object | No | Source Session data |

### Timeout Control

- The caller (teamEvolver) sets HTTP timeout to `timeout_seconds + 30` seconds.
- The Agent **must** stop execution within `limits.timeout_seconds`, and must not continue consuming model or tool resources after the HTTP caller times out.
- Baseline and candidate requests are sent concurrently, sharing the same deadline.

### Response Body (`teamevolver.replay-branch-result.v1`)

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `schema_version` | string | Yes | `teamevolver.replay-branch-result.v1` |
| `protocol_version` | string | Yes | `1.0` |
| `request_id` | string | Yes | Must exactly match request's request_id |
| `branch` | string | Yes | Must exactly match request's branch |
| `status` | string | Yes | Execution status: `succeeded`, `failed`, `unsupported` |
| `metrics` | object | Required when status=succeeded | Efficiency metrics |
| `metrics.interaction_turns` | integer | Yes | Interaction turns, non-negative integer |
| `metrics.tool_call_count` | integer | Yes | Tool call count, non-negative integer |
| `metrics.total_tokens` | integer | Yes | Total token consumption, non-negative integer |
| `metrics.input_tokens` | integer | No | Input tokens |
| `metrics.output_tokens` | integer | No | Output tokens |
| `metrics.cache_read_tokens` | integer | No | Cache read tokens |
| `metrics.reasoning_tokens` | integer | No | Reasoning tokens |
| `metrics.api_calls` | integer | No | API call count |
| `output` | object | No | Output result |
| `output.final_response` | string | No | Final response text |
| `trace` | object | No | Execution trace (recommended when supports_full_trace=true) |
| `trace.messages` | array | No | Message list |
| `trace.events` | array | No | Event list |
| `trace.interactions` | array | No | Interaction records |
| `artifacts` | array | No | Artifact list (when supports_artifacts=true) |
| `context_input_hash` | string | Recommended | SHA-256 hash of actually injected context, used to verify consistency between branches |
| `runtime_checklist_report` | object | No | Checklist execution results |
| `checklist_evidence` | object | No | Checklist evidence |
| `error` | object | Required when status!=succeeded | Error information |
| `error.code` | string | Yes | Error code |
| `error.message` | string | Yes | Error description |
| `error.retryable` | boolean | Yes | Whether retryable |
| `elapsed_seconds` | number | No | Execution time in seconds |

### Error Codes

Agent-returned error codes (`error.code`):

| Error Code | Description | retryable |
|-----------|-------------|-----------|
| `EXECUTION_FAILED` | Execution failed (generic error) | false |
| `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` | Encountered external tool call that cannot be deterministically replayed | false |
| `TIMEOUT` | Execution timeout | false |
| `HTTP_ERROR` | HTTP communication error | Depends |

teamEvolver-side adapter error codes:

| Error Code | Description |
|-----------|-------------|
| `INVALID_RESPONSE` | Agent returned invalid format, request_id/branch mismatch, missing metrics |
| `TIMEOUT` | HTTP request timeout |
| `HTTP_ERROR` | HTTP connection error or non-2xx response |

## 3. Isolation Requirements

The Agent's Replay runtime must meet the following isolation requirements:

1. **Data Isolation:** Only instantiate source tenant/user/runtime configurations required for the branch; do not load the complete production database or production credentials.
2. **Context Determinism:** Inject the frozen Context projection actually used at runtime (from the request's `context_snapshot`); do not perform new context searches or resolution. Return `context_input_hash` as the hash of actually injected content.
3. **Credential Isolation:** Keep upstream model credentials outside the candidate-side control process, behind a short-lived parent broker. Worker processes must not directly hold model API Keys.
4. **Network Isolation:** Place Workers in a private network namespace, with their local model sidecar connecting to the parent broker through a protected Unix socket. Direct external network access is prohibited.
5. **Filesystem Isolation:** The branch workspace is the only writable host path.
6. **External Tool Policy:**
   - Workspace-local tools (file read/write, code search, etc.) may execute normally within the sandbox.
   - Recorded external tool calls: Deterministically replay results after matching by tool name + normalized parameter signature + call sequence + result SHA-256. Matching by tool name alone does not meet protocol specifications.
   - Unrecorded network/external tools: Return `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` (fail-closed); do not fall back to live calls.

Pi Agent currently implements the `external_tool_replay=fail-closed` policy: workspace-local tools execute within the sandbox, while network-capable tools directly mark cases as unrunnable when encountering unrecorded calls.

## 4. Checklist and Efficiency Comparison

### Checklist Gate

Checklist completion is a pass/fail gate condition, not a weighted score. Each checklist item must explicitly pass/fail. The candidate branch must pass all checklist items to be accepted.

### Efficiency Comparison Dimensions

Efficiency comparison is prioritized as follows (lower is better):

1. `interaction_turns` -- Interaction turns
2. `tool_call_count` -- Tool call count
3. `total_tokens` -- Total token consumption

With all checklist items passing, the candidate branch is automatically accepted only if it is no worse than baseline (no_regression).

## 5. Usage Examples

### Example baseline request sent by teamEvolver

```json
{
  "schema_version": "teamevolver.replay-branch-request.v1",
  "protocol_version": "1.0",
  "request_id": "replay_a1b2c3d4e5f6...",
  "job_id": "job-20240115-001",
  "branch": "baseline",
  "case": {
    "query": "How do I configure the maximum connection count for a database connection pool?",
    "materials": []
  },
  "limits": {
    "timeout_seconds": 600,
    "max_interactions": 4
  },
  "context_snapshot": {
    "snapshot_id": "ctxsnap_...",
    "items": []
  },
  "current_skill": {
    "name": "database-config",
    "content": "# Database Configuration\n..."
  }
}
```

### Example successful response returned by Agent

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_a1b2c3d4e5f6...",
  "branch": "candidate",
  "status": "succeeded",
  "metrics": {
    "interaction_turns": 2,
    "tool_call_count": 3,
    "total_tokens": 3200,
    "input_tokens": 2800,
    "output_tokens": 400
  },
  "output": {
    "final_response": "The method to configure maximum connections for a database connection pool is as follows..."
  },
  "trace": {
    "messages": [],
    "events": [],
    "interactions": []
  },
  "context_input_hash": "sha256:abc123def456...",
  "runtime_checklist_report": {
    "provides_code_example": {"passed": true},
    "mentions_default_value": {"passed": true}
  },
  "elapsed_seconds": 12.5
}
```

### Example unsupported response returned by Agent (external tool not replayable)

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_a1b2c3d4e5f6...",
  "branch": "candidate",
  "status": "unsupported",
  "metrics": {},
  "error": {
    "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
    "message": "external tool call 'send_email' cannot be deterministically replayed",
    "retryable": false
  },
  "elapsed_seconds": 2.1
}
```

## 6. JSON Schema Reference

| Schema | Path |
|--------|------|
| Replay Request | `docs/schemas/replay-branch-request-v1.schema.json` |
| Replay Result | `docs/schemas/replay-branch-result-v1.schema.json` |

### Legacy Compatibility

Earlier Pi Agent builds used different request/response formats. `teamEvolver/integrations/replay_adapters.py:141` (`LegacyAgentsHubHttpAdapter`) provides an adapter for a compatibility period, converting legacy formats to V1 standard format. Newly integrated Agents should implement the V1 format directly.
