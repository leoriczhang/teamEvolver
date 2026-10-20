# Sessions

A Session is the complete record of one task-oriented interaction between an Agent and a user, including the conversation, tool calls, artifacts, context usage and efficiency data. Sessions are the original source of all Evidence.

## Session as a Complete Interaction Record

A v2 Session contains at least `session_id`, `runtime.type`, `runtime_context.user_id` and non-empty `turns`. Each turn can carry:

- User input and Agent reply (`prompt_text`, `response_text` or full `messages`)
- Tool calls and results
- `context_usage`: the Context references actually used in this turn
- Artifacts and efficiency metrics

The formal definition is [agent-session-v2.schema.json](../../schemas/agent-session-v2.schema.json) together with [Agent Protocol v2](../agent-integrations/06-protocol-v2.md).

## Intake and Mandatory Analysis

Two entry points feed the same persisted pipeline:

| Entry | Acceptance | First task |
| --- | --- | --- |
| Agent push `POST /ingest_session` | Validates the v2 envelope, tenant key and `user_id`, and the `context_usage` references | `session.normalize` |
| Data source pull `POST /api/datasource/pull` or scheduled pull | Checks that the Adapter is bound at a fixed revision | `session.pull`, which accepts each item and enqueues `session.normalize` |

In one short transaction the API writes `ingestions`, the raw payload (PostgreSQL ObjectStore) and `worker_tasks`, and returns `202` with `job_id`. The task payload pins `config_revision` and `adapter_revision`, so later configuration or Adapter changes do not affect in-flight tasks.

The Worker runs four mandatory stages in order; each writes a `session_stage_results` row whose result is an immutable payload:

1. `normalize`: validate `turns` and produce the standard Session.
2. `analyze`: one model call for value classification, summary and four-dimension scoring (`team_skills/evolution/stages/analyze.py:analyze_session`).
3. `trace_analyze`: per-trace analysis (`team_skills/evolution/stages/trace_analyze.py:analyze_session_traces`).
4. `archive`: writes the formal `sessions` row only after the first three stages succeed.

A missing model, a failed call or invalid output fails the stage, which is retried by the Worker retry rules; no rule-based substitute is produced and no formal Session is written.

## Value Classification

`value_judge.decision` from `analyze` determines `sessions.decision`:

| Model decision | `sessions.decision` | Destination |
|----------|------|------|
| `valuable`: contains reusable team-level Skill Evidence | `valuable` | Consumable by Skill Evolution |
| `memory_candidate`, `task_only`, `chitchat` and similar | `skipped` | Archived, not used by Skill Evolution |

The full decision goes to `filter_audit` and the four scores to `judge`. Injected Skills (`injected_skills`) only mean the Skill was visible to the Agent and are not Evidence; Skills actually used, tool calls, procedures and task outcomes are Evidence.

## Evidence

Skill Evolution consumes Sessions with `decision=valuable` and records cross-cycle accumulated Evidence per Skill in `skill_evidence`. Each branch needs at least `min_group_sessions` Sessions and `min_group_users` users; see [Evolution Loop](./02-evolution-loop.md).

## context_usage Tracking

Each turn can carry `context_usage`:

```json
{
  "context_usage": {
    "context_snapshot_id": "ctxsnap_...",
    "memory_refs": [{"context_ref": "...", "operation": "retrieved"}],
    "skill_refs": [{"context_ref": "...", "operation": "injected"}],
    "feedback": {"outcome": "success", "correction": ""}
  }
}
```

At push acceptance `teamEvolver/context/usage.py:verify_usage` checks: the snapshot belongs to the current tenant and user; each `context_ref` was issued by a Context endpoint, is unexpired, has the matching kind and belongs to that snapshot; `operation` is one of `retrieved`, `injected`, `read`, `selected`; at most 200 per kind. Failures return 400.

## Session Materials

Sessions can reference files uploaded by users. `team_replay/datasets/materials.py:collect_session_materials` recovers materials from a Session for Test Datasets and True Replay:

1. **Inline materials** (`source_materials`): cross-host Agents carry file contents as base64.
2. **Sandbox snapshot** (`runtime_context.sandbox_snapshot_path`): same-host deployments recover from a tar.gz snapshot.

Materials are deduplicated by SHA-256, with ≤ 20 MiB per file, ≤ 80 MiB total and at most 100 files; only relative paths are accepted, and absolute paths and `..` are rejected.

## metrics Fields

| Field | Description |
|------|------|
| `interaction_turns` | Number of interaction turns |
| `tool_call_count` | Total tool calls |
| `total_tokens` | Total tokens |
| `input_tokens`, `output_tokens` | Input and output tokens |
| `cache_read_tokens`, `cache_write_tokens`, `reasoning_tokens` | Cache and reasoning tokens |

These metrics feed the True Replay efficiency comparison; missing values are recorded as unavailable, never filled with 0.

## Session Storage

| Location | Content |
| --- | --- |
| `ingestions` | Acceptance record: source, external ID, user, Account, `dedupe_key`, task ID, Adapter/configuration revision, payload reference and hash, business time |
| `session_stage_results` | Status, result reference and error code per stage |
| `sessions` | Formal Session: `decision`, `judge`, `filter_audit`, `meta`, `content_ref`, `content_hash`, `business_time`, `ingested_at` |
| `storage_objects` | Raw payload (`ingest/<tenant>/<ingestion>.json`) and per-stage results (`session/<hash>/<stage>/<digest>.json`) |

The console shows both `business_time` (when it happened) and `ingested_at` (when it was stored). All tables have RLS enabled.

## Idempotency

- The `dedupe_key` of an Agent push is `push:<session_id>`; a repeated push within a tenant returns the first `ingestion_id` and `job_id` with `dedupe=true`.
- The `dedupe_key` of a pull is computed from the request window and filters; scheduled pulls deduplicate by date window.
- The formal Session ID is computed from `(tenant_id, user_id, session_id)` (`session_ingestion/identifiers.py:tenant_user_session_id`), so same-named Sessions of different tenants or users do not collide.

## Code Entry Points

| Module | Path |
|------|------|
| HTTP acceptance | [api/__init__.py](../../../session_ingestion/api/__init__.py) |
| Data source control plane | [api/datasource.py](../../../session_ingestion/api/datasource.py) |
| Worker Handlers | [handlers/](../../../session_ingestion/handlers/) |
| Mandatory stages | [stages.py](../../../session_ingestion/stages.py) |
| v2 envelope | [push/protocol.py](../../../session_ingestion/push/protocol.py) |
| Repository and queries | [repository.py](../../../session_ingestion/repository.py), [queries.py](../../../session_ingestion/queries.py) |
| Immutable payloads | [payloads.py](../../../session_ingestion/payloads.py) |

## Related Docs

- [Session Ingestion API](../api/03-session-ingest.md)
- [Session Query API](../api/08-sessions-api.md)
- [Tenant Data Source Onboarding](../guides/10-datasource-onboarding.md)
- [Evolution Loop](./02-evolution-loop.md)
