# Memory System

Memory is retrievable long-term facts, context, preferences, and team consensus. Unlike Skills, Memory does not directly prescribe complete task execution workflows—it provides Agents with contextual background rather than operational steps. teamEvolver divides Memory into personal and team asset layers, continuously maintained and evolved by DreamCycle.

## Personal Memory vs Team Memory

Memory in teamEvolver is categorized by ownership:

| Dimension | Personal Memory | Team Memory |
|-----------|----------------|-------------|
| Storage path | `viking://user/peers/{peer}/memories/` | `viking://user/memories/` |
| Write permission | Only corresponding personal Agent can write | Only DreamCycle maintenance engine can write |
| Sharing scope | Belongs to single user, not shared by default | Available to team members and Agents after shareability judgment |
| Content source | User preferences, personal work habits, specific context | Common patterns appearing repeatedly across multiple people, team consensus, long-valid facts |
| Evolution method | Agent directly writes/forgets, no gate | DreamCycle aggregates, deduplicates, cleans, merges; automatic or human processing by risk |

Agents can only write personal Memory through `remember`/`forget` operations in Context Workspace interface; team Memory can only be changed through DreamCycle's Memory Evolution process.

## DreamCycle

DreamCycle is the continuous evolution process for team Memory. It runs a set of maintenance jobs in priority order during preset time windows, performing aggregation, deduplication, cleanup, profile maintenance, and discoverability maintenance on team Memory.

DreamCycle is **disabled by default**. Set `dreamcycle.enabled: true` in configuration to start the scheduler.

### Scheduling Window

DreamCycle uses time-window-based scheduling:

- **Active hours**: `dreamcycle.active_start_hour=0`, `dreamcycle.active_end_hour=6`, default runs 0:00–6:00 AM
- **Round interval**: `dreamcycle.round_interval_minutes=90`, 90 minutes between rounds
- **Rounds per night**: `dreamcycle.rounds_per_window=3`, maximum 3 rounds per night
- **Max turns per job**: `dreamcycle.max_turns_per_job=25`, maximum 25 ReAct reasoning turns per job

Scheduler supports cross-midnight window configuration (e.g., `active_start_hour=22, active_end_hour=6`). Supports daemon mode (`--daemon`) and single-execution mode (`--once`, ignores time window and runs one round immediately).

### Maintenance Jobs

DreamCycle executes the following jobs in priority order:

| Job | Priority | Responsibility |
|-----|----------|----------------|
| `team_overview` | 10 (executes first) | Maintains team profile: member roster, role responsibilities, current project summary, common services/tools/address information |
| `dedup` (deduplication) | 20 | Semantic deduplication: identifies semantically duplicate or highly overlapping team Memory entries and merges/archives them, uses embedding models for similarity detection |
| `cleanup` | 30 | Cleans up expired content: archives process details of completed projects, temporary information older than 30 days, old versions superseded by new versions, debug artifacts and inspection reports |
| `onboarding_check` | 40 | Newcomer discoverability check: simulates newcomers searching "what does the team do", "who's on the team", "what projects", "what tools", "workflow", verifies core entry points exist |
| `consolidate` | 50 (lowest priority, opportunistic execution) | Cross-member consolidation: extracts common patterns appearing independently across **≥2 different peers** from personal Memory, de-identifies and precipitates as team Memory |

> **Note**: The `consolidate` job has read-only access to personal Memory. It is strictly prohibited to copy original personal Memory text to team Memory; must distill to abstracted common conclusions and strip personally identifiable information.

### Semantic Deduplication

DreamCycle uses embedding models for semantic similarity detection, with two thresholds controlling deduplication behavior:

- **`dreamcycle.dedup_merge_threshold=0.86`**: When cosine similarity ≥0.86, treats as same content, triggers merge
- **`dreamcycle.dedup_warn_threshold=0.72`**: When similarity between 0.72–0.86, marks as possible duplicate, LLM judges whether merge needed

When no embedding model configured (`dreamcycle.embed_model` empty), semantic deduplication disabled; deduplication judgment degrades to LLM text-only judgment without lexical overlap.

### Memory Change Ledger

Every Memory write (add/merge/archive/cleanup) by DreamCycle is recorded as an immutable change record via `MemoryChangeLedger`:

1. **Pre-change snapshot**: Takes OpenViking Snapshot commit on affected paths before writing, records `before_oid` and `before_hash`
2. **Change execution**: Performs actual write via Viking tools (remember/forget/merge/sanitize)
3. **Post-change snapshot**: Takes another Snapshot commit after writing, records `after_oid`, `after_hash`, and `diff_hash`
4. **Persistent record**: Writes complete change record (change_id, run_id, job_name, action, target_paths, source_refs, reason, before/after snapshot references, decision result) to `memory-changes/{date}/{change_id}.json`

Change record schema version is `teamevolver.memory-change.v1`; each record contains complete audit chain: actor (default `teamEvolver:dreamcycle`), started_at, completed_at, result (applied/partial/failed/noop), snapshot_status.

### Blackboard

All jobs in the same DreamCycle round share one Blackboard instance for cross-job communication of processed URI facts and intermediate observations, avoiding repeated reading and processing of the same Memory entry. Each job uses independent ReAct engine instance but shares tool registry and Blackboard.

## Memory Replay

DreamCycle supports content-level True Replay validation for applied Memory Changes (`MemoryTrueReplayRunner`). Similar to Skill True Replay, Memory Replay executes two branches in parallel:

- **Baseline branch**: Loads Memory content before change (snapshot corresponding to before_oid)
- **Candidate branch**: Loads Memory content after change (snapshot corresponding to after_oid)

Both branches share frozen Context projection (consistency guaranteed via `shared_context_hash`), differences limited to changed Memory entries themselves. Validation uses identical Checklist gate and efficiency comparison rules (turns → tool calls → Token).

Memory Replay result schema version is `teamevolver.memory-true-replay.v1`, recorded at `memory-replays/{change_id}/{replay_id}.json`.

## DreamCycle and OpenViking Relationship

All DreamCycle Memory reads/writes complete through OpenViking API, not direct local filesystem operations:

- **Authentication identity**: DreamCycle uses configured `agent_id` (parsed via `OPENVIKING_AGENT_ID` or OpenViking API Key) as OpenViking user identity (`X-OpenViking-User`)
- **Maintenance space**: By default maintains own user's `viking://user/memories/`; when `customer_id` configured (`OPENVIKING_CUSTOMER_ID`), maintenance scope narrows to `viking://user/peers/{customer_id}/memories/`
- **Read scope**: Read tools can search and read across all users (including peers); write/archive tools strictly limited to authenticated user's own Memory space
- **Snapshot capability**: Uses OpenViking Snapshot for pre/post-change content version capture, supports diff and rollback to historical snapshots
- **Test backend**: Supports in-process InMemoryObjectStore with `memory://` protocol, only for unit tests and mock mode, not as user-visible storage backend

> **Note**: `teamEvolver/storage/memory.py` provides test-only in-memory object storage, not DreamCycle's Memory storage backend. DreamCycle's persistent storage is always OpenViking.

## Configuration Reference

```yaml
dreamcycle:
  enabled: false               # Whether to enable DreamCycle scheduler
  active_start_hour: 0         # Active window start hour (default 0:00)
  active_end_hour: 6           # Active window end hour (default 6:00)
  rounds_per_window: 3         # Maximum rounds per night
  round_interval_minutes: 90   # Round interval in minutes
  max_turns_per_job: 25        # Max ReAct reasoning turns per job
  dedup_merge_threshold: 0.86  # Semantic merge threshold
  dedup_warn_threshold: 0.72   # Semantic warning threshold
  embed_model: ""              # Embedding model name; empty disables semantic deduplication
```

## Code Entry Points

| Module | Path |
|--------|------|
| DreamCycle scheduler | [dreamcycle/scheduler.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/scheduler.py) |
| DreamCycle config | [dreamcycle/config.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/config.py) |
| Memory Change ledger | [dreamcycle/memory_changes.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_changes.py) |
| Memory Replay runner | [dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py) |
| Blackboard | [dreamcycle/blackboard.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/blackboard.py) |
| ReAct engine | [dreamcycle/react/engine.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/react/engine.py) |
| Job base and concrete jobs | [dreamcycle/jobs/](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/jobs/) |
| Viking toolset | [dreamcycle/tools/viking.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/tools/viking.py) |
| In-memory object store (test) | [storage/memory.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/storage/memory.py) |
| OpenViking storage client | [storage/viking.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/storage/viking.py) |
| Default config values | [config_store/defaults.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/config_store/defaults.py) |

## Related Documentation

- [Architecture Overview](./01-architecture): DreamCycle position in system architecture
- [Evolution Loop](./02-evolution-loop): Memory Evolution stage in evolution loop
- [Skill System](./03-skills): Skill vs Memory boundary comparison
- [True Replay](./06-true-replay): Validation mechanism used by Memory Replay
