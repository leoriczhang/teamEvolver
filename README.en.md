# teamEvolver

<div align="center">

### Capability Evolution Control Plane for Agent Teams

[![Python](https://img.shields.io/badge/Python-3.11-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Control%20Plane-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/Console-React%20%2B%20TypeScript-149ECA.svg?logo=react&logoColor=white)](https://react.dev/)
[![OpenViking](https://img.shields.io/badge/Context-OpenViking-0F766E.svg)](https://github.com/volcengine/OpenViking)
[![License](https://img.shields.io/badge/License-MIT-18181B.svg)](./LICENSE)
[![中文](https://img.shields.io/badge/README-中文-2563EB.svg)](./README.md)

**Turn real Agent Sessions into reusable, verifiable, governed team Skills and team Memory.**

</div>

---

## Positioning

teamEvolver sits outside the Agent Runtime and keeps team capabilities evolving under governance. It receives Sessions reported by Agents or pulled from data sources, derives Evidence through mandatory analysis, generates Skill Candidates or Memory Changes, and closes the loop through static checks, True Replay, Candidate Review, version publishing and pull distribution.

- Agents keep their own models, tools, workspaces and execution loops.
- teamEvolver owns Evidence, evolution, validation, versions, audit and publishing.
- PostgreSQL is the only authority for mutable business state; OpenViking stores knowledge artifacts such as personal and team Memory, personal Skills and Wiki.
- One image runs in two roles, `api` and `worker`, and both scale horizontally.


## Evolution Loop

```mermaid
flowchart LR
    A["Session push / data source pull"] --> B["normalize → analyze → archive"]
    B --> C["Skill Evolution"]
    C --> D["Skill Candidate"]
    D --> E["True Replay<br/>Baseline vs Candidate"]
    E --> F["Candidate Review"]
    F --> G["New version"]
    G --> H["GET /sync/skills"]
    I["Personal Memory"] --> J["Cross-user aggregation + DreamCycle"]
    J --> K["Team Memory"]
    J --> L["Memory Replay"]
```

The Checklist is a completion gate, not a weighted score. Once both sides complete the Checklist, True Replay compares interaction turns, tool calls and token usage in that order.

## Capabilities

| Module | Capability |
| --- | --- |
| Session intake | v2 protocol push, tenant data source Adapter pulls and scheduled pulls, value classification and four-dimension scoring |
| Skill Evolution | Per-Skill group planning, team evidence threshold, Candidates with same-source Test Datasets |
| True Replay | Runs Baseline and Candidate in the real Agent through the tenant-bound Replay Adapter, with Checklist judge and progressive disclosure |
| Team Skill library | PostgreSQL append-only version chain, publish, rollback, tombstone, idempotent mutations, ETag pull |
| Team Memory | Cross-user aggregation, DreamCycle maintenance, Memory Change and Memory Replay |
| Successful experiences | Experience and Pattern compile, projection into OpenViking |
| Knowledge base mining | Uploaded material converted to Markdown, OpenViking Compile builds the Wiki |
| Experiment workbench | Edit Skill Bundles and run True Replay on unsaved Candidates against datasets |
| Multi-tenancy | Tenants, members, platform / tenant administrators, RLS isolation, tenant keys |

## Console

### Run Overview

<a href="./docs/assets/teamEvolver-console-dashboard.png">
  <img src="./docs/assets/teamEvolver-console-dashboard.png" alt="teamEvolver run overview" width="100%">
</a>

### Evolution Pipeline

<a href="./docs/assets/teamEvolver-evolution-pipeline.png">
  <img src="./docs/assets/teamEvolver-evolution-pipeline.png" alt="teamEvolver evolution pipeline" width="100%">
</a>

Console navigation has seven groups: skill mining, workbench, agents and capabilities, knowledge and methods, evolution and experiments, review and publishing, settings and integration. See [Web Console](./docs/en/guides/03-console.md).

## Architecture

```mermaid
flowchart TB
    Agent["Agent Runtime<br/>tenant key + user_id"] --> LB["Load balancer"]
    Browser["Browser console"] --> LB
    LB --> API["teamEvolver api ×N<br/>auth · validation · short txn · 202"]
    API --> PG[("PostgreSQL<br/>RLS · worker_tasks")]
    Worker["teamEvolver worker ×N<br/>Session analysis · Evolution · Replay · Memory"] --> PG
    Worker --> OV[("OpenViking<br/>Memory · Skill · Wiki · Compile")]
    API --> OV
    Worker --> Replay["Replay Adapter<br/>customer Agent"]
```

The API only does authentication, authorization, validation, short accept transactions and queries; long tasks are written to `worker_tasks`, return `202`, and are claimed by Workers. See [Architecture](./docs/en/concepts/01-architecture.md).

## Quickstart

Requirements: Python 3.11, PostgreSQL 14+, OpenViking, an OpenAI-compatible model endpoint.

```bash
bash scripts/install_teamEvolver.sh
source .venv/bin/activate

export TEAMEVOLVER_ENV=local
export TEAMEVOLVER_PG_DSN="postgresql://te_app:...@127.0.0.1:5432/teamevolver"
export TEAMEVOLVER_ROOT_API_KEY="<random>"
export TEAMEVOLVER_OV_ENDPOINT="http://127.0.0.1:1933"
export TEAMEVOLVER_OV_API_KEY="<OpenViking Root Key>"
export TEAMEVOLVER_LLM_API_KEY="<model key>"

teamEvolver schema apply
teamEvolver start --role all
```

Open `http://127.0.0.1:52010/`; the first visit shows the administrator initialization page. Full steps are in [Quickstart](./docs/en/getting-started/02-quickstart.md).

Common commands:

```bash
teamEvolver status
teamEvolver config show
teamEvolver schema status
teamEvolver stop
```

Production runs the `api` and `worker` roles separately; see [Production Deployment Guide](./docs/en/guides/02-deployment.md). `compose.yaml` at the repository root starts both roles from one image:

```bash
TARGET_ARCH=x86_64 sh cicd_scripts/build.sh
docker compose build
docker compose up -d
```

## Agent Integration

Use [Protocol v2](./docs/en/agent-integrations/06-protocol-v2.md): a service address, a tenant key and `user_id` are all an Agent needs; no Agent registration.

| Purpose | Endpoint |
| --- | --- |
| Report Sessions | `POST /ingest_session` |
| Get context | `/internal/agents/context/*` |
| Pull team Skills | `GET /sync/skills` |

The OpenViking Root Key and model keys are used only on the server. True Replay creates an independent session per branch through the tenant-bound Replay Adapter.

## Security and Consistency

- Every business table has RLS isolating by `tenant_id`; the application role must be NOSUPERUSER, NOBYPASSRLS.
- Credentials are stored as Secret Refs; endpoints only report whether they are configured.
- Workers use leases and fences to prevent duplicate execution; external writes with unknown results are reconciled instead of retried blindly.
- Team Skill mutations write version, files and idempotency record in one transaction; Agents always fetch versions by pull.
- Context references are issued by the server, bound to `(tenant_id, user_id)`, and expire after 15 minutes.
- Team Memory and team Skills are read-only for Agents; Agents can write only personal Memory.
- Tools with external side effects that cannot be replayed deterministically fail directly in Replay instead of falling back to real calls.

## Project Layout

```text
team-evolver/
├── teamEvolver/          # platform: composition root, bootstrap config, database, runtime, storage, OpenViking client, identity, CLI
├── session_ingestion/    # Session push, data source pull, mandatory analysis stages
├── team_skills/          # Skill library, Candidates, Evolution, successful experiences, import/export, workbench
├── team_replay/          # True Replay, Test Datasets, Replay Adapter
├── team_memory/          # team Memory aggregation, DreamCycle maintenance, Memory Replay
├── team_miner/           # mining composition entrypoints
│   ├── knowledge_miner/ # knowledge base mining: documents → Wiki
│   └── skill_miner/     # Skill mining: documents → Skill packages
├── web-ui/               # React + TypeScript console sources
├── tests/                # unit, integration, architecture and docs contract tests
└── docs/                 # bilingual Markdown docs
```

## Documentation

The docs index is [docs/README.md](./docs/README.md).

| Section | Content |
| --- | --- |
| Getting started | [Introduction](./docs/en/getting-started/01-introduction.md), [Quickstart](./docs/en/getting-started/02-quickstart.md), [Installation](./docs/en/getting-started/03-installation.md) |
| Concepts | [Architecture](./docs/en/concepts/01-architecture.md), [Evolution Loop](./docs/en/concepts/02-evolution-loop.md), [Skills](./docs/en/concepts/03-skills.md), [Memory](./docs/en/concepts/04-memory.md), [True Replay](./docs/en/concepts/06-true-replay.md), [Storage](./docs/en/concepts/09-storage-layout.md) |
| Guides | [Configuration](./docs/en/guides/01-configuration.md), [Deployment](./docs/en/guides/02-deployment.md), [Web Console](./docs/en/guides/03-console.md), [Observability](./docs/en/guides/04-observability.md), [Troubleshooting](./docs/en/guides/06-troubleshooting.md) |
| Agent integration | [Overview](./docs/en/agent-integrations/01-overview.md), [Protocol v2](./docs/en/agent-integrations/06-protocol-v2.md), [Custom Agent](./docs/en/agent-integrations/05-custom-agent.md) |
| API reference | [API Overview](./docs/en/api/01-overview.md), [Endpoint Index](./docs/en/api/00-endpoints.md) |
| 中文 | [中文文档](./docs/zh/getting-started/01-introduction.md) |

After code changes, update the docs following the [Docs Maintenance Guide](./docs/en/api/99-docs-maintenance.md) and run `node docs/scripts/check-docs-refs.mjs --strict`.

## Development

```bash
bash scripts/install_teamEvolver.sh
npm --prefix web-ui ci
bash scripts/verify_local.sh
```

`verify_local.sh` runs Python compilation, the test suite, frontend tests and the production build.

## License

[MIT](./LICENSE)
