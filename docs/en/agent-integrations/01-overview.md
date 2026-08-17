# Agent Integration Overview

As the Agent team capability evolution control plane, teamEvolver provides standardized integration protocols enabling various Coding Agents, IDE plugins, and AI platforms to feed back session data, use team-shared Memory and Skills, participate in True Replay validation, and receive real-time team skill updates.

## Integration Tiers

teamEvolver defines two integration tiers, stacking capabilities incrementally from minimum viable to full:

| Capability | Minimum Integration | Full Integration |
|-----------|---------------------|-----------------|
| Agent Registration (`/internal/agents/register`) | Required | Required |
| Subject Mapping | Required | Required |
| Session Ingest (`POST /ingest_session`) | Required | Required |
| Context Workspace (resolve/read/skills/remember/forget) | No | Required |
| Context Session (start/append/commit) | No | Required |
| True Replay Branch Execution (replay.branch.v1) | No | Required |
| Skill Sync Push/Pull | No | Required |
| Memory Write (remember/forget) | No | Required (personal scope) |

**Minimum integration** allows Agents to feed session data back to teamEvolver, participating in evolution loop session value filtering and candidate Skill generation.

**Full integration** further allows Agents to use teamEvolver as unified Context backend, execute True Replay comparative validation in isolated sandboxes, and sync team-published Skill updates in real-time.

## Prerequisites

- teamEvolver service deployed and running, default listening on `http://<host>:52010`
- OpenViking storage backend configured (persistence layer for shared Memory/Skills)
- Control plane key `EVOLVE_INGEST_API_KEY` obtained (environment variable, provided by teamEvolver operations)
- For full integration, Agent needs to expose HTTP endpoints callable by teamEvolver (Replay URL, Skill Sync Webhook URL)

## Capability Matrix

Following are capability identifiers defined by teamEvolver Protocol V1 and their corresponding endpoints:

| Capability ID | Description | Minimum Integration | Full Integration |
|---------------|-------------|---------------------|-----------------|
| `session.ingest.v1` | Session data ingestion | Required | Required |
| `context.workspace.v1` | Context Workspace read/write | No | Required |
| `replay.branch.v1` | True Replay branch execution callback | No | Required |
| `skill.sync.v1` | Skill change push receive | No | Required |
| `memory.personal.read.v1` | Personal Memory read | No (via workspace) | Required |
| `memory.personal.write.v1` | Personal Memory write (remember/forget) | No | Optional |
| `memory.team.read.v1` | Team Memory read | No (via workspace) | Required |
| `skill.personal.read.v1` | Personal Skill read | No (via workspace) | Optional |
| `skill.team.read.v1` | Team Skill read | No (via workspace) | Required |
| `skill.bundle.v1` | Full Skill Bundle pull | No | Recommended |

## Rollout Sequence

Recommended to enable capabilities incrementally in following order, ensuring stability at each step before enabling next layer:

1. **Registration & Subject Mapping** -- Register Agent via `/internal/agents/register`, configure `external_subject` to teamEvolver user mappings in admin UI or registration payload.
2. **Shadow Mode Context** -- Enable Context Workspace but don't inject returned content into model prompt; only verify connectivity and permission configuration.
3. **Enable Session Ingest** -- Switch one integration point to V1 Session ingest; verify session data correctly stored and attributed.
4. **Enable Context Injection** -- Inject Memory/Skill content returned by Context Workspace into model context.
5. **Enable True Replay** -- Configure replay_url to have teamEvolver callback Agent to execute baseline/candidate branch comparisons during candidate Skill validation.
6. **Enable Skill Sync** -- Configure skill_sync_url, receive team Skill publish/rollback events, update local Skill cache in real-time.
7. **Disable Legacy Paths** -- After one compatibility cycle, disable old shared keys and direct storage paths.

Rollback must never lower Replay security level, baseline CAS checks, or central Checklist adjudication.

## Related Documentation

- [Protocol V1 Specification](./02-protocol-v1.md) -- Detailed wire protocol definitions, request/response formats, and error codes
- [Hermes Integration Guide](./03-hermes.md) -- Hermes Coding Agent integration steps
- [Pi Agent Integration Guide](./04-pi-agent.md) -- Pi Coding Agent runtime integration reference
- [Custom Agent Integration](./05-custom-agent.md) -- Step-by-step guide for integrating custom Agents from scratch
- [API Reference](../api/01-overview.md) -- Complete HTTP API reference documentation
- JSON Schema definitions in `docs/schemas/` directory
