# Introduction

teamEvolver is the **capability evolution control plane for Agent teams**. It does not execute tasks; it sits on top of existing Agent Runtimes and turns experience from real work into reusable, verifiable, governed team Skills and team Memory.

## Positioning

The Agent Runtime executes tasks; teamEvolver makes the team stronger with use:

- **Experience intake**: Sessions pushed by Agents or pulled from data sources are analyzed and become formal Sessions and Evidence.
- **Candidate generation**: Skill Evolution generates Skill Candidates from Evidence; Memory Evolution generates Memory Changes.
- **Real validation**: True Replay runs Baseline and Candidate in isolated real Agent sessions, uses the Checklist as the completion gate, then compares objective efficiency.
- **Gated publishing**: Candidate Review combines static checks with the True Replay verdict to decide Publish; Rollback restores historical content as a new version.
- **Continuous evolution**: team Memory is deduplicated and revised through cross-user aggregation and DreamCycle maintenance.

## Runtime Shape

The same release and image run in two roles:

| Role | Responsibility |
| --- | --- |
| `api` | Authentication, authorization, validation, short accept transactions and queries; long tasks are written to `worker_tasks` and return `202 Accepted` |
| `worker` | Claims `worker_tasks` and runs long tasks such as Session analysis, Skill Evolution, True Replay, Memory Compile and experience sync |

Both roles scale horizontally without session affinity. Architecture details are in [Architecture](../concepts/01-architecture.md).

## Data Ownership

| Data | Authority |
| --- | --- |
| Global configuration, tenants, members, Secret Refs, Adapters, Sessions, Skill versions, Candidates, Datasets, Replay, Memory runs, Worker tasks, audit | PostgreSQL |
| External knowledge artifacts such as Skills, Memory and Wiki, plus Compile Tasks | OpenViking |
| Large objects such as immutable payloads, reports and Team Miner watermarks | PostgreSQL `storage_objects` (ObjectStore) |
| Temporary files, logs, PIDs and rebuildable caches | Local directories |

Local directories hold no unique copy of anything; replicas coordinate only through PostgreSQL. See [Storage and Directory Layout](../concepts/09-storage-layout.md).

## Use Cases

- **Team coding Agents**: members share team Skills and distill new Skills from completed tasks.
- **Enterprise internal Agents**: private deployment that accumulates domain Skills and Memory from business Sessions.
- **Multiple Runtimes**: different Agent Runtimes report Sessions, read Context and pull Skills through the same v2 protocol.
- **Evaluation and evolution research**: True Replay runs controlled Baseline vs Candidate experiments.

## Non-Goals

- **No task execution**: there is no built-in Agent Runtime; True Replay runs in the integrator's Runtime through the tenant-bound ReplayAdapterFactory.
- **No replacement of Agent configuration**: models, tools and system Prompts stay managed by the Runtime.
- **No bypass of governance**: a Candidate is published automatically only when static checks pass and True Replay returns accept; administrators can explicitly choose `force`.

## Next Steps

- [Quickstart](./02-quickstart.md): start an all-in-one development environment locally.
- [Installation](./03-installation.md): development install, release build and offline install.
