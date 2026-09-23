# Agent integration overview

Prepare the service URL, tenant Key and `user_id`, then follow [v2](./06-protocol-v2.md).
The operator issues or configures the tenant credential. Agents need no registration.
The tenant configuration determines the Account.

1. Submit real Sessions with the [Session API](../api/03-session-ingest.md).
2. Retrieve personal/team Context with the [Context API](../api/04-context-workspace.md).
3. Refresh team Skills before model calls through [Skill pull](../api/06-skill-sync.md).
4. For True Replay, an administrator binds a preinstalled [adapter](../api/05-replay-branch.md).

See [Hermes setup](./03-hermes.md), [custom integration](./05-custom-agent.md), and
[staged upgrades](../guides/11-agent-deregistration.md).
