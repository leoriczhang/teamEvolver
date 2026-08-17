# Agent Registration API

## 1. API Implementation Overview

The Agent registration interface is used to register new Agent runtimes with teamEvolver and obtain scoped access tokens. V1 registration uses control plane key authentication, where the Agent declares supported capabilities, callback endpoints, and metadata.

Upon first successful registration, the response includes `credentials.agent_access_token`. This token is returned only once; the server stores only its SHA-256 hash. Re-registering an existing agent_id will not re-issue a token unless `rotate_access_token: true` is specified in the request.

Code implementation: `teamEvolver/integrations/agent_registry.py:107` (`register_agent`)
Route entry point: `teamEvolver/proxy/routes.py:3366` (`register_agent_runtime`)

## 2. Interface and Parameter Specification

### Request

```
POST /internal/agents/register
Authorization: Bearer <EVOLVE_INGEST_API_KEY>
Content-Type: application/json
```

### Request Parameters

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `schema_version` | string | Yes | Must be `teamevolver.agent-registration.v1` |
| `protocol_version` | string | Yes | Protocol version, must match `^1\.` pattern (e.g., `1.0`) |
| `agent_id` | string | Yes | Agent unique identifier, format `runtime:tenant`, maximum 160 characters |
| `runtime_type` | string | Yes | Runtime type identifier (e.g., `hermes`, `agentshub`, `my-agent`) |
| `runtime_version` | string | No | Agent runtime version number |
| `display_name` | string | No | Display name, defaults to agent_id |
| `capabilities` | object/array | Yes | Declared supported capabilities, object format with detailed configuration recommended |
| `endpoints` | object | No | Agent callback endpoint configuration |
| `endpoints.health_url` | string(uri) | No | Health check URL |
| `endpoints.replay_url` | string(uri) | No | Replay callback URL (required when declaring replay.branch.v1) |
| `endpoints.skill_sync_url` | string(uri) | No | Skill Sync webhook URL |
| `auth` | object | No | Authentication configuration (excluding secrets) |
| `metadata` | object | No | Custom metadata (e.g., tenant_id) |
| `subject_mappings_authoritative` | boolean | No | Whether to authoritatively sync subject mappings, default false |
| `subject_mappings` | array | No | Subject mapping list, replaces existing mappings in authoritative mode |
| `subject_mappings[].external_subject` | string | No | Agent-side user identifier |
| `subject_mappings[].team_evolver_user_id` | string | No | teamEvolver user ID |
| `rotate_access_token` | boolean | No | Whether to rotate the access token, default false |

### Capability Details

| Capability | Detail Fields | Description |
|-----------|--------------|-------------|
| `session.ingest.v1` | None | Supports Session ingest |
| `context.workspace.v1` | `scopes` | Array of accessible Context scopes, options: `personal_memory`, `team_memory`, `personal_skills`, `team_skills` |
| `replay.branch.v1` | `transport`, `endpoint`, `max_interactions`, `supports_materials`, `supports_artifacts`, `supports_full_trace`, `idempotent`, `auth_profile` | True Replay callback configuration |
| `skill.sync.v1` | `transport`, `endpoint`, `auth_profile` | Skill Sync push configuration |

### Response

| Field | Type | Description |
|-------|------|-------------|
| `agent_id` | string | Agent unique identifier |
| `runtime_type` | string | Runtime type |
| `runtime_version` | string | Runtime version |
| `display_name` | string | Display name |
| `capabilities` | array[string] | Normalized capability list |
| `capability_ids` | array[string] | Normalized capability ID list (including alias mappings) |
| `endpoints` | object | Validated endpoint configuration |
| `status` | string | Status (`active`) |
| `created_at` | string(ISO8601) | Creation time |
| `updated_at` | string(ISO8601) | Update time |
| `credentials` | object | Credential information (returned only on first registration or rotation) |
| `credentials.agent_access_token` | string | Agent access token, format `tev1_<random>` |
| `subject_sync` | object | Subject sync result |
| `subject_sync.missing_user_ids` | array[string] | User IDs not found in mappings |
| `access_token_configured` | boolean | Whether access token is configured (in public records) |

## 3. Usage Examples

### Minimal Registration Example

```bash
curl -X POST "http://localhost:52010/internal/agents/register" \
  -H "Authorization: Bearer my-control-plane-key" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "runtime_version": "1.0.0",
    "capabilities": {
      "session.ingest.v1": {}
    }
  }'
```

### Complete Registration Example

```bash
curl -X POST "http://localhost:52010/internal/agents/register" \
  -H "Authorization: Bearer my-control-plane-key" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "runtime_version": "1.0.0",
    "display_name": "My Custom Agent",
    "capabilities": {
      "session.ingest.v1": {},
      "context.workspace.v1": {
        "scopes": ["personal_memory", "team_memory", "team_skills"]
      },
      "replay.branch.v1": {
        "transport": "http",
        "endpoint": "https://agent.example.com/replay",
        "max_interactions": 10,
        "supports_materials": true,
        "supports_full_trace": true,
        "auth_profile": "my_agent"
      },
      "skill.sync.v1": {}
    },
    "endpoints": {
      "health_url": "https://agent.example.com/health",
      "replay_url": "https://agent.example.com/replay",
      "skill_sync_url": "https://agent.example.com/skill-sync"
    },
    "metadata": {
      "tenant_id": "tenant-a"
    }
  }'
```

## 4. Response Contract and Error Handling

### Success Response Example

```json
{
  "schema_version": "teamevolver.agent-registration.v1",
  "protocol_version": "1.0",
  "runtime_version": "1.0.0",
  "agent_id": "my-agent:prod",
  "runtime_type": "my-agent",
  "display_name": "My Custom Agent",
  "capabilities": ["session.ingest.v1"],
  "capability_ids": ["session.ingest.v1"],
  "capability_details": {},
  "endpoints": {},
  "status": "active",
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z",
  "credentials": {
    "agent_access_token": "tev1_abcdef1234567890..."
  }
}
```

### Error Codes

| HTTP Status | Error Message | Cause |
|------------|--------------|-------|
| 401 | `invalid Agent control-plane key` | Incorrect control plane key |
| 503 | `EVOLVE_INGEST_API_KEY is required for Agent Protocol V1 registration` | Server not configured with control plane key |
| 400 | `agent_id is required` | Missing agent_id |
| 400 | `V1 registration cannot carry storage credentials` | V1 registration payload contains `storage` field (OpenViking credentials), not allowed in V1 |
| 400 | `Agent endpoint must be an HTTP(S) URL` | Invalid URL format in endpoints |
| 400 | `Agent endpoint cannot contain credentials` | URL contains username:password |
| 400 | `Agent endpoint targets a forbidden metadata host` | URL points to cloud metadata service |
| 400 | `Agent endpoint targets a forbidden IP address` | URL points to link-local/multicast/unspecified address |
| 400 | `unsupported registration schema` | Incorrect schema_version |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED: <version>` | protocol_version major version is not 1 |

### Important Notes

1. V1 registration payloads **must not** contain a `storage` field (OpenViking endpoint/key); in V1 mode all storage credentials remain on the teamEvolver server side.
2. Endpoint URLs must be http/https, must not contain credentials (user:pass@host), and must not point to metadata services (169.254.169.254 etc.) or private/link-local IP addresses.
3. Secret fields (key, token, secret, password, credential) in the registration payload are automatically stripped and not persisted to the registry.
4. Re-registering the same agent_id updates the record but does not automatically rotate the token; you must explicitly set `rotate_access_token: true`.
