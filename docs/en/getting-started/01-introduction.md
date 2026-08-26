# Introduction

teamEvolver is a **control plane for Agent team capability evolution**. It is not another Agent Runtime, but a capability evolution layer that sits on top of existing Agent Runtimes (such as Hermes, Pi, Codex, etc.), converting experience generated from real work into reusable, verifiable, and governable team Skills and team Memory.

## Core Positioning

Agent Runtimes are responsible for executing tasks, while teamEvolver is responsible for making teams stronger with use:

- **Experience Feedback Loop**: Extracts reusable Evidence from actual Agent Sessions
- **Candidate Generation**: Generates Skill Candidates and Memory Change proposals based on Evidence
- **Grounded Validation**: Runs Baseline and Candidate in parallel in isolated real Agent Runtimes via True Replay
- **Gated Release**: Checklist completion gate + efficiency comparison (rounds/tool calls/Token) + admin review
- **Continuous Evolution**: DreamCycle maintains existing team Memory, while the cross-user aggregation pipeline uses an editable Skill and `ov compile` to consolidate personal experience into a shared team directory

## Relationship with OpenViking

teamEvolver uses OpenViking to persist shared assets and evolution artifacts; local storage is limited to configuration, login Sessions, and runtime state:

| Data Type | Location in OpenViking |
|-----------|------------------------|
| Team Skill | Versioned Skill Bundles under `viking://resources/team-skill-evolver/skills/` |
| Personal Skill | `viking://resources/team-skill-evolver/peers/<account>/skills/` |
| Team Memory | `viking://resources/shared-knowledge/` by default; the prefix is configurable |
| Personal Memory | `viking://user/<user>/memories/` |
| Sessions and evolution artifacts | `sessions/`, `session_archive/`, `candidate_skills/`, `validation_*`, and related paths under `viking://resources/team-skill-evolver/` |
| Snapshot | Account-scoped OpenViking Snapshot history, addressed by immutable commit OIDs |

The console exposes these assets through two distinct entries:

- **Agent Workspace** contains personal/team Skills, Memory, and Resources that Agents can reference, with direct access to Skill Lab and Memory Lab.
- **Platform Assets** contains read-only internal artifacts such as Sessions, Candidates, Validation records, and Evidence; Agents cannot reference this storage.

## Use Cases

- **Team Coding Agents**: Multiple Hermes machines share team Skills, automatically distilling new skills from completed tasks
- **Multi-Runtime Integration**: Pi, Hermes and other Coding Agent Runtimes unified evolution and capability distribution
- **Enterprise Internal Agents**: Private deployment, accumulating domain Memory and Skills from business conversations
- **Evaluation and Evolution Research**: Using True Replay for rigorous Baseline vs Candidate comparison experiments

## What It Does Not Do

teamEvolver explicitly does not take on the following responsibilities:

- **Does Not Execute Tasks**: Has no own Agent Runtime; all Replays execute in connected Runtimes
- **Does Not Replace Agent Configuration**: The Agent's own model, tools, and system Prompt remain managed by the Runtime
- **Does Not Perform Centralized Inference**: LLM calls in the evolution Pipeline are configurable but only assist analysis; they do not replace Runtime inference
- **Does Not Bypass Governance Boundaries**: The default `validated` mode requires Checklist, result-count, and runtime-compatibility gates; gray-zone results enter human review, while administrators may explicitly choose `direct` or force a release

## Next Steps

- [Quick Start](./02-quickstart): Run a local teamEvolver instance in 5 minutes
- [Installation & Deployment](./03-installation): Complete installation and configuration instructions
