# FAQ

## Basic Concepts

### How does teamEvolver relate to OpenViking?

PostgreSQL is the only authoritative store of teamEvolver's mutable state: Sessions, Skill versions, Candidates, tasks and configuration all live there. OpenViking stores Memory and Resources readable by Agents and provides Compile; Team Memory aggregation, knowledge base mining and successful experience sync depend on it. Without OpenViking, Session ingest, Skill evolution, True Replay and publishing still work.

### Is teamEvolver an Agent Runtime?

No. teamEvolver does not run user-facing Agent tasks and does not take over the Agent's tool loop. On top of existing Agent Runtimes, it receives Sessions, generates and validates Skill Candidates, and maintains Team Memory. Analysis, evolution and validation call the configured model.

### How does teamEvolver relate to Langfuse?

Two independent uses: a data source adapter can pull Agent sessions from Langfuse, see the [Data Source Onboarding Guide](../guides/10-datasource-onboarding.md); trace observability reports teamEvolver's own model calls to Langfuse, see the [Trace Observability Guide](../guides/04-observability.md). Both are optional.

## Deployment and Configuration

### What is the default port?

The `api` role uses 52010 for the console, API and health checks. The `worker` role serves only `/healthz` and `/readyz` on 52011.

### Where is the configuration file?

The bootstrap configuration comes from environment variables and the YAML pointed to by `TEAMEVOLVER_CONFIG_FILE`; `teamEvolver config` shows the merged result read-only. Tenant and module configuration lives in PostgreSQL and is changed through the console or API. See the [Configuration Reference](../guides/01-configuration.md).

### Which models are supported?

Any service compatible with the OpenAI Chat Completions API. The default Base URL is `https://api.openai.com/v1` and the model is `gpt-4o-mini`. The bootstrap configuration uses `TEAMEVOLVER_LLM_*`, the tenant default model is changed in the console "Model Settings", and stage models are overridden in "Evolution Pipeline".

### Do evolution and Memory aggregation run on a schedule?

Not by default. Skill evolution and Team Memory maintenance can each be configured in the console for manual or periodic automatic triggering; manual run actions always remain available. The Worker also runs scheduled data source pulls and successful experience sync.

### Can I connect to a self-hosted OpenViking on another machine?

Yes. In "Runtime Status → OpenViking Deployment" choose remote self-hosted and enter a reachable address in the Endpoint override, for example `http://openviking.example.com:1933`. Saving hot-reloads without a restart.

### What is the difference between DreamCycle and Team Memory aggregation?

They are two stages of a Team Memory run. The aggregation stage reads the personal Memory of selected users and compiles it into Team Memory; the DreamCycle maintenance stage deduplicates, revises and marks outdated content in existing Team Memory. A run can do aggregation only, maintenance only, or both; see [Memory](../concepts/04-memory.md).

## Agent Integration

### What is the minimal integration?

1. Get the service address and tenant Key from the administrator.
2. Decide a stable `user_id` for each end user.
3. After a Session ends, call `POST /ingest_session` per the [Agent Protocol](../agent-integrations/06-protocol-v2.md).

The minimal integration needs neither Replay nor Skill pull.

### What if the tenant Key leaks?

Rotate the tenant Key in "Members & Access" or call `POST /api/tenants/{tenant_id}/rotate-token`. The new Key is shown once, the old Key stops working immediately, and the server stores only a hash. Rotation affects every Agent of the tenant, so update their configuration.

### Can multiple Agent Runtimes connect?

Yes. Different Runtimes use the same tenant Key and declare the real user's `user_id`. Context and Sessions are isolated by `(tenant_id, user_id)`, and all Sessions take part in evolution together.

## True Replay

### Does True Replay call real external tools?

That is up to the Replay adapter. The adapter must give each branch its own workspace and distinguish tools that can be replayed in a sandbox from tools with external side effects. Cases that cannot be replayed safely should be marked not runnable. See [True Replay](../concepts/06-true-replay.md).

### How do Checklists relate to scoring?

A Checklist is a completion gate, not a quality score. A Candidate must satisfy every Checklist item, and only then is efficiency compared by interaction turns, tool calls and token usage. See [Checklist](../concepts/07-checklist.md).

### Must the Baseline branch also pass the Checklist?

Yes. When the Baseline cannot complete the Checklist, the Case or the existing Skill has a problem, and no efficiency comparison is made.

## Skills and Memory

### Automatic publish or human review?

With `auto_evaluate=true` (default), a Candidate is evaluated automatically after generation. `publish_mode=manual` (default) requires an administrator to publish from "Candidate Review". With `automatic`, publishing occurs only when static checks and the runtime gate pass and the verdict is accept. See [Evolution Loop](../concepts/02-evolution-loop.md).

### Does a Skill rollback delete versions?

No. A rollback writes a new version with historical content, and every version is kept. See [Publish and Rollback](../concepts/08-publish-rollback.md).

### What is the difference between personal and Team Memory?

- **Personal Memory**: under `viking://user/<user>/memories/`, written by Agents through `POST /internal/agents/context/remember`, editable by the owner in the console
- **Team Memory**: under `viking://resources/shared-knowledge/` by default, generated and maintained by Team Memory runs, read-only for Agents, editable by tenant administrators

## Troubleshooting

### Runtime status says OpenViking is unavailable?

Check the Endpoint, Account and Root Key in "Runtime Status → OpenViking Deployment", and the network from the service to OpenViking.

### Session ingest succeeded but the console does not show it?

202 only means accepted. Call `GET /api/jobs/{job_id}` with the returned `job_id` to see whether `session.normalize` and `session.analyze` finished; analysis needs a working model and a running Worker.

### Frontend build reports "cannot find module"?

Run `npm --prefix web-ui ci` in the repository root, then `npm --prefix web-ui run build`.

More in [Troubleshooting](../guides/06-troubleshooting.md).
