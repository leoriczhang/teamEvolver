# Session System

A Session is a complete interaction record generated between an Agent and a user around a task. It is not a chat log, but a structured record containing conversations, tool calls, artifacts, context usage, and efficiency data, serving as the input source for teamEvolver's evolution closed loop.

## Session as Complete Interaction Record

A Session records the entire process of an Agent from receiving a task to completion (or termination), with core elements including:

- **Identity information**: `session_id` (unique identifier), `runtime` (runtime type and integration_id), `runtime_context.external_subject` (user identifier)
- **Turn sequence (turns)**: Each turn contains `turn_num`, `prompt_text`/`instruction` (user input), `response_text`/`response` (Agent reply), `messages` (complete message sequence), `tool_calls`/`tool_results` (tool calls and results)
- **Efficiency metrics (metrics)**: `interaction_turns` (interaction turns), `tool_call_count` (tool call count), `total_tokens` (total Token consumption), with breakdown for input/output/cache/reasoning tokens
- **Context usage (context_usage)**: Records actual context references used in that turn, including `context_snapshot_id`, `memory_refs`, `skill_refs`, `feedback`
- **Source materials**: User-uploaded files, embedded as base64 or referenced via sandbox snapshot paths

Formal Session Schema definition at [agent-session-v1.schema.json](file:///home/zhangpengkun/teamEvolver/docs/schemas/agent-session-v1.schema.json).

## Session Ingest Pipeline

After each session concludes, the Agent reports the Session trajectory via `POST /ingest_session` endpoint. The ingest pipeline performs:

1. **Identity validation**: Validates integration-scoped token, maps `integration_id + external_subject` to teamEvolver user
2. **Schema validation**: Checks required fields (schema_version, protocol_version, session_id, runtime, runtime_context, turns)
3. **Duplicate detection**: Determines duplicate submissions of processed Sessions via content fingerprint. Fingerprint computed from turn count and conversation text hash; same Session with no new turns not re-enqueued
4. **Dual-write storage**: Writes simultaneously to queue (`sessions/`) and archive (`session_archive/`); queue consumed by evolution engine, archive for audit and historical lookup
5. **Index update**: Maintains `session_index.json`, recording metadata for all Sessions (title, user, turn count, Token, tool call count, value judgment result)
6. **Filter audit**: Writes to `session_filter_audit/`, recording filter decisions

Sessions removed from queue after consumption by evolution engine, but archive permanently retained.

## Session Filtering & Value Classification

Before entering evolution queue, Sessions undergo value judgment by `SessionValueClassifier`, determining whether they enter Skill Evolution or Memory Evolution pipeline.

### Decision Categories

| decision | Meaning | Destination |
|----------|---------|-------------|
| `valuable` | Contains reusable team-level Skill Evidence: executed workflows, concrete outputs, explicit Skill gaps, domain processes, or user feedback on outputs | Enters Skill Evolution queue |
| `memory_candidate` | Useful Evidence is user-specific preferences or habits (not team SOP), and may remain useful for that user's future tasks | Routed to Memory Evolution (DreamCycle) |
| `task_only` | Real task request but no completion outputs or actionable evolution Evidence yet | Archived, awaiting more Evidence accumulation |
| `chitchat` | Social, empty conversation, or non-task interaction | Skipped, does not enter evolution |

### Decision Modes

Classifier supports three modes:

1. **Deterministic mode**: Controlled Candidate audit Sessions (containing `candidate_job_id` + `candidate_sha256` and `candidate_skill_gap_report` tool call succeeds) directly judged `valuable` with confidence 1.0
2. **Model mode**: Uses configured LLM to classify Session summary, outputs decision, confidence, reason, memory_candidates
3. **Heuristic mode**: Fallback when LLM unavailable—no user text judged chitchat; tool calls/Skill usage/validation feedback judged valuable; long text (≥80 chars) or multi-turn judged task_only; short exchanges judged chitchat

> **Note**: Injected Skills (`injected_skills`) only indicate Skills visible to Agent, not Evidence; actually used Skills (`used_skills`), tool calls, concrete operational workflows, and task outputs are much stronger Evidence signals. Do not misjudge explicit requirements for single deliverables as user Memory.

### Session Summary

Classifier does not use full Session text, but extracts structured summary:
- User request list (max 20 entries)
- Used tool name list (max 20 entries)
- Interaction turn summaries (each turn user input truncated to 4000 chars, response truncated to 6000 chars, including tool call count and used Skills)
- Verified team Skill feedback (extracts skill_refs and feedback when `context_usage.verified=true`)
- Efficiency metrics (interaction_turns, tool_call_count, total_tokens)

## Evidence Accumulation

Single Sessions typically insufficient to trigger evolution. Evolution engine aggregates similar Evidence into change windows, generating Candidates only when Evidence accumulates to threshold (`evolve.evidence_change_debt_threshold=3`).

Session's `context_usage` is key for Evidence provenance:
- **`used_context_refs`**: List of context_ref actually read by Agent in Context Workspace, reported by Agent at Session commit, server resolves to concrete OpenViking Memory/Skill references
- **`verified` flag**: When Agent confirms using team Skill and provides feedback (outcome, correction), that Skill reference marked verified—most valuable Evidence source
- **`context_snapshot_id`**: Frozen Context projection ID, ensuring same Session's context can be reconstructed

## Session Materials

Sessions may reference user-uploaded files as task inputs. `collect_session_materials` recovers these files from Sessions, supporting two sources:

1. **Embedded materials (source_materials)**: Cross-host Agents carry file content directly in Session payload via base64 encoding
2. **Sandbox snapshot (sandbox_snapshot_path)**: Same-host Pi Agent deployments recover uploaded files via tar.gz snapshot path, without inflating ingest payload

Material collection performs deduplication (SHA-256 deduplication) and size limits: single file ≤20MB, total size ≤80MB, total file count ≤100. Recovered materials used to inject isolated sandbox workspace during True Replay, ensuring Replay Cases can access original input files.

Path security handling: Only relative paths accepted, absolute paths and paths containing `..` rejected to prevent path traversal.

## context_usage Tracking

Each turn in a Session can carry `context_usage` field, tracking which contexts the Agent actually used:

```json
{
  "context_usage": {
    "context_snapshot_id": "ctxsnap_...",
    "memory_refs": [...],
    "skill_refs": [...],
    "feedback": {
      "outcome": "success|partial|failure",
      "correction": "user correction content",
      "error_code": "..."
    }
  }
}
```

Agents report `used_context_refs` via Context Workspace's `sessions/commit` endpoint; server resolves these opaque refs to concrete OpenViking resource references and submits OpenViking `session.used` records. Usage records persisted per payload, ensuring failed retries do not double-count.

## metrics Fields

Session's metrics field records efficiency data, baseline source for True Replay efficiency comparison:

| Field | Type | Description |
|-------|------|-------------|
| `interaction_turns` | int | Number of interaction turns between Agent and user/tools |
| `tool_call_count` | int | Total tool calls |
| `total_tokens` | int | Total Token consumption (input + output) |
| `input_tokens` | int | Input Token count |
| `output_tokens` | int | Output Token count |
| `cache_read_tokens` | int | Cache read Token count |
| `cache_write_tokens` | int | Cache write Token count |
| `reasoning_tokens` | int | Reasoning Token count |

These metrics used in True Replay for Baseline vs Candidate efficiency comparison, priority order: turns → tool call count → total Token.

## Session Storage Structure

In OpenViking backend, Session data organized by prefix:

| Prefix | Purpose |
|--------|---------|
| `sessions/{session_id}.json` | Pending consumption queue (deleted after consumption) |
| `session_archive/{session_id}.json` | Permanent archive |
| `session_filter_audit/{session_id}.json` | Filter decision audit records |
| `session_index.json` | Metadata index for all Sessions (max 10000 entries) |

Archive retains complete Session content and status (queued/consumed/skipped). Index sorted by ingest time descending, supporting fast console browsing and filtering.

## Content Fingerprinting & Idempotency

Session storage uses content fingerprinting for idempotent writes:
- Fingerprint computed from each turn's prompt_text, response_text, runtime type/integration_id, context_usage (snapshot_id, memory_refs, skill_refs, feedback)
- Duplicate submissions for same session_id with unchanged fingerprint (no new turns) judged duplicate, not re-enqueued
- If fingerprint changes (new turns), updates archive and queue, treated as session continuation

This prevents Agents from re-entering same conversation into evolution pipeline due to retries or delayed reporting.

## Code Entry Points

| Module | Path |
|--------|------|
| Session storage & lifecycle | [session_store.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/session_store.py) |
| Session value classifier | [session_filter.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/session_filter.py) |
| Session materials collection | [session_materials.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/session_materials.py) |
| Session Schema definition | [docs/schemas/agent-session-v1.schema.json](file:///home/zhangpengkun/teamEvolver/docs/schemas/agent-session-v1.schema.json) |
| Agent integration protocol (Session Ingest) | [Protocol V1 Specification](../agent-integrations/02-protocol-v1) |

## Related Documentation

- [Architecture Overview](./01-architecture): Session Ingest position in architecture
- [Evolution Loop](./02-evolution-loop): How Sessions drive evolution loop
- [True Replay](./06-true-replay): Sessions as True Replay Case source
- [Memory System](./04-memory): Destination of memory_candidate Sessions
