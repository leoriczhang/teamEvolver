# Architecture Overview

teamEvolver is a monolithic FastAPI service (default port 52010) that embeds the evolution engine, team-Memory aggregation, DreamCycle, validation Worker, SkillMiner, and React console. Shared assets and evolution artifacts use OpenViking; YAML configuration, console Sessions, and some runtime state remain under `~/.teamEvolver/`.

## System Architecture Diagram

```
┌─────────────────────────────────────────────────────────────┐
│                    External Agent Runtimes                   │
│  ┌──────────┐  ┌──────────┐  ┌──────────┐  ┌──────────────┐ │
│  │ Hermes   │  │ Pi (AH)  │  │ Codex    │  │ Custom Agent │ │
│  └────┬─────┘  └────┬─────┘  └────┬─────┘  └──────┬───────┘ │
│       │     Hook/SDK│     SDK    │     Protocol V1         │
└───────┼─────────────┼────────────┼──────────────┼───────────┘
        │             │            │              │
        ▼             ▼            ▼              ▼
┌─────────────────────────────────────────────────────────────┐
│                  teamEvolver Service (:52010)                │
│                                                              │
│  ┌──────────────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Proxy Server │  │ Agent Proto  │  │ Context Workspace │  │
│  │ (FastAPI)    │◄─┤ V1 Handler   │◄─┤ (token-scoped)    │  │
│  └──────┬───────┘  └──────────────┘  └───────────────────┘  │
│         │                                                    │
│  ┌──────▼───────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Session      │  │ Evolve Kernel│  │ Skill Mutation    │  │
│  │ Ingest/Filter│─►│ (11-stage    │─►│ Service (commit/  │  │
│  │              │  │  pipeline)   │  │  tombstone/outbox)│  │
│  └──────────────┘  └──────┬───────┘  └─────────┬─────────┘  │
│                           │                     │            │
│  ┌──────────────┐  ┌──────▼───────┐  ┌──────────▼────────┐  │
│  │ DreamCycle   │  │ True Replay  │  │ Validation Worker │  │
│  │ Scheduler    │  │ (baseline vs │◄─┤ (async, isolated) │  │
│  │ (Memory job) │  │  candidate)  │  │                   │  │
│  └──────┬───────┘  └──────────────┘  └───────────────────┘  │
│         │                                                    │
│  ┌──────▼───────┐  ┌──────────────┐  ┌───────────────────┐  │
│  │ Langfuse     │  │ Dataset      │  │ Web Console       │  │
│  │ (tracing)    │  │ Synthesizer  │  │ (React, embedded) │  │
│  └──────────────┘  └──────────────┘  └───────────────────┘  │
│         │                                                    │
└─────────┼────────────────────────────────────────────────────┘
          │
          ▼
┌─────────────────────────────────────────────────────────────┐
│                     OpenViking (storage)                     │
│  Sessions  │  Skills  │  Memory  │  Snapshots  │  Resources │
└─────────────────────────────────────────────────────────────┘
```

## Core Components

### Proxy Server ([server.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/proxy/server.py))

FastAPI application assembly entry point, responsible for:
- Registering all HTTP routes (console static files, health checks, Agent protocol interfaces, admin interfaces)
- Initializing Langfuse tracing, DreamCycle, team-Memory aggregation, and the background validation Worker
- Mounting embedded static frontend (from `teamEvolver/web/dist/`)

### Agent Protocol V1 ([agent_protocol.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/agent_protocol.py))

Integration protocol layer for external Agents, defining capability constants:

| Capability Constant | Description |
|---------------------|-------------|
| `session.ingest.v1` | Session trajectory ingestion |
| `context.workspace.v1` | Context read/write (Memory/Skill) |
| `replay.branch.v1` | True Replay branch execution |
| `skill.sync.v1` | Team Skill distribution sync |

### Context Workspace ([agent_context.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/proxy/agent_context.py))

Controlled context interface for Agents, using integration-scoped tokens, mapping to teamEvolver users via `integration_id + external_subject`, returning opaque `context_ref`, never exposing OpenViking URIs or Keys.

### Evolve Kernel ([evolve/](file:///home/zhangpengkun/teamEvolver/teamEvolver/evolve/))

11-stage evolution pipeline from Session to published Skill:

1. **Ingest** — Receive and validate Session payload
2. **Filter** — Filter by source, length, and outliers
3. **Summarize** — Extract turn summaries and key decisions
4. **Judge** — Evidence classification (Skill/Memory/task requirement/runtime issue/insufficient)
5. **Group** — Aggregate similar Evidence into change windows
6. **Evolve** — Generate Skill Candidate or Memory Change proposals
7. **Create** — Create Candidate version (does not affect published versions)
8. **Merge** — Merge context dependencies
9. **Dataset Synthesis** — Generate test datasets from homologous Evidence
10. **Validate** — True Replay validation (Baseline vs Candidate)
11. **Publish** — Release new version after passing gates, sync to Agents

### True Replay ([true_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/true_replay.py))

Executes Baseline and Candidate branches in parallel in isolated real Agent Runtimes:
- Baseline: Control group without Candidate loaded
- Candidate: Experimental group with the Skill to validate loaded
- Uses frozen Context projection (Snapshot Hash)
- Checklist as completion gate, efficiency ranked by turns → tool calls → Token

### Skill Mutation Service ([mutations.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/mutations.py))

All team Skill create/update/delete operations must go through this service:
- Transactional changes: commit records + tombstone
- Persistent sync outbox ensuring reliable distribution
- Monotonically increasing version numbers, complete audit chain preserved

### DreamCycle ([dreamcycle/](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/))

Continuous team Memory evolution:
- `team_overview` — Maintains team profile
- `dedup` — Semantic deduplication
- `cleanup` — Cleans up expired and low-value Memory
- `consolidate` — Merges related Memory entries
- `onboarding_check` — Newcomer discoverability checks
- Default runs during 0-6 AM window

### Team-Memory Aggregation ([aggregation/](file:///home/zhangpengkun/teamEvolver/teamEvolver/aggregation/))

After an administrator selects OpenViking Account users, the aggregation service runs per-user compiles with bounded concurrency, then writes `viking://resources/<shared_knowledge_prefix>/` through bounded tree-reduce. Work data stays in a sibling root, and persisted fingerprints enable incremental skipping.

### Validation Worker ([validation/worker.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/validation/worker.py))

Asynchronous validation Worker consuming Candidate queue in background, executing True Replay and collecting results.

### Langfuse ([observability/langfuse.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/observability/langfuse.py))

- **Inbound**: Pulls Agent Session trajectories from Langfuse
- **Outbound**: Reports all LLM calls in the evolution pipeline to Langfuse for observability
- All Prompts and model parameters are white-box configurable in the console

## Port Conventions

teamEvolver uses a single **52010** port for all capabilities:

| Path | Method | Description |
|------|--------|-------------|
| `/health`, `/healthz` | GET | Health check |
| `/status` | GET | Service status, queue count, skill count |
| `/console` | GET | Web console |
| `/ingest_session` | POST | Agent Session ingestion |
| `/internal/agents/*` | * | Agent Protocol V1 internal interfaces |
| `/trigger` | POST | Manually trigger evolution cycle |
| `/api/aggregation/*` | * | Team-Memory aggregation, runs, and settings |
| `/api/*` | * | Other console management APIs |

## Code Entry Points

| Component | Code Path |
|-----------|-----------|
| Service startup | [launcher.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/launcher.py) |
| CLI entry point | [cli/](file:///home/zhangpengkun/teamEvolver/teamEvolver/cli/) |
| HTTP routes | [proxy/routes.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/proxy/routes.py) |
| Evolution core | [evolve/](file:///home/zhangpengkun/teamEvolver/teamEvolver/evolve/) |
| Skill management | [skills/](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/) |
| Agent integration | [integrations/](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/) |
| DreamCycle | [dreamcycle/](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/) |
| Team-Memory aggregation | [aggregation/](file:///home/zhangpengkun/teamEvolver/teamEvolver/aggregation/) |
| Configuration store | [config_store/](file:///home/zhangpengkun/teamEvolver/teamEvolver/config_store/) |
| Frontend source | [web-ui/src/](file:///home/zhangpengkun/teamEvolver/web-ui/src/) |
