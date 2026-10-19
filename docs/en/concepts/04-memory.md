# Memory

Memory is retrievable long-term facts, background, preferences and team consensus. It gives Agents context and does not prescribe a full task procedure. Both personal and team Memory live in OpenViking; Memory runs, stages, checkpoints, Memory Changes and Replay records live in PostgreSQL.

## Personal and Team Memory

| Aspect | Personal Memory | Team Memory |
|------|-------------|-------------|
| Path | `viking://user/<user>/memories/` | `viking://resources/<shared_knowledge_prefix>/`, default `shared-knowledge` |
| Agent permission | Context `remember` / `forget` write only the caller's own Memory | Read-only |
| Console | Users edit their own Memory; administrators can switch users | Only tenant administrators edit |
| Source | Agent writes, user edits | Cross-user aggregation + DreamCycle maintenance |

Team Memory lives in the Account-shared Resources namespace, so every authorized user retrieves the same output, while write permission stays under teamEvolver control.

## Memory Evolution

A Memory run is accepted by `POST /api/aggregation/run` (`pipeline` is `both`, `aggregation` or `maintenance`) or `POST /api/memory/maintenance/run` (always `maintenance`). The API pins the current `memory` module configuration revision, writes `memory_runs` and a `memory.aggregate` task, and returns `202`. The Worker runs `team_memory/pipeline.py`:

1. **Exclusivity**: only one Memory run per OpenViking Account at a time, guaranteed by the `memory:account:<account>` lease in `resource_leases`; when it is taken the task retries after 5 seconds.
2. **Before snapshot**: copy the target directory to the run work root `before/`.
3. **Aggregation stage** (`aggregation`, `both`):
   - Pin the aggregation Skill (default `viking://agent/skills/team-memory-okf`) into the work root and record its revision.
   - Page through Account users (default 1000 per page, safety cap 50000), excluding the merge identity itself.
   - For each user, read the originals of the selected kinds under `memories/` and write them deterministically to `snapshot.jsonl` in private staging; 20 MiB per user, no model calls.
   - In incremental mode, reuse the previous snapshot when neither the user's original fingerprint nor the Skill revision changed.
   - Tree-reduce Compile in groups of `merge_fan_in` (default 4, range 2–15); when the user count reaches `partition_threshold` (default 512), first split into `partition_count` (default 256) partitions by stable hash.
4. **Maintenance stage DreamCycle** (`maintenance`, `both`): pin the maintenance Skill (default `viking://agent/skills/team-memory-maintenance`), copy current team Memory to a private snapshot as input, then Compile back into the same directory to deduplicate, revise and mark stale entries.
5. **After snapshot**: copy to `after/` and write one Memory Change (before/after references).

Each completed stage writes `memory_stages`; each user's snapshot fingerprint goes to `memory_checkpoints`. After a Worker crash or lease expiry another Worker resumes from completed stages without repeating finished user snapshots or Compiles.

Work roots:

```text
personal source   viking://user/<user>/memories/<kind>/
work root         viking://user/<merge-user>/resources/teamEvolver/staging/<run-id>/
final root        viking://resources/<shared_knowledge_prefix>/
```

The work root is in the merge identity's private Resources, so raw snapshots and intermediate output are not exposed in the Account-shared Resources.

## Aggregation and Maintenance Skills

Two Skills define the aggregation output structure and maintenance rules and are stored in OpenViking. Administrators read and write them under "Evolution pipeline → Team Memory self-evolution":

- `GET /api/aggregation/okf-skill?stage=aggregation|maintenance`
- `PUT /api/aggregation/okf-skill?stage=...`: the Skill `name` must equal the last segment of the configured URI and the body must not exceed 128 KiB; the save is accepted as a `memory.compile` task and returns `202`.

A Skill change only makes the next run redo the affected Compiles; snapshots of unchanged users are not copied again.

## Memory Change and True Replay

`GET /api/memory/changes` lists Memory Changes. Administrators can run a change as a Memory Replay:

- `POST /api/memory/changes/{change_id}/true-replay`: reads the before and after snapshots (2 MiB limit); Baseline is the before content, Candidate the after content.
- `POST /api/openviking/memory/true-replay`: submit `memory_path`, `before_content` and `after_content` directly.

Both run as `memory.replay` tasks and reuse the Checklist gate and efficiency comparison of [True Replay](./06-true-replay.md). Past results are at `GET /api/memory/changes/{change_id}/true-replays`.

## Asset Page Editing

"Personal and team assets" browses and edits `viking://user` and `viking://resources` through `/api/openviking/workspace/*`:

- Browse directories, files and L0/L1 summaries
- Administrators or asset owners enter edit mode, keep drafts across files, review one Diff and save in batch
- Batch writes use content hash preconditions to avoid overwriting concurrent changes

## Configuration

The `memory` module configuration is stored in `tenant_configs`; `GET/POST /api/aggregation/settings` reads and writes its `aggregation` object and requires `expected_revision`:

```json
{
  "expected_revision": 3,
  "shared_knowledge_prefix": "shared-knowledge",
  "okf_skill_uri": "viking://agent/skills/team-memory-okf",
  "maintenance_skill_uri": "viking://agent/skills/team-memory-maintenance",
  "kinds": [],
  "account_user_limit": 50000,
  "account_user_page_size": 1000,
  "merge_fan_in": 4,
  "merge_concurrency": 4,
  "partition_threshold": 512,
  "partition_count": 256,
  "preserve_manual_edits": false,
  "incremental_publish": false,
  "publish_batch_users": 8
}
```

With `preserve_manual_edits=true` current team Memory joins aggregation as an extra input; with `incremental_publish=true` only changed users are written to the target directory in batches of `publish_batch_users`. Maintenance is manual by default. With `maintenance.trigger_mode=automatic`, the Worker accepts maintenance jobs every `maintenance.interval_seconds`; administrators can still run maintenance manually at any time.

## Code Entry Points

| Module | Path |
|------|------|
| HTTP | [api/](../../../team_memory/api/) |
| Run acceptance | [runs.py](../../../team_memory/runs.py) |
| Pipeline | [pipeline.py](../../../team_memory/pipeline.py) |
| Compile grouping | [merge.py](../../../team_memory/merge.py) |
| Worker Handler | [handlers.py](../../../team_memory/handlers.py) |
| Repository | [repository.py](../../../team_memory/repository.py) |
| OpenViking adapter | [openviking.py](../../../team_memory/openviking.py) |
| Default maintenance Skill | [maintenance_skill.py](../../../team_memory/maintenance/maintenance_skill.py) |
| Memory Replay | [replay.py](../../../team_memory/replay.py) |

## Related Docs

- [Storage and Directory Layout](./09-storage-layout.md)
- [True Replay](./06-true-replay.md)
- [Team Memory Aggregation API](../api/11-team-memory-aggregation.md)
