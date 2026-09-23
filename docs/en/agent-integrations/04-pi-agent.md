# Pi Agent / AgentsHub integration

Follow [custom v2 integration](./05-custom-agent.md) with a service URL, tenant Key and user ID.
`runtime.type` can identify the actual runtime, such as `pi` or `agentshub`; it is not authentication.
Context, Skill pull and ownership follow [v2](./06-protocol-v2.md).

The deployment Owner provides a customer Python Replay adapter and an administrator binds it to the tenant.
Prefer standard factory sessions for turn-by-turn execution. The old AgentsHub branch-only wrapper is
explicit compatibility only. Every transport must preserve isolated A/B sessions/workspaces and keep
Checklists out of downstream input. See [Replay API](../api/05-replay-branch.md).

The sections below keep the detailed V1-era Pi Agent integration reference for the compatibility window.

## Pi Agent Overview

Pi Agent runs the agent loop as a subprocess (launched via `posix_spawn + setsid` in `pi_rpc_worker.py` to avoid fork/vfork instability), with the following core characteristics:

- **Runtime mode**: Subprocess CLI Agent communicating with host via RPC
- **Tool set**: `bash` (terminal commands), `file` (file read/write), `builtin` (built-in tools), `mcp` (MCP tools)
- **Sandbox model**: systemd-user level isolation using `PrivateNetwork=yes` network namespace + Unix Socket Model Broker
- **Process launch**: `posix_spawn + setsid` for stable startup, avoiding segmentation faults
- **External tool replay policy**: fail-closed (external calls that cannot be deterministically replayed are rejected, no fallback to live calls)

Reference integration code (host side):

```
/path/to/AgentsHub/backend/app/integrations/team_evolver.py
/path/to/AgentsHub/backend/app/integrations/team_evolver_replay.py
/path/to/AgentsHub/backend/app/core/pi_agent.py
```

## Declared Capabilities

Pi Agent declares the following V1 capabilities on registration:

| Capability ID | Description |
|---------------|-------------|
| `session.ingest.v1` | Reports complete Session trajectories via async job queue (async-job delivery) |
| `context.workspace.v1` | Uses Context Workspace to fetch personal/team Memory and Skill context |
| `replay.branch.v1` | Supports True Replay HTTP branch execution, external tools fail-closed |
| `skill.sync.v1` | Receives Skill publish/rollback HTTP webhook pushes |
| `memory.personal.read.v1` | Reads personal Memory |
| `memory.personal.write.v1` | Writes personal Memory |
| `memory.team.read.v1` | Reads team Memory |
| `skill.personal.read.v1` | Reads personal Skills |
| `skill.team.read.v1` | Reads team Skills |
| `skill.team.evolve.v1` | Participates in team Skill evolution |
| `skill.bundle.v1` | Supports `bundle_v1` format Skill Bundle installation |

## Registration Flow

### 1. Obtain Control Plane Key

Configure the environment variable `EVOLVE_INGEST_API_KEY` in your Pi Agent deployment, matching the teamEvolver service control plane registration key.

### 2. Send Registration Request

```
POST /internal/agents/register
Authorization: Bearer <EVOLVE_INGEST_API_KEY>
Content-Type: application/json
```

Registration payload example:

```json
{
  "schema_version": "teamevolver.agent-registration.v1",
  "protocol_version": "1.0",
  "agent_id": "pi:<tenant-id>",
  "runtime_type": "pi",
  "runtime_class": "pi",
  "runtime_version": "<pi-version>",
  "display_name": "Pi Agent",
  "capabilities": {
    "session.ingest.v1": {"delivery": "async-job"},
    "context.workspace.v1": {
      "scopes": [
        "personal_memory", "team_memory",
        "personal_skills", "team_skills"
      ],
      "operations": [
        "resolve", "read", "skills",
        "remember", "forget", "session"
      ]
    },
    "replay.branch.v1": {
      "transport": "http",
      "endpoint": "https://<pi-host>/api/internal/team-evolver/replay",
      "max_interactions": 20,
      "supports_materials": true,
      "supports_artifacts": true,
      "supports_full_trace": true,
      "idempotent": false,
      "runtime": "pi",
      "sandbox": "systemd-user",
      "network_policy": "private-network+unix-broker",
      "external_tool_replay": "fail-closed",
      "tools": ["bash", "file", "builtin", "mcp"]
    },
    "skill.sync.v1": {
      "transport": "http",
      "endpoint": "https://<pi-host>/api/internal/team-evolver/sync"
    },
    "memory.personal.read.v1": {},
    "memory.personal.write.v1": {},
    "memory.team.read.v1": {},
    "skill.personal.read.v1": {},
    "skill.team.read.v1": {},
    "skill.team.evolve.v1": {},
    "skill.bundle.v1": {"formats": ["bundle_v1"]}
  },
  "endpoints": {
    "health_url": "https://<pi-host>/api/health",
    "replay_url": "https://<pi-host>/api/internal/team-evolver/replay",
    "skill_sync_url": "https://<pi-host>/api/internal/team-evolver/sync"
  },
  "auth": {"replay_profile": "pi"},
  "metadata": {
    "tenant_id": "<tenant-id>",
    "platform": "linux",
    "tools": ["bash", "file", "builtin", "mcp"]
  },
  "subject_mappings_authoritative": true,
  "subject_mappings": []
}
```

### 3. Obtain and Persist the Tenant Machine Credential

Registration **issues no token at all**: it only declares identity (`agent_id` = integration_id), runtime_type, capabilities, and endpoints. The credential needed for Agent API calls is supplied by the operator:

- Multi-tenant deployments: obtain the **tenant machine credential** (format `tevt_<random>`) from the console when the tenant is created; it is shown only once. Pi Agent must persist this value and use it to authenticate all subsequent Agent API calls; teamEvolver stores only its SHA-256 hash, and rotation is `POST /api/tenants/{tenant_id}/rotate-token`.
- Single-tenant deployments (`storage_pg` disabled): the console does not issue credentials; the operator configures the `TEAMEVOLVER_TENANT_TOKEN` environment variable (it must start with `tevt_`) and the server resolves it to the `default` tenant. Rotation means changing the value and restarting the process.

It is a tenant-wide full-privilege credential, with the tenant identity derived server-side from the credential (a client-supplied `X-Tenant-Id` that disagrees is rejected). Every Context Workspace request must declare `integration_id` (Query parameter on `describe`/`skills`, Body field on the other seven POST routes); the server validates that the Agent belongs to the current tenant and is `active`, and uses it as the ownership key for `context_ref`, Context Sessions and audit records (the declared identity owns the resource). This credential cannot call admin routes (`/api/*`) or the Agent registration route (`/internal/agents/register`).

### 4. Configure Replay API Key

When teamEvolver calls Pi's Replay endpoint, it looks up the authentication key via environment variable:

```bash
TEAMEVOLVER_AGENT_PI_REPLAY_API_KEY=<key>
```

Skill Sync API Key is configured via:

```bash
TEAMEVOLVER_AGENT_PI_SKILL_SYNC_API_KEY=<key>
```

## Session Ingestion

After each session ends, Pi Agent reports the complete Session trajectory via an async job queue. The ingestion includes:

- Complete message sequence (user messages, Agent replies, tool calls and results)
- Per-interaction details (prompt, response, tool_calls, token consumption)
- Agent Event logs
- Runtime Context (subject identity, source materials, sandbox snapshot)
- Session metadata (model config, tool list, Skill usage)

Session ingestion uses `POST /api/agent/sessions`, authenticated with the tenant machine credential (`tevt_`). Delivery uses a durable outbox guaranteeing at-least-once delivery with exponential backoff retry.

## Runtime Context Construction

When reporting Sessions, Pi provides the following fields in `runtime_context`:

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `external_subject` | string | Yes | User unique identifier, format `<tenant>:<user-id>`, used for subject mapping |
| `tenant_id` | string | Recommended | Tenant ID for multi-tenant isolation |
| `user_id` | string | Recommended | User ID within tenant |
| `source_materials` | array | Optional | Source materials used in this session (repos, files, etc.) |
| `sandbox_snapshot` | object | Optional | Sandbox snapshot info (image, workspace hash) for True Replay isolation |

Example:

```json
{
  "runtime_context": {
    "external_subject": "tenant-a:user-42",
    "tenant_id": "tenant-a",
    "user_id": "user-42",
    "source_materials": [
      {
        "type": "repository",
        "uri": "https://github.com/example/repo",
        "ref": "main",
        "sha": "abc123"
      }
    ],
    "sandbox_snapshot": {
      "image": "pi/sandbox:v2",
      "workspace_hash": "def456"
    }
  }
}
```

Subject mappings must be pre-configured in the teamEvolver admin console, mapping `external_subject` to teamEvolver user IDs. Mappings can also be batch-synced via the `subject_mappings` field during registration (`subject_mappings_authoritative: true` indicates Pi side provides authoritative mappings).

## Context Workspace Integration

Before each interaction turn, Pi Agent fetches context via the Context Workspace API:

1. **Resolve**: Call `GET /api/agent/context/resolve?external_subject=<id>` to get the current user's context projection
2. **Inject**: Inject team/personal Memory entries and published team Skill Bundles into Agent system prompt or tool context
3. **Commit**: After session ends, submit this turn's Context usage snapshot via `POST /api/agent/context/session` for evolutionary analysis
4. **Remember**: Write valuable user preferences or knowledge to personal Memory via `POST /api/agent/context/remember`

Related code: `teamEvolver/proxy/agent_context.py`

## Pi True Replay Implementation

Pi Agent implements Protocol V1-level True Replay, with core characteristic **external_tool_replay=fail-closed**.

### Sandbox Isolation

Pi's Replay Worker is launched via systemd-run with the following isolation:

| Isolation Dimension | systemd Directive | Description |
|--------------------|-------------------|-------------|
| Network namespace | `PrivateNetwork=yes` | Worker has no independent network stack; can only connect to Model Broker via Unix Socket |
| Filesystem | `ProtectSystem=strict`, `ProtectHome=yes` | System directories read-only; real HOME invisible |
| Writable paths | `ReadWritePaths={sandbox_home}` | Only sandbox temp directory writable |
| Process launch | `posix_spawn + setsid` | Avoids fork/vfork segfaults; ensures independent process group |
| Credential isolation | Protected Unix Socket | Model API Key brokered via ReplayModelSidecar short-lived credentials; Worker never holds real keys |

### External Tool Replay Strategy

1. **Workspace-local tools** (file read/write, bash commands, code search, etc.) execute realistically in the branch sandbox without interception.
2. **Network-reachable tools** (HTTP requests, database access, external APIs):
   - If the tool has recorded return results from the original session, Pi deterministically replays results per call sequence;
   - If an unrecorded external tool call is encountered, Pi **does not** fall back to live calls, directly returns `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` error, marking the case unreplayable (fail-closed).
3. **Deterministic context**: Replay receives `frozen_context` (frozen context projection) distributed by teamEvolver; no new context resolution is performed.
4. **Model credential proxy**: Model requests from Replay Worker are forwarded through ReplayModelSidecar → Unix Socket → ReplayModelBroker; the Broker holds the real API Key and transparently proxies responses in streaming mode.

### Replay Result Format

Pi Replay endpoint returns results containing complete trace info:

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_<hash>",
  "branch": "candidate",
  "status": "succeeded",
  "metrics": {
    "interaction_turns": 3,
    "tool_call_count": 8,
    "total_tokens": 6200,
    "api_calls": 3,
    "input_tokens": 5400,
    "output_tokens": 800
  },
  "output": {
    "final_response": "..."
  },
  "trace": {
    "messages": [],
    "events": [],
    "interactions": []
  },
  "context_input_hash": "<sha256-of-frozen-context>",
  "runtime_checklist_report": {},
  "elapsed_seconds": 52.3
}
```

### Skill Bundle Installation

In the Replay Candidate branch, the Skill Bundle under validation is installed into the Skill directory within the isolated sandbox (`~/.pi/skills/<name>/`), without affecting the published version. The Baseline branch loads the currently published Skill version. The two branches' Skill Bundles are completely independent.

## Skill Sync

When a Skill is published or rolled back, teamEvolver pushes change notifications to Pi's `skill_sync_url` via HTTP webhook. Upon receiving notification, Pi:

1. Pulls the latest Skill Bundle
2. Updates local Skill cache
3. Hot-loads into running Agents (no restart required)

Delivery uses a durable outbox guaranteeing reliable push, with per-tenant filtering, exponential backoff retry, and dead-letter queue.

## Related Code Paths

| File | Description |
|------|-------------|
| `teamEvolver/integrations/agent_protocol.py` | Protocol V1 wire protocol constants and validation |
| `teamEvolver/integrations/agent_registry.py` | Agent registration and identity resolution |
| `team_replay/adapters.py` | Replay HTTP adapters |
| `teamEvolver/integrations/skill_sync_adapters.py` | Skill Sync webhook push |
| `teamEvolver/proxy/agent_context.py` | Context Workspace API implementation |
| `team_replay/engine.py` | True Replay core execution engine |
| `team_replay/policy.py` | Progressive disclosure and Checklist decisions |
| `team_replay/memory.py` | Memory True Replay runner |
| `team_replay/model_broker.py` | Replay Model Broker (Unix Socket credential proxy) |
| `AgentsHub/backend/app/integrations/team_evolver.py` | Pi-side integration reference (registration, Session ingest, Sync receive) |
| `AgentsHub/backend/app/integrations/team_evolver_replay.py` | Pi-side Replay branch execution and isolation |
| `AgentsHub/backend/app/core/pi_agent.py` | Pi Agent Runtime core (subprocess management, RPC, tool loop) |
| `AgentsHub/backend/app/core/pi_rpc_worker.py` | Pi RPC Worker (posix_spawn + setsid launch) |
