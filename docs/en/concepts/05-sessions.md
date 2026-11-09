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

In one short transaction the API writes `ingestions`, the raw payload (PostgreSQL ObjectStore) and `worker_tasks`, and returns `202` with `job_id`. The task payload pins ingestion `config_revision` and `adapter_revision`. Session Analyze currently resolves its model and stage settings from the `skills` module at execution time; the ingestion revision is not a configuration snapshot for every historical model request.

The Worker saves five stages in order in `session_stage_results`, with immutable result payloads:

1. `normalize`: validate `turns` and produce the standard Session.
2. `split`: persist a semantic split plan and derive child tasks when needed; retain the full Session if the splitter is unavailable or fails.
3. `analyze`: one model call for value classification, summary and four-dimension scoring (`team_skills/evolution/stages/analyze.py:analyze_session`).
4. `trace_analyze`: per-trace analysis (`team_skills/evolution/stages/trace_analyze.py:analyze_session_traces`).
5. `archive`: write the formal `sessions` row only after the preceding stages succeed.

A missing model, a failed call or invalid output fails the stage, which is retried by the Worker retry rules; no rule-based substitute is produced and no formal Session is written.

Default Session Analyze requests `response_format={"type":"json_object"}` in the same single model call and returns one JSON object: `classification` and `judge` are objects, while `summary` is a string. Prompt Studio displays this same default output contract. The entire reply must be valid JSON; brackets are never repaired, incomplete JSON is never salvaged, and missing scores are never supplied. Analysis outputs are written only after classification, summary and the existing four-dimension score validators all pass. Existing tagged Analyze Prompt overrides retain their `<classification>`, `<summary>` and `<judge>` contract. When saving edits to the new default, retaining the explicit heading `## 输出格式（严格返回单个 JSON 对象）` declares JSON-object mode. Only trusted Prompt configuration selects the protocol; neither Session input nor model output can select it, and invalid JSON never falls back to tag parsing. Classification criteria, score weights, model and sampling parameters retain their existing configuration.

## Continuous Sessions and Source Revisions

One upstream Session can grow across several pull windows. For verified sources, `source_session_key` is determined by tenant, stable `source_instance_id`, `upstream_namespace` and upstream Session ID, independent of local pull batches or semantic segments. The instance comes from an explicit Adapter declaration or provider plus normalized endpoint. The namespace comes from a declared project or upstream `projectId` / `project_id`, never from credentials. Missing or conflicting namespaces produce `identity_verified=false`; legacy Adapters retain their existing intake behavior without a cross-batch source-deduplication guarantee.

Each stable Trace ID has a factual `revision` and preceding-context `context_revision`. Original Langfuse identity is retained in `source_trace_id`; a Mapper's display `trace_id` does not replace it. Factual revisions include original interaction, tools, observed requests and Skill usage, excluding fetch/analysis times, summaries and scores. Identical text with different Trace IDs represents distinct events and is not merged by text.

- Identical facts from the same source reuse the accepted task and old analysis, even after analysis configuration changes, without automatic reanalysis. The original task may still be processing or retrying.
- Growing from six turns to seven retains all seven. Only the new turn is in `target_trace_refs`; the original six become `context_trace_refs`. Session Analyze can read the full trajectory; Trace Analyze processes targets, with full previous turns supplied in `analysis_context`.
- A late Trace inserted into earlier context or a corrected turn updates affected context revisions. Missing turns in partial responses are retained, never inferred as deleted. Known older `source_updated_at` values do not overwrite newer facts. Uncertain ordering is recorded in `revision_conflicts`, rather than silently treated as confirmed facts.
- Segments share one source root. `provenance.full_snapshot_ref` references the complete immutable source snapshot. Context-only turns cannot increase Evidence or independent-source counts; segments do not create independent upstream sources.

Analysis inputs preserve original requests in `request_context`. Both `generation_input` and `trace_input` have completeness `unknown`; conversation alone is `conversation_only`. Agent-reported requests are `agent_reported` and cannot grant themselves server verification. None of these levels automatically proves the complete final model input. See the [Ingestion and Data Source API](../api/03-session-ingest.md).

A successful Session Analyze writes a checkpoint for that ingestion. If subsequent Trace Analyze fails, a retry reuses it only when real dependencies, including effective Prompts, stage settings and model configuration, match. Trace-result caches also bind target revisions, full analysis context, summary and dependency version. Changed dependencies, failures or missing verifiable dependency wiring can cause more model calls; the system does not promise exactly one model invocation. Existing formal Sessions, historical Evidence and audit records are neither deleted nor rewritten.

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
| `session_source_heads` | Latest accepted factual snapshot per source; does not imply completed analysis |
| `session_source_snapshots` | Idempotent mapping from source root and factual snapshot revision to ingestion |
| `session_analysis_cache` | Trace-analysis result references bound to dependencies and target context |
| `sessions` | Formal Session: `decision`, `judge`, `filter_audit`, `meta`, `content_ref`, `content_hash`, `business_time`, `ingested_at` |
| `storage_objects` | Raw payload (`ingest/<tenant>/<ingestion>.json`) and per-stage results (`session/<hash>/<stage>/<digest>.json`) |

The console shows both `business_time` (when it happened) and `ingested_at` (when it was stored). All tables have RLS enabled.

## Idempotency

- The `dedupe_key` of an Agent push is `push:<session_id>`; a repeated push within a tenant returns the first `ingestion_id` and `job_id` with `dedupe=true`.
- Each manual pull creates a new scan. With `Idempotency-Key`, the same key within a tenant and Adapter reuses the request; different bodies return 409. Scheduled pulls still deduplicate by date window.
- Verified-source item acceptance deduplicates by `source_session_key` and factual snapshot revision; transaction locks make concurrent acceptance converge on one ingestion.
- Formal Session IDs are `session_<ingestion_id>`, with the upstream external ID preserved. Source identity and local archive identity are separate; old Sessions are not backfilled with new source identities.

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
| Source revisions and acceptance | [source_revisions.py](../../../session_ingestion/source_revisions.py), [source_acceptance.py](../../../session_ingestion/source_acceptance.py), [source_repository.py](../../../session_ingestion/source_repository.py) |

## Related Docs

- [Session Ingestion API](../api/03-session-ingest.md)
- [Session Query API](../api/08-sessions-api.md)
- [Tenant Data Source Onboarding](../guides/10-datasource-onboarding.md)
- [Evolution Loop](./02-evolution-loop.md)
