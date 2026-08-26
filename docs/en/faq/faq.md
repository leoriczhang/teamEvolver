# Frequently Asked Questions

## Basic Concepts

### What is the relationship between teamEvolver and OpenViking?

teamEvolver is an Agent capability evolution control plane built on top of OpenViking. OpenViking handles persistent storage (Memory, Skill, Session, Snapshot), while teamEvolver manages evolution logic (Evidence extraction, Candidate generation, True Replay validation, gated publishing). Without OpenViking, teamEvolver cannot run.

### Is teamEvolver an Agent Runtime?

No. teamEvolver is not the Agent Runtime for user tasks and does not take over an integrated Agent's tool loop. It does call the configured model for Evidence extraction, evolution, and validation. It is a control layer sitting on top of existing Agent Runtimes (Hermes, Pi, Codex, etc.), responsible for enabling continuous evolution of team capabilities.

### What is the relationship between teamEvolver and Langfuse?

Langfuse is an observability tool. teamEvolver uses it for two purposes: (1) pulling Session trajectories reported by other Agents from Langfuse; (2) reporting LLM calls within the evolution Pipeline to Langfuse for tracing. Langfuse is an optional dependency; the system runs without it configured.

## Deployment and Configuration

### What is the default port?

**52010**. All capabilities (console, API, health checks) use this single port.

### Where is the configuration file?

`~/.teamEvolver/config.yaml`. Use the `teamEvolver config <key> <value>` command to modify settings.

### Which LLM Providers are supported?

Any provider compatible with the OpenAI Chat Completions API. The default configuration is Volcano Engine Doubao (`https://ark.cn-beijing.volces.com/api/v3`), configurable via `llm.api_base` and `llm.api_key`.

### Is DreamCycle enabled by default?

No. DreamCycle is a background Job for continuous team Memory evolution and requires explicit enabling with `dreamcycle.enabled: true`. It runs in the default 0-6 AM window.

### Can I connect a self-hosted OpenViking server on another machine?

Yes. Under **Governance → Runtime Status → OpenViking Deployment**, select **Self-hosted OpenViking** and enter a remotely reachable endpoint such as `http://10.0.0.8:1933`. Saving hot-reloads the integration without restarting teamEvolver.

### How do DreamCycle and team-Memory aggregation differ?

Cross-user aggregation is explicitly started by an administrator after selecting Account users and uses `ov compile` to produce shared output under `viking://resources/<shared_knowledge_prefix>/`. DreamCycle is an optional scheduled maintenance engine for overview, deduplication, cleanup, and discoverability. They are configured independently.

## Agent Integration

### What do I need for minimum integration?

1. Register the Agent by calling `/internal/agents/register` using the control-plane key
2. Save the returned `agent_access_token`
3. Admin completes `external_subject → user` mapping in the console
4. Call `/ingest_session` to report trajectories after Agent sessions end

Minimum integration does not require implementing Replay or Skill Sync.

### What to do about SUBJECT_NOT_MAPPED error?

This indicates the `external_subject` reported by the Agent is not mapped to a teamEvolver user. The administrator needs to add the mapping in the console **Users & Permissions** page, or batch sync mappings via the `subject_mappings` field during registration.

### What if the Agent Access Token is compromised?

Call `POST /internal/agents/register` again for the same Agent with the control-plane key and `"rotate_access_token": true`. Issuing the new token immediately invalidates the old one; the server stores only its SHA-256 hash.

### Can I integrate multiple Agent Runtimes?

Yes. Each Agent Runtime receives an independent `integration_id` and access token during registration, isolated from each other. teamEvolver aggregates Sessions from all registered Agents for evolution.

## True Replay

### Does True Replay call real external tools?

Fail-closed by default. Replay-capable Agents must distinguish external tools (network requests, sending emails, writing to databases, etc.) from in-sandbox tools (file editing, bash commands). External tools that cannot be deterministically replayed cause Cases to be marked unrunnable, rather than falling back to live side effects. See [True Replay](../concepts/06-true-replay).

### What is the relationship between Checklist and scoring?

Checklist is a **completion gate** (pass/fail), not a quality score. Candidates must satisfy all Checklist items to pass, after which efficiency is compared (turns → tool calls → Tokens). Checklist does not contribute scores during efficiency comparison.

### Must the Baseline branch also pass Checklist?

Yes. If even Baseline cannot complete the Checklist, it indicates a problem with the test Case itself or a fundamental Skill defect; in this case, no Candidate comparison is performed.

## Skills and Memory

### Auto-publish or human review?

The default is `publish_mode: validated`. A Candidate may be published by the background process after `validation.required_results`, `validation.required_approvals`, and runtime-compatibility gates pass; gray-zone results enter human review when `human_review_enabled: true`. `publish_mode: direct` skips the Candidate validation queue.

### Does Skill rollback delete versions?

No. Rollback means "restoring historical content as a new version number"; all versions are preserved, forming a complete audit chain.

### What is the difference between personal Memory and team Memory?

- **Personal Memory:** Belongs to a single user, can only be written and read by that user's Agents; Memory written via `POST /internal/agents/context/remember` is personal Memory by default
- **Team Memory:** Stored under `viking://resources/shared-knowledge/` by default, produced by cross-user aggregation and optionally maintained by administrators or DreamCycle; read-only to Agents

## Troubleshooting

### After service startup, /status shows openviking_connected: false?

Check that `sharing.viking_endpoint` and the service key (prefer `sharing.viking_team_api_key`) are correct and the OpenViking service is network-reachable.

### Session ingest succeeded but not visible in queue?

Check whether `runtime_context.external_subject` is correctly mapped and whether the Session was classified by SessionValueClassifier as non-chitchat with value.

### "cannot find module" error during frontend build?

Run `npm install` in the `web-ui/` directory to install frontend dependencies, then re-run `npm run build`.

For more issues, see the [Troubleshooting Guide](../guides/06-troubleshooting).
