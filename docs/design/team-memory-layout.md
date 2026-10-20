# Team Memory Implementation Layout

`team_memory/` owns the Team Memory run: cross-user aggregation, DreamCycle maintenance, Memory Change records and Memory Replay. It is a sibling of `teamEvolver/` and ships in the same distribution. Every run executes on the Worker as `memory.aggregate` / `memory.compile` / `memory.replay` tasks; PostgreSQL holds run state and OpenViking holds the Memory content.

| Path | Responsibility |
| --- | --- |
| `api/__init__.py` | Memory run, status and Skill routes (`create_router`) |
| `api/skills.py` | Editable aggregation and maintenance Skill routes |
| `api/replay.py` | Memory Change and Memory Replay routes |
| `runs.py` | Short acceptance and fenced run mutations |
| `handlers.py` | Worker handlers registered for the `memory.*` kinds |
| `pipeline.py` | Stage driver: snapshot, deterministic staging, tree-reduce, maintenance |
| `merge.py` | Checkpointed Compile operations with deterministic keys |
| `aggregation/sources.py` | Per-user Memory enumeration and kind selection |
| `aggregation/staging.py` | Deterministic `snapshot.jsonl` staging per user |
| `aggregation/state.py` | Incremental fingerprints and checkpoints |
| `aggregation/okf_skill.py` | Default aggregation Skill text |
| `maintenance/maintenance_skill.py` | Default DreamCycle maintenance Skill text |
| `openviking.py` | OpenViking identity, copy and snapshot helpers |
| `changes.py` | Reads Memory Change before/after snapshots |
| `replay.py` | Builds Memory Replay specs on the public Replay contract |
| `repository.py` | `memory_runs`, `memory_stages`, `memory_checkpoints`, `memory_changes` |
| `config.py`, `schemas.py` | Module configuration and request schemas |
| `tests/` | Module tests |

The run model is described in [Memory](../en/concepts/04-memory.md); the HTTP contract is [Team Memory Aggregation](../en/api/11-team-memory-aggregation.md).

The run does not provide atomic publication, physical deletion, or automatic snapshot rollback in OpenViking.
