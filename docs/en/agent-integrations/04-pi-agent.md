# Pi Agent / AgentsHub Integration

Pi Agent and AgentsHub integrate through [Protocol v2](./06-protocol-v2.md). The Session `runtime.type` uses the actual runtime label (such as `pi`, `agentshub`); it is used only for analysis and Replay selection and plays no part in authentication.

## Data Plane

| Purpose | Endpoint | Description |
| --- | --- | --- |
| Session reporting | `POST /ingest_session` | Report after the task; `runtime_context.user_id` is the AgentsHub user |
| Context | `/internal/agents/context/*` | `resolve` before model calls, `read` as needed; `remember` writes personal Memory |
| Skill pull | `GET /sync/skills` | Incremental via `ETag`; install bundles into Pi's Skill directory |

Request formats and error codes are in [Session Ingestion](../api/03-session-ingest.md), [Agent Context](../api/04-context-workspace.md), [Skill Pull](../api/06-skill-sync.md).

## Runtime Context

```json
{
  "runtime": {"type": "pi", "version": "1.4.0"},
  "runtime_context": {"user_id": "alice"},
  "turns": [{
    "turn_num": 1,
    "prompt_text": "...",
    "response_text": "...",
    "tool_calls": [],
    "context_usage": {"context_snapshot_id": "ctxsnap_...", "skill_refs": [{"context_ref": "ctx_...", "operation": "injected"}]}
  }]
}
```

References in `context_usage` must come from `resolve` / `skills` responses for the same `user_id` and are valid for 15 minutes.

## True Replay

True Replay for Pi is implemented by a Replay Adapter written by the deployer; see [Replay Adapter](../api/05-replay-branch.md#replay-adapter). Requirements:

- Each branch creates its own Pi subprocess, session and workspace with identical network, model, tools and initial materials; the only difference is the installed Skill bundle (`context.treatment.skill`).
- Each branch's Skill bundle is installed into that branch's own workspace, never a shared directory.
- Tools with external side effects that cannot be replayed deterministically fail directly (raise `ReplayUnsupported`) instead of falling back to real calls.
- `send()` returns the complete assistant / tool messages of the turn and the actually observed `tool_call_count` and `total_tokens`.

The Adapter can use `TurnBasedReplayAdapter` to call a per-turn HTTP endpoint provided by AgentsHub; the endpoint key is supplied to the TeamEvolver Worker via `TEAMEVOLVER_AGENT_<PROFILE>_REPLAY_API_KEY`.
