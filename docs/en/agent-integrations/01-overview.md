# Agent Integration Overview

Agent runtimes integrate with TeamEvolver through three data-plane endpoints, all using a tenant key + `user_id`; no Agent registration or user mapping is needed. Protocol rules are in [Protocol v2](./06-protocol-v2.md).

## Preparation

| Item | Source |
| --- | --- |
| Service address | Provided by the deployer, for example `https://teamevolver.example.com` |
| Tenant key (`tevt_...`) | Issued when a platform administrator creates the tenant, or rotated by a tenant administrator; the `default` tenant uses `TEAMEVOLVER_TENANT_TOKEN` |
| `user_id` | ID, within the tenant, of the user the Agent serves |

Keep the tenant key only on the Agent server or in a protected configuration file. The OpenViking Root Key and model keys are used only on the TeamEvolver server.

## Three Data-Plane Endpoints

| Purpose | Endpoint | When |
| --- | --- | --- |
| Report Sessions | [`POST /ingest_session`](../api/03-session-ingest.md) | After each task |
| Get context | [Agent Context](../api/04-context-workspace.md) | Retrieve Memory / Skill / Resource before model calls; write personal Memory during the task |
| Pull team Skills | [`GET /sync/skills`](../api/06-skill-sync.md) | Before model calls, incremental via `ETag` |

The minimal integration only reports Sessions. Context and Skill pull let the Agent use the knowledge the team has accumulated.

## True Replay

When validating Skill Candidates, TeamEvolver drives the customer Agent through the tenant-bound Replay Adapter to run Baseline and Candidate branches. The Replay Adapter is a Python file written by the deployer, uploaded with the service Root Key and bound by a tenant administrator; the Agent side must provide an independent session and workspace per branch. See [Replay Adapter](../api/05-replay-branch.md#replay-adapter).

## Runtime Notes

- [Pi Agent / AgentsHub](./04-pi-agent.md)
- [Custom Agent](./05-custom-agent.md)
