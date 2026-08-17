# Introduction

teamEvolver is a **control plane for Agent team capability evolution**. It is not another Agent Runtime, but a capability evolution layer that sits on top of existing Agent Runtimes (such as Hermes, Pi, Codex, etc.), converting experience generated from real work into reusable, verifiable, and governable team Skills and team Memory.

## Core Positioning

Agent Runtimes are responsible for executing tasks, while teamEvolver is responsible for making teams stronger with use:

- **Experience Feedback Loop**: Extracts reusable Evidence from actual Agent Sessions
- **Candidate Generation**: Generates Skill Candidates and Memory Change proposals based on Evidence
- **Grounded Validation**: Runs Baseline and Candidate in parallel in isolated real Agent Runtimes via True Replay
- **Gated Release**: Checklist completion gate + efficiency comparison (rounds/tool calls/Token) + admin review
- **Continuous Evolution**: DreamCycle periodically performs team Memory aggregation, deduplication, cleanup, and profile maintenance

## Relationship with OpenViking

teamEvolver uses OpenViking as its sole persistent storage backend and does not use any local ObjectStore:

| Data Type | Location in OpenViking |
|-----------|------------------------|
| Team Skill | Versioned Skill Bundles under `viking://skills/` |
| Team Memory | Long-term memory entries under `viking://memory/team/` |
| Personal Memory | Personal memory entries under `viking://memory/personal/<user>/` |
| Session Archive | Complete trajectories and Evidence under `viking://sessions/` |
| Snapshot | Frozen Context projections under `viking://snapshots/` |

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
- **Does Not Perform Unautomatic Releases**: Skill releases must pass Checklist gates and human review

## Next Steps

- [Quick Start](./02-quickstart): Run a local teamEvolver instance in 5 minutes
- [Installation & Deployment](./03-installation): Complete installation and configuration instructions
