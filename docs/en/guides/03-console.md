# Web Console User Guide

The teamEvolver console and API share one FastAPI service, available by default at `http://127.0.0.1:52010/`. Frontend source lives in `web-ui/src/`, and the production build lives in `teamEvolver/web/dist/`.

## Login and Bootstrap

When no users exist, the first visit opens the administrator bootstrap screen. The form defaults to username and password `admin`; replace the password before submitting in production. After bootstrap, other members may register regular accounts from the login screen, and administrators can adjust roles and Workspace bindings under **Users & Permissions**.

The console uses an HttpOnly Session Cookie. Regular users can view and edit only their own personal assets. Administrators can switch users and manage team assets.

## Navigation

| Area | Pages | Purpose |
|------|-------|---------|
| Skill Mining | Overview, Knowledge Sources, Mining Jobs | Manage document sources, run the three-stage SkillMiner pipeline, review and submit artifacts |
| Evolution Loop | Operations, Langfuse Integration, Evolution Pipeline | Follow Session → Candidate → Replay → Publish and configure Skill/Memory evolution |
| Asset Center | Experiment Workbench, Personal and Team Assets, Platform Assets | Edit and validate Skills, manage Agent-referable assets, or inspect internal storage read-only |
| Governance | Global Model, Users & Permissions, Runtime Status | Manage models, identities, OpenViking deployment, and system health |
| Documentation | Documentation | Read and search repository Markdown in English or Chinese |

Pages can be opened directly with `?view=<key>`, for example `/?view=workspace` or `/?view=health`.

## Skill Mining

### Overview

Summarizes knowledge sources, mining jobs, artifacts, and runtime state with shortcuts to common actions.

### Knowledge Sources

The source page supports:

- Uploading documents and tracking post-processing
- Creating, renaming, merging, and deleting source directories
- Browsing source files and readiness state
- Carrying a selected directory directly into a new mining job

### Mining Jobs

Each job runs these stages:

1. Sample package construction
2. Semantic discovery
3. Skill and `EVALUATION.md` compilation
4. Optional reflection rounds and Benchmark execution

Jobs support parallel execution, stop, delete, copyable diagnostics, human evidence supplements, and resume. Completed Markdown artifacts can be previewed or edited, then submitted to the Candidate validation pipeline.

## Evolution Loop

### Operations

The Operations page has four tabs:

- **Overview**: service status, Session queue and history, Candidate summary, and Skill versions
- **Candidate Review**: Candidate detail, Bundle Diff, True Replay evidence, and release decision
- **Evolution Audit**: Sessions consumed, Candidates generated, and release outcome for every cycle
- **Filter Audit**: pre-ingest Session value classification and skip reasons

A Candidate must first pass the Checklist completion gate. Efficiency is then compared in order by interaction turns, tool calls, and tokens. Administrators can publish according to the evaluation or force a release when they explicitly accept the risk.

### Langfuse Integration

The Langfuse page manages two independent paths:

- **Inbound Session pull** previews and imports Sessions filtered by environment, user, tags, release, version, or trace name.
- **Outbound tracing** records teamEvolver's internal model and tool calls.

Administrators can also manage per-agent entries in the "Mapper Registry" panel: each entry carries routing constraints (trace-name patterns, tags, sessionId patterns — first match in list order wins) and mapping code (optionally a `map_session` session hook). Per-entry template insertion and dry-run (including route-match feedback) plus a panel-level "Route Preview" (paste a sample Trace to see the whole registry's routing outcome) are supported. A mapper error falls back for that trace without aborting the whole import.

### Evolution Pipeline

The top-level tabs are:

- **Skill Evolution**: pipeline graph, editable Prompts, stage model options, process settings, and real input/output tests
- **Team Memory Evolution**: cross-user memory aggregation

Team-Memory aggregation follows three explicit steps:

1. Enter or confirm the OpenViking Account and choose incremental or full mode.
2. Load Account users and select them with select-all, clear, or invert controls.
3. Confirm the selection, start the background task, and poll group progress.

After a page refresh, the console restores the latest task from the service. Restarting the service process clears this run list. Administrators can edit the **Team Memory Aggregation Skill** and configure the final output prefix on the same page. The default final root is `viking://resources/shared-knowledge/`; Phase 1 raw snapshots and merge intermediates stay in the merge identity's private Resources, and the Skill runs only during merge.

## Experiment Workbench

The Experiment Workbench combines Skill bundle editing and True Replay debugging. The file tree edits `SKILL.md`, scripts, and reference files; the right panel selects the dataset and run parameters; the bottom panel shows Candidate traces and file diffs. Every unsaved text file is included in the Candidate without first overwriting the released Skill.

## Personal and Team Assets

The asset page exposes two OpenViking roots:

| Space | Root URI |
|-------|----------|
| Personal Memory | `viking://user` |
| Team Resources | `viking://resources` |

The tree supports search, Markdown/JSON/code previews, source view, and directory L0/L1 summaries. Self-hosted OpenViking deployments expose a Studio link. An embedded CLI is also available when the `ov` binary is installed. The console does not expose Skill import or export actions.

### Multi-file Editing

1. Click **Edit** to enter edit mode.
2. Modify multiple text files. Drafts remain available while switching files or asset spaces.
3. Click **Finish editing** to review each numbered line-level Diff.
4. Click **Save changes** to submit the batch.

The server compares content hashes captured when editing began. If another writer changed any file, it returns 409 and preserves all drafts. Each file is limited to 2 MB; one batch may contain at most 100 files and 16 MB. Team Resources are writable only by administrators; Platform Assets always remains read-only.

## Platform Assets

Platform Assets is read-only and exposes only directories required by teamEvolver itself, including:

- `sessions/`, `session_archive/`, `session_ledger/`
- `candidate_skills/`, `validation_*`
- `skill_lab/`, `skill_datasets/`, `evolution_datasets/`
- `skill_evidence/`, `memory-changes/`, `memory-replays/`
- `skill_mutation_commits/`, `skill_sync_outbox/`

The page separates Session state from Skill-evolution artifacts by storage source. Session objects come from PostgreSQL (or local/NAS), while Skill artifacts come from NAS. It provides a logical-key tree, content preview, category counts, and backend labels without depending on OpenViking or showing the OV CLI. These paths are not offered directly to Agents as Workspace assets.

## Governance

### Global Model

Administrators configure an OpenAI-compatible Base URL, Model ID, API Key, Max Tokens, and Temperature and can test the connection directly. Saving hot-reloads the global model used by evolution and mining. Stage-specific overrides remain under **Evolution Pipeline**.

### Users & Permissions

This page manages:

- Team display name
- User account, role, display name, email, and password
- Console profiles and Personal Memory/Team Resources settings; Agents use tenant Key + user_id
- Account + Name bindings for Personal Memory and Team Resources

Trusted mode always uses the tenant Root Key. User records store only the OpenViking Name, while Account comes from the current tenant configuration. Regular users can read and update only their own profile; administrators manage all users.

### Runtime Status

Runtime Status aggregates checks for the service, OpenViking, model, user registry, team Skills, and Agent Integrations and reports queue and Candidate counts.

The **OpenViking Deployment** panel supports:

- Volcengine Cloud OpenViking
- Self-hosted OpenViking on the same machine
- Remote self-hosted OpenViking through an endpoint override
- Account and OpenViking Root Key

Saving hot-reloads OpenViking, DreamCycle, and embedded evolution integrations without restarting the main process.

## Built-in Documentation

**Documentation** automatically scans `docs/zh/`, `docs/en/`, and `docs/design/`. The reader supports a directory tree, full-text search, language switching, GFM tables, code blocks, and repository images.

## Related Documentation

- [Configuration Reference](./01-configuration.md)
- [Skill Miner Guide](./07-skill-miner.md)
- [Prompt Studio Guide](./08-prompt-studio.md)
- [Storage Spaces and Directory Layout](../concepts/09-storage-layout.md)
- [Team Memory Aggregation API](../api/11-team-memory-aggregation.md)
