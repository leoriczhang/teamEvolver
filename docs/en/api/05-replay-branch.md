# ReplayAdapterFactory and control plane

True Replay executes customer Agents through a tenant-bound Python factory.
The system owns A/B orchestration, Checklist judging and natural user feedback; adapters only execute Agents.
The runtime contract is `open(context)`, `send(user_message)` and `close()`.
Implementation: [hooks](../../../team_replay/hooks.py), [loader](../../../team_replay/adapter_runtime.py),
[engine](../../../team_replay/engine.py).

## Factory and branches

Files live in deployment-wide `replay.adapters_dir`. The default directory contains example adapters.
`replay.adapter` binds the default tenant; other tenants explicitly set flat `replay_adapter`
and never inherit that binding. Administrators select preinstalled adapters in the health page.
Files export `REPLAY_ADAPTER` and `build_replay_adapter(config)`.

```python
from team_replay.hooks import AgentObservation

REPLAY_ADAPTER = {"label": "Customer Agent", "enabled": True}

def build_replay_adapter(config):
    return CustomerFactory(config)

class CustomerFactory:
    def __init__(self, config):
        self.config = config

    def open(self, context):
        # Create an isolated customer conversation/workspace, apply treatment,
        # and return a Session implementing send(message) and close().
        raise NotImplementedError("Connect your customer runtime here")
```


`ReplayContext` contains only request_id, runtime_type, treatment, materials, context_snapshot and timeout_seconds.
The first query and subsequent feedback are passed only through `send()`. Adapters maintain conversation continuity.
Every branch needs its own session/workspace. Model, tools, initial materials, Context and limits stay equal;
only treatment differs. Return unsupported if isolation cannot be guaranteed.

A treatment may contain one Skill or a peer Skill Set with no primary member.
The Test Dataset declares the associated set in `skills[]`, while each Replay
Case selects the subset needed by that task through `skill_ids[]`. Baseline and
Candidate must resolve and record the same Skill set; an experiment may replace
one or more changed Skills.

`AgentObservation` requires response; messages, artifacts and metrics are optional.
Token/API metrics must be real runtime observations. Missing metrics are unavailable, never zero-filled.
Adapters never receive Checklists, judge state or hidden requirements.
An independent judge verifies evidence; a separate user simulator renders natural feedback.
Candidate incomplete/Baseline complete means reject; Candidate alone complete means accept.
Compare objective efficiency only when both complete; both incomplete or judge unavailable means inconclusive.

## Control plane

| Method | Endpoint | Permission |
| --- | --- | --- |
| GET / PUT | `/api/replay-adapter` | Console admin: describe / bind `{"file":"customer.py"}` |
| GET | `/api/replay-adapter/code?file=customer.py` | Root: read source/revision |
| PUT | `/api/replay-adapter/code` | Root: `{file, code, expected_revision}` |
| POST | `/api/replay-adapter/code/test` | Root: `{file, code, context, query}` |


Python runs in the service process: source operations and execution tests require Root.
AST validation is not a sandbox. Tests require an explicit query and context (runtime_type and optional
skill/materials/context_snapshot/timeout_seconds), with no publication data.
Source writes use an optimistic revision check; conflicts return 409.
Built-in wrappers support TurnBased, MappedHttp and DEAP; branch-only legacy protocols are explicit compatibility wrappers.
Validation runtimes come from `validation.runtimes`, not Agent registration.

## DEAP/upclaw Response Contract

`POST /deapAgent/blocking/message` keeps the top-level `answer` for compatibility and should return observations for the current turn:

```json
{
  "answer": "final answer",
  "traceId": "trace-123",
  "replay": {
    "messages": [{"role": "assistant", "content": "..."}],
    "metrics": {"tool_call_count": 2, "total_tokens": 841},
    "artifacts": []
  }
}
```

Successful Replay responses require non-negative `tool_call_count` and `total_tokens` values scoped to the current turn; teamEvolver aggregates across turns. `messages` contains only that turn's complete assistant/tool trajectory. Older upstream responses without `replay` still produce an answer, but the result records `metrics_incomplete=true`, `metrics_incomplete_reason`, and the missing metrics instead of filling zeroes. `traceId` is diagnostic correlation only; teamEvolver does not require a later Langfuse query to decide the evaluation.

This repository implements DEAP response mapping and backward compatibility. The upclaw service must implement these response fields in its own repository.
