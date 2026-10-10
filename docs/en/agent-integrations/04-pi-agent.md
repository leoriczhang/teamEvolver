# Pi Agent / AgentsHub integration

Pi Agent uses [Protocol V2](./06-protocol-v2.md), like any other custom
runtime. A tenant machine credential authenticates the request and `user_id`
identifies the business user. Agent registration and capability declarations
are not required.

## Basic configuration

```bash
export TEAMEVOLVER_URL="http://<teamevolver-host>:52010"
export TEAMEVOLVER_TENANT_TOKEN="tevt_<random>"
```

Pi reports its actual runtime label in Sessions:

```json
{
  "session_id": "pi-session-001",
  "user_id": "alice",
  "runtime": {"type": "pi"}
}
```

`runtime.type` is used for analysis and Replay Runtime selection, not
authentication.

## Context and Skills

- Context Workspace uses `/internal/agents/context/*`.
- Every request explicitly supplies `user_id`.
- `integration_id` may be a stable deployment label for source attribution and auditing.
- Skills are pulled; publishing does not depend on a registered webhook.
- The OpenViking Root Key remains only on the teamEvolver server.

## True Replay

The deployment owner configures a Replay Adapter for the Pi Runtime and an
administrator binds it to the tenant. The Adapter creates isolated Baseline
and Candidate sessions and workspaces. Downstream input must not contain
Checklists, evaluation criteria, or candidate-quality labels.

See [Replay API](../api/05-replay-branch.md).

## References

- [Custom Agent integration](./05-custom-agent.md)
- [Session ingest](../api/03-session-ingest.md)
- [Context Workspace](../api/04-context-workspace.md)
- [Skill synchronization](../api/06-skill-sync.md)
