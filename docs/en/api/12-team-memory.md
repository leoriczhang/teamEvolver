# Two-Stage Team Memory

## 1. Implementation

This is the current contract as of 2026-09-15, superseding legacy ReAct execution.
Implementation lives under `team_memory/`. Old Python namespaces
remain as import compatibility; historical Memory Change and Replay remain readable.

```text
Personal Memory -> deterministic private staging
  -> aggregation Skill + compile -> authoritative Team Memory
  -> cp -r -> private run snapshot
  -> maintenance Skill + compile -> same authoritative Team Memory
```

The default target is `viking://resources/shared-knowledge`. Large aggregation
runs still use bounded tree reduction; two logical stages can need many compile calls.
The HTTP client uses `POST /api/v1/compile`, `GET /api/v1/tasks/{task_id}`, and
`POST /api/v1/fs/cp`. No host CLI is needed. Unsupported `skill_revision` is no
longer sent; full Skill packages are copied into private, run-specific Skill roots
and fingerprinted before execution, so later shared-Skill edits cannot change a run.

## 2. Interface

Existing `/api/aggregation/*` endpoints remain available. External execution
requires a Root/Admin credential; console admins may inherit the configured
credential, but cannot forward it to a different Endpoint.

| Endpoint | Change |
| --- | --- |
| `POST /api/aggregation/run` | `pipeline`: `both` (default), `aggregate`, or `maintain` |
| `GET /api/aggregation/status/{task_id}` | Adds stage, pipeline, snapshot URI, maintenance Skill URI/fingerprint, and outstanding upstream tasks |
| `GET /api/aggregation/runs` | Persistent run history; interrupted runs become failed on restart |
| `GET/PUT /api/aggregation/okf-skill` | `stage=aggregation` or `maintenance`, optional `account_id` |
| `GET/POST /api/aggregation/settings` | Adds `maintenance_skill_uri`, distinct from `okf_skill_uri` |
| `POST /trigger-dreamcycle` | Admin-only legacy entry point, now snapshot + maintenance compile |

```yaml
aggregation:
  shared_knowledge_prefix: shared-knowledge
  okf_skill_uri: viking://agent/skills/team-memory-okf
  maintenance_skill_uri: viking://agent/skills/team-memory-maintenance
```

Incremental mode reuses unchanged aggregation groups and skips successful maintenance
when both its output inventory and Skill fingerprint match. Full mode forces execution.
Maintenance-only needs no selected users and does not collect personal Memory.
Legacy Job settings are retained for compatibility, not used by the compile executor.
Models are configured in the OpenViking compile runtime.

## 3. Example

```bash
curl -X POST http://localhost:52010/api/aggregation/run \
  -H 'Content-Type: application/json' \
  -d '{"root_key":"<root-key>","account_id":"default","pipeline":"both","mode":"incremental","user_ids":["alice","bob"]}'
```

Use `pipeline=maintain` and omit `user_ids` to maintain only. An omitted user list
means all users for aggregation; an explicitly empty list is rejected.

## 4. Recovery and Limits

- A process-local target lock spans both stages. Overlapping targets return 409;
  at most four worker threads are admitted. Multi-replica deployments need external coordination.
- Known upstream tasks retain the target lock on timeout or status failure. A new
  request checks their terminal state before releasing the old reservation.
- Staging/aggregation failure prevents maintenance. Copy failure prevents maintenance.
  Maintenance failure retains aggregation output and its snapshot; there is no automatic rollback.
- Neither copy nor multi-file compile writes are atomic. Readers may see updates in progress.
  Copying a snapshot back merges directories and cannot undo newly added files precisely.
- Compile upsert does not delete omitted files. The default Skill rewrites obsolete
  pages as short pointers and marks them archived. Search does not automatically filter that marker.
- Full Resource checkout submission still has upstream limits, including 128 Wiki
  pages and 256 operations by default. Batching sources does not bypass full-target limits.
- Private run snapshots and Skill copies are retained, not automatically garbage-collected.
- New run records do not populate the legacy per-change ledger or automatically run True Replay.

Implementation: `team_memory/service.py` and `team_memory/maintenance/workflow.py`.
