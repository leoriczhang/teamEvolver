# teamEvolver

<div align="center">

### The Capability Evolution Control Plane for Agent Teams

[![Python](https://img.shields.io/badge/Python-3.10%2B-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Control%20Plane-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/Console-React%20%2B%20TypeScript-149ECA.svg?logo=react&logoColor=white)](https://react.dev/)
[![OpenViking](https://img.shields.io/badge/Context-OpenViking-0F766E.svg)](https://github.com/volcengine/OpenViking)
[![License](https://img.shields.io/badge/License-MIT-18181B.svg)](./LICENSE)
[![中文](https://img.shields.io/badge/README-中文-2563EB.svg)](./README.md)

**Turn real Agent Sessions into reusable, validated, and governed team Skills and team Memory.**

</div>

---

## Product

teamEvolver runs outside the Agent runtime and owns continuous capability evolution and governance. It receives real Sessions and domain material, extracts traceable Evidence, proposes Skill Candidates or Memory Changes, and closes the loop through static checks, True Replay, human gates, versioned publication, and controlled distribution.

It is neither another Agent runtime nor a file synchronization script:

- Agents keep their own models, tools, workspaces, and execution loops.
- teamEvolver owns Evidence, evolution, validation, versions, audit, and release.
- OpenViking stores team Skills, Memory, Sessions, and replay snapshots.
- Langfuse can independently provide Session ingestion and evolution observability.

## Evolution Loop

```mermaid
flowchart LR
    A["Real Sessions / Domain Material"] --> B["Evidence Classification"]
    B --> C["Skill Candidate"]
    B --> D["Memory Change"]
    C --> E["Test Dataset"]
    E --> F["True Replay<br/>Baseline vs Candidate"]
    F --> G["Candidate Review"]
    G --> H["Versioned Release"]
    H --> I["Agent Skill Sync"]
    D --> J["DreamCycle"]
    J --> K["Memory True Replay"]
    K --> L["Team Memory"]
```

A Checklist is a completion gate, not a weighted score. Once the gate passes, True Replay compares efficiency by interaction turns, tool calls, and total tokens, in that order.

## Core Capabilities

| Module | Current capability |
| --- | --- |
| Sessions and Evidence | V1 Session ingest, Langfuse pull, value classification, recent/historical Evidence windows, filter audit |
| Skill Evolution | Summarize, judge, group, improve/create/merge, and same-source Test Dataset synthesis |
| True Replay | Run Baseline and Candidate in the real Agent Runtime; inspect Checklist completion, traces, artifacts, and efficiency |
| Candidate Governance | Review, replay-gated or forced publish, version detail, full Bundle diff, rollback, and audit |
| Memory Evolution | DreamCycle team overview, deduplication, cleanup, onboarding discoverability, consolidation, and Memory Replay |
| Skill Workspace | Create, edit, import, version, and test `SKILL.md` plus multi-file Bundles |
| Context Workspace | Resolve and read personal/team Memory and Skills with levelled content, receipts, and usage attribution |
| SkillMiner | Compile domain documents into Skills, semantic reports, `EVALUATION.md`, and internal Benchmarks |
| Agent Protocol V1 | Registration, identity mapping, Context, Session ingest, Replay Branch, and Skill Sync |
| Observability | Langfuse Session import plus model, tool, Skill Evolution, and DreamCycle tracing |

Every team Skill mutation goes through `SkillMutationService`, which owns commit records, tombstones, a durable outbox, and per-Agent delivery state.

## Console

### Operations Overview

The complete Session queue and history, pending candidates, replay decisions, and Skill versions.

<a href="./docs/assets/teamEvolver-console-dashboard.png">
  <img src="./docs/assets/teamEvolver-console-dashboard.png" alt="Complete teamEvolver operations overview" width="100%">
</a>

### White-box Evolution Pipeline

The complete Skill Evolution pipeline, eight editable prompts, model and process settings, and real input/output testing.

<a href="./docs/assets/teamEvolver-evolution-pipeline.png">
  <img src="./docs/assets/teamEvolver-evolution-pipeline.png" alt="Complete teamEvolver evolution pipeline" width="100%">
</a>

## Architecture

```mermaid
flowchart TB
    subgraph Sources["Evidence Sources"]
        Pi["Pi Agent"]
        Hermes["Hermes"]
        Generic["Agent Protocol V1"]
        LFIn["Langfuse Sessions"]
        Docs["Domain Documents"]
    end

    subgraph Control["teamEvolver Control Plane :52010"]
        Console["React Console"]
        API["FastAPI"]
        Evolution["Skill Evolution"]
        Replay["Validation Worker / True Replay"]
        Memory["DreamCycle"]
        Mutation["SkillMutationService / Outbox"]
    end

    subgraph Context["OpenViking"]
        Sessions["Sessions / Evidence"]
        Skills["Team Skills / Versions"]
        Memories["Personal + Team Memory"]
        Snapshots["Replay Snapshots"]
    end

    Sources --> API
    Console --> API
    API --> Evolution
    API --> Replay
    API --> Memory
    Evolution --> Mutation
    Mutation <--> Skills
    API <--> Sessions
    API <--> Memories
    Replay <--> Snapshots
    Mutation --> Pi
    Mutation --> Hermes
    Mutation --> Generic
```

The console, evolution engine, validation queue, DreamCycle, SkillMiner, and Agent integrations share one FastAPI service and one configuration source.

## Quick Start

Requires Python 3.10+. The full install provisions Hermes inside the project virtual environment for document mining and True Replay.

```bash
git clone https://github.com/leoriczhang/teamEvolver.git
cd teamEvolver

python3 -m venv .venv
source .venv/bin/activate
python -m pip install -U pip
python -m pip install -e ".[all]"

teamEvolver config service.port 52010
teamEvolver start --daemon
teamEvolver status
```

Open `http://127.0.0.1:52010/`. Create the initial administrator, then configure:

1. **Global Model**: OpenAI-compatible Base URL, model, and API key.
2. **OpenViking**: local or cloud deployment with personal and team spaces.
3. **Agent Integration**: register runtimes, map external subjects, and enable Session, Context, Replay, and Skill Sync.
4. **Langfuse**: optionally enable Session pull or tracing; they are independent.

Common commands:

```bash
teamEvolver status
teamEvolver doctor
teamEvolver config show
teamEvolver stop
```

The repository includes the production console build. Run the frontend build only when changing `web-ui/`.

## Agent Integration

Use [Agent Integration Protocol V1](./docs/en/agent-integrations/02-protocol-v1.md):

| Capability | Agent responsibility |
| --- | --- |
| `session.ingest.v1` | Submit complete Sessions, tool traces, tokens, Skill usage, and Context references |
| `context.workspace.v1` | Resolve and read personal/team Context through short-lived `context_ref` values |
| `replay.branch.v1` | Synchronously execute one isolated Baseline or Candidate branch |
| `skill.sync.v1` | Receive released Bundles and verify version plus SHA-256 before installation |

A V1 token identifies an Integration, never a user. Each request supplies a stable `external_subject`, mapped by an administrator:

```text
integration_id + external_subject -> teamEvolver user
```

OpenViking credentials, model credentials, and team-Memory write privileges remain on the teamEvolver server.

## Safety and Consistency

- Replay materializes only the required runtime configuration, never a complete production database.
- Candidate processes do not receive upstream model credentials; model access uses an ephemeral broker.
- External side effects fail closed unless they can be replayed deterministically.
- Context references are server-issued and bound to Integration, Subject, Session, and expiry.
- Team Memory and team Skills are read-only to regular Agents; personal-Memory writes require Subject mapping.
- Skill publish, rollback, delete, and sync use one mutation path and a durable outbox.
- Langfuse tracing is fail-open and never blocks evolution or Memory maintenance.

## Repository Layout

```text
teamEvolver/
├── teamEvolver/
│   ├── evolve/          # Evidence, Skill Evolution, Dataset, release
│   ├── validation/      # Candidate queue and True Replay worker
│   ├── dreamcycle/      # Team-Memory evolution and Memory Replay
│   ├── integrations/    # Agent V1, Hermes, Langfuse, replay adapters
│   ├── proxy/           # FastAPI, console, and Workspace interfaces
│   ├── skillminer/      # Document-to-Skill mining
│   ├── skills/          # Bundles, versions, and SkillMutationService
│   └── storage/         # OpenViking storage adapters
├── web-ui/              # React + TypeScript console source
├── tests/               # Unit, integration, protocol, and replay tests
└── docs/                # Markdown documentation sources (bilingual zh/en, browsable in-console)
```

## Documentation

Documentation is maintained as Markdown source files in `docs/`, available in both English and Chinese. After logging into the console, the built-in reader under "Docs → Documentation" in the left sidebar supports sidebar tree navigation, full-text search, language switching, and Markdown/GFM/code block/table rendering:

| Section | Content |
| --- | --- |
| Getting Started | [Introduction](./docs/en/getting-started/01-introduction.md), [Quick Start](./docs/en/getting-started/02-quickstart.md), [Installation](./docs/en/getting-started/03-installation.md) |
| Concepts | [Architecture](./docs/en/concepts/01-architecture.md), [Evolution Loop](./docs/en/concepts/02-evolution-loop.md), [Skills](./docs/en/concepts/03-skills.md), [Memory & DreamCycle](./docs/en/concepts/04-memory.md), [True Replay](./docs/en/concepts/06-true-replay.md) |
| Guides | [Configuration](./docs/en/guides/01-configuration.md), [Deployment](./docs/en/guides/02-deployment.md), [Web Console](./docs/en/guides/03-console.md), [Observability](./docs/en/guides/04-observability.md), [Troubleshooting](./docs/en/guides/06-troubleshooting.md) |
| Agent Integrations | [Overview](./docs/en/agent-integrations/01-overview.md), [Protocol V1 Spec](./docs/en/agent-integrations/02-protocol-v1.md), [Hermes Integration](./docs/en/agent-integrations/03-hermes.md), [Custom Agent](./docs/en/agent-integrations/05-custom-agent.md) |
| API Reference | [Overview](./docs/en/api/01-overview.md), [Agent Register](./docs/en/api/02-agent-register.md), [Session Ingest](./docs/en/api/03-session-ingest.md), [Context Workspace](./docs/en/api/04-context-workspace.md), [Skills Admin](./docs/en/api/09-skills-admin.md) |
| Design Notes | [Master PRD](./docs/design/01-master-prd.md), [DreamCycle Evaluation](./docs/design/02-dreamcycle-snapshot-evaluation.md), [OpenViking Research](./docs/design/03-openviking-capabilities.md) |

### Documentation sync convention

When modifying code, update the corresponding docs following the [Docs Maintenance Guide](./docs/en/api/99-docs-maintenance.md). Run `node docs/scripts/check-docs-refs.mjs` before committing to verify all code references and links are valid.

- [Protocol JSON Schemas](./docs/schemas/)
- [中文文档](./docs/zh/getting-started/01-introduction.md)

## Development Verification

```bash
python -m pip install -e ".[all,dev]"
npm --prefix web-ui ci
bash scripts/verify_local.sh
```

`verify_local.sh` runs Python compilation, the test suite, and the production frontend build.

## License

[MIT](./LICENSE)
