# Agent registration retirement

New clients use [tenant Key + user_id](../agent-integrations/06-protocol-v2.md) directly.
The console provides user administration and tenant Replay runtime binding.

The compatibility release retains the old registration control plane for migrating old clients.
It is not a v2 prerequisite. Phase 6 removes registration, subject mappings and push management in
a separate release. See the [legacy protocol](../agent-integrations/02-protocol-v1.md) and
[upgrade guide](../guides/11-agent-deregistration.md).

The sections below keep the full V1 registration reference for the compatibility window.

The Agent registration interface registers new Agent runtimes with teamEvolver, declaring their identity (`agent_id` = integration_id), `runtime_type`, capabilities, callback endpoints, and metadata. V1 registration uses control plane key authentication. Registration **no longer issues any token**; it remains a required step: the registration record is the only source for the `integration_id → runtime_type → subject mapping`, and the only source for Replay capability resolution.

The Agent data plane now has exactly **one** machine credential: the **tenant machine credential** (format `tevt_<random>`). On multi-tenant deployments it is issued by the console when a tenant is created (`POST /api/tenants`), shown only once, and the server stores only its SHA-256 hash; rotate it via `POST /api/tenants/{tenant_id}/rotate-token` (the old credential dies immediately). On single-tenant deployments the operator configures it via the `TEAMEVOLVER_TENANT_TOKEN` environment variable. It is a tenant-wide full-privilege machine credential with no capability → scope narrowing, but Context Workspace calls must declare `integration_id` in the request; see [Context Workspace API](./04-context-workspace.md).

Code implementation: `teamEvolver/integrations/agent_registry.py:107` (`register_agent`)
Route entry point: `teamEvolver/proxy/routes.py:register_agent_runtime`

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

### Capability Details

| Capability | Detail Fields | Description |
|-----------|--------------|-------------|
| `session.ingest.v1` | None | Supports Session ingest |
| `context.workspace.v1` | `scopes` | Array of accessible Context scopes, options: `personal_memory`, `team_memory`, `personal_skills`, `team_skills` |
| `replay.branch.v1` | `transport`, `endpoint`, `orchestration`, `request_template`, `response_mapping`, `max_interactions`, `idempotent`, `auth_profile` | True Replay callback configuration. `orchestration: "server_driven"` enables the per-turn Turn protocol; `request_template`/`response_mapping` render per-turn requests and map response fields for plain HTTP endpoints. Fields not provided fall back to server defaults |
| `skill.sync.v1` | `transport`, `endpoint`, `auth_profile` | Skill Sync push configuration |

Server defaults are filled in by `teamEvolver/integrations/agent_registry.py:resolve_replay_capability`: `transport` (`http` when `endpoints.replay_url` exists, otherwise `local`), `max_interactions` (default 20), `idempotent` (default `false`).

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
| `subject_sync` | object | Subject sync result |
| `subject_sync.missing_user_ids` | array[string] | User IDs not found in mappings |

The response contains **no** credential fields (registration no longer issues tokens): the Agent data plane uses the tenant machine credential `tevt_<random>`.

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

### Server-Driven Replay Registration Example

An Agent running `scripts/replay_turn_server.py` registers with `orchestration: "server_driven"`; teamEvolver then calls its turn endpoint once per turn:

```bash
curl -X POST "http://localhost:52010/internal/agents/register" \
  -H "Authorization: Bearer my-control-plane-key" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "capabilities": {
      "replay.branch.v1": {
        "transport": "http",
        "orchestration": "server_driven",
        "endpoint": "http://<turn-server-host>:8010/turn/my-agent",
        "auth_profile": "my_agent"
      }
    },
    "endpoints": {
      "replay_url": "http://<turn-server-host>:8010/turn/my-agent"
    }
  }'
```

On the teamEvolver side export `TEAMEVOLVER_AGENT_MY_AGENT_REPLAY_API_KEY=<secret>`; the turn server receives the same secret via `--api-key`. The registration response contains no credential: registration only declares identity, and the Agent data plane uniformly uses the tenant machine credential.

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
  "updated_at": "2024-01-15T10:30:00Z"
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
4. Re-registering the same agent_id updates that Agent's identity, capabilities, and endpoint record (no token is issued or rotated—registration no longer issues tokens).
5. The Agent data plane has only one machine credential, the tenant machine credential (`tevt_`), which is independent of the registration flow: registration declares identity, while the credential comes from the console (multi-tenant) or `TEAMEVOLVER_TENANT_TOKEN` (single-tenant).
