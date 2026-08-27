# Memory System

Memory consists of retrievable long-term facts, context, preferences, and team consensus. It gives an Agent context rather than prescribing a complete workflow as a Skill does. teamEvolver separates personal and team Memory and manages them through distinct aggregation, maintenance, experiment, and audit paths.

## Personal and Team Memory

| Dimension | Personal Memory | Team Memory |
|-----------|-----------------|-------------|
| Default path | `viking://user/<user>/memories/` | `viking://resources/shared-knowledge/` |
| Configuration | Per-user `personal_space.viking_user` | `aggregation.shared_knowledge_prefix` |
| Agent permission | Write its own Memory through Context Workspace `remember` / `forget` | Read-only |
| Console permission | The owner can edit personal Memory; administrators can switch users | Administrator-only writes |
| Main source | Preferences, work habits, personal facts, and Session extraction | Common knowledge aggregated from repeated patterns across users |
| Evolution | Agent writes, Workspace edits, Memory Lab experiments | Cross-user aggregation, administrator batch edits, optional DreamCycle maintenance, and Memory Replay |

Team Memory uses an account-shared Resources namespace rather than one user's private `memories/` tree. Authorized users retrieve the same team output while teamEvolver retains write control.

## Cross-user Team-Memory Aggregation

The console entry **Evolution Pipeline → Team Memory Evolution** uses `MemoryAggregationService` and `ov compile`:

1. The console defaults to the configured Endpoint, Account, and Trusted Root Key; the independent interface also supports API-key mode through `admin_key`.
2. The service enumerates users with the Root/Admin Key. API-key mode also reads existing plaintext User Keys and excludes the team service user.
3. The administrator selects users and chooses incremental or full mode.
4. Phase 1 uses each User identity to read visible Memory text and deterministically copy it into the merge identity's private staging space; it invokes no model or Skill.
5. Phase 2 uses the pinned Skill revision to tree-reduce groups of at most 15 sources. Large accounts use fixed hash partitions only as private intermediate products, then run a final cross-partition semantic reduction into the configured team-Memory root.

Default paths:

```text
Personal source  viking://user/<user>/memories/<kind>/
Work root        viking://user/<merge-user>/resources/teamEvolver/staging/<target-hash>/
Final root       viking://resources/shared-knowledge/
```

`shared_knowledge_prefix` and `staging_dir` are configurable. The work root belongs to the merge identity's private Resources, so raw snapshots and `_merge` intermediates are not exposed through account-shared Resources. The final root contains only aggregated team Memory.

### Aggregation Skill

The **Team Memory Aggregation Skill** defines the output structure. Administrators edit the complete `SKILL.md` in the console; it is published to the account-shared `viking://agent/skills/team-memory-okf` location with a version snapshot. The Skill executes only during Phase 2 merge.

Changing the Skill invalidates affected merge output without recopying unchanged users. Incremental mode reuses an unchanged user's existing deterministic snapshot.

### Scale and Recovery

- Phase 1 snapshot-copy concurrency defaults to 6.
- User inventory is fetched in stable 1,000-user pages, with a default safety limit of 50,000.
- `merge_fan_in` defaults to 4 and is constrained to 2–15 to stay below the 16-source compile limit.
- Accounts above 512 staged users use up to 256 stable private partitions. Adding or changing a user invalidates only that partition and the affected path to the final semantic root.
- A 10,000-user full run currently plans roughly 3,600 merge compiles. It is resumable, but runtime and model cost remain deployment-dependent.
- The final shared tree still follows the user-edited Skill's semantic directories. It is a distilled team-memory view; immutable raw snapshots remain private.
- One user failure does not stop the others; the next incremental run retries failed or changed users.
- Group checkpoints are appended immediately. Restarting the process clears the live run list but not completed snapshots or merge checkpoints.

See [Team Memory Aggregation API](../api/11-team-memory-aggregation.md) for the complete contract.

## DreamCycle Maintenance

DreamCycle is an optional Memory maintenance engine for team overview, deduplication, cleanup, discoverability checks, and consolidation against its configured target. It is disabled by default and starts only when `dreamcycle.enabled: true`.

### Schedule Window

- `active_start_hour=0`, `active_end_hour=6`: default 00:00–06:00 window
- `rounds_per_window=3`: at most three rounds per window
- `round_interval_minutes=90`: 90 minutes between rounds
- `max_turns_per_job=25`: maximum ReAct turns per Job

### Maintenance Jobs

| Job | Responsibility |
|-----|----------------|
| `team_overview` | Maintain the team profile and entry points |
| `deduplication` | Identify and merge semantically duplicate content |
| `cleanup` | Archive expired, low-value, or superseded content |
| `onboarding_check` | Check discoverability of team, project, tool, and workflow information |
| `consolidate` | Distill de-identified common knowledge from multiple sources |

DreamCycle retains its own ReAct Jobs, policy tools, and scheduler. It is not an alias for the cross-user `ov compile` aggregation API; the two paths can be enabled independently.

## Memory Changes and True Replay

DreamCycle writes are recorded by `MemoryChangeLedger` with before/after Snapshot OIDs, content hashes, diff hashes, source references, policy reasons, and outcomes. Records use `teamevolver.memory-change.v1` under the platform root's `memory-changes/` path.

`MemoryTrueReplayRunner` can use pre-change content as Baseline and post-change content as Candidate in an A/B Replay with frozen Context. Checklist remains the completion gate, followed by efficiency comparison in order of interaction turns, tool calls, and tokens. Results live under `memory-replays/<change_id>/`.

## Workspace and Memory Lab

**Asset Center → Agent Workspace** shows both personal and team Memory and supports:

- Directory, file, and L0/L1 browsing
- Edit mode for administrators or asset owners
- Drafts across multiple files with one Diff review and batch save
- Content-hash preconditions that prevent overwriting concurrent edits

Memory Lab edits an in-memory draft and compares Context injection or True Replay before and after the change. Experiments never modify released Memory automatically.

## Configuration Example

```yaml
aggregation:
  enabled: true
  shared_knowledge_prefix: shared-knowledge
  staging_dir: staging
  account_user_limit: 50000
  account_user_page_size: 1000
  phase1_concurrency: 6
  merge_fan_in: 4
  merge_concurrency: 4
  partition_threshold: 512
  partition_count: 256

dreamcycle:
  enabled: false
  active_start_hour: 0
  active_end_hour: 6
  rounds_per_window: 3
  round_interval_minutes: 90
  max_turns_per_job: 25
  dedup_merge_threshold: 0.86
  dedup_warn_threshold: 0.72
```

## Code Entry Points

| Module | Path |
|--------|------|
| Cross-user aggregation service | `teamEvolver/aggregation/service.py` |
| Aggregation routes and settings | `teamEvolver/proxy/aggregation_routes.py` |
| Aggregation Skill | `teamEvolver/aggregation/okf_skill.py` |
| Workspace scopes and batch writes | `teamEvolver/proxy/openviking_workspace.py` |
| DreamCycle scheduler | `teamEvolver/dreamcycle/scheduler.py` |
| Memory Change ledger | `teamEvolver/dreamcycle/memory_changes.py` |
| Memory Replay | `teamEvolver/dreamcycle/memory_replay.py` |
| OpenViking storage client | `teamEvolver/storage/viking.py` |

## Related Documentation

- [Storage Spaces and Directory Layout](./09-storage-layout.md)
- [True Replay](./06-true-replay.md)
- [Configuration Reference](../guides/01-configuration.md)
- [Web Console](../guides/03-console.md)
- [Team Memory Aggregation API](../api/11-team-memory-aggregation.md)
