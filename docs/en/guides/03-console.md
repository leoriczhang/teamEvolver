# Web Console User Guide

teamEvolver provides a full-featured Web console for monitoring evolution status, managing skills, configuring parameters, reviewing candidates, etc. Console starts by default with `teamEvolver start`, accessible at `http://<host>:<port>` (default `http://127.0.0.1:52010`).

Frontend source at `web-ui/src/`, build artifacts at `teamEvolver/web/dist/`.

## Accessing Console

After starting service, open service address in browser to access. If user authentication enabled, first access requires login. Default admin account created via CLI.

```bash
teamEvolver start
# Open http://127.0.0.1:52010
```

## Agent Workspace

The Agent Workspace combines personal and team Skills, Memory, and Resources that Agents can reference. It opens in read-only **browse mode** by default.

To update multiple Memory or Skill files:

1. Click **Edit** in the top toolbar.
2. Open and modify any number of Memory or Skill files. Drafts remain available while switching files or personal/team Workspaces.
3. Click **Finish editing** to review every line-level Diff in one window.
4. Click **Save changes** to submit the complete change set. The server checks each file against its content hash from the start of the edit session. If another writer changed a file, the submission is rejected and all drafts remain available.

Resources, team assets without write permission, and platform assets always remain read-only.

## Dashboard

Dashboard is console homepage providing global view of system operational status.

![Console Dashboard](/assets/teamEvolver-console-dashboard.png)

### Status Cards

Status cards at page top show core metrics:

- **Service Status**: Shows whether service running normally, plus PID and port info
- **Evolution Cycle**: Shows last evolution time, next scheduled evolution time, current round status
- **Queue Status**: Number of sessions waiting processing
- **Total Skills**: Number of skills in local skill library
- **Candidates Awaiting Review**: Number of candidate skills awaiting human review or under validation
- **DreamCycle Status**: Memory maintenance engine operational status (if enabled)

### Session Queue

Shows list of sessions currently waiting to enter evolution pipeline, including:

- Session ID
- Session title/summary
- Ingestion time
- User alias
- Value classification result (valuable/skipped/duplicate)
- Injected skills list
- Metric scores

Click individual session to view complete conversation content, tool call trajectory, and scoring details.

### Session History

Shows historical sessions and evolution cycle records. Each cycle record contains:

- Timestamp
- Participating session count
- Evolved skill groupings
- Skills uploaded to cloud count
- Queued candidates count
- Scoring results and decision reasons

Can filter by session ID to view evolution triggered by specific sessions.

### Candidates Awaiting Review

Shows list of candidate skills under validation or awaiting human review. Each candidate displays:

- Skill name
- Proposed action (improve/optimize_description/create)
- Current validation status (evaluating/open/rejected/published)
- Replay efficiency comparison (turns, tool calls, Token changes)
- Checklist pass status
- Submission time

Click candidate to enter detailed review page, view skill diff, replay details, Checklist report, and make approve/reject decisions.

### Skill Versions

Shows version history for each skill in skill library, including:

- Skill name
- Current version number
- Last update time
- Version change notes
- Effectiveness metrics (injection count, effectiveness rate)

## Evolution Pipeline View

Evolution pipeline view provides complete 11-stage pipeline visualization and debugging capabilities.

![Evolution Pipeline](/assets/teamEvolver-evolution-pipeline.png)

### Pipeline Stages

Pipeline contains following 11 stage nodes (8 LLM stages + 3 logical/IO/gate nodes):

| Stage | Type | Description |
|-------|------|-------------|
| Ingest (Session enqueue) | IO | Sessions enter queue via `/ingest_session` or Langfuse pull |
| Session Filter (Value classification) | LLM | Determines whether session is skill evidence, user memory, ordinary task, or chitchat |
| Summarize (Session summary) | LLM | Builds lossless trajectory and generates trajectory-aware summary |
| Judge (Session scoring) | LLM | Supplements quality/efficiency/tool-usage dimension scores for sessions |
| Group (Group by skill) | Logic | Buckets sessions by referenced skills; no-skill sessions go to no-skill bucket |
| Evolve (Improve skill) | LLM | Decides improve/optimize/create/skip for existing skills based on evidence |
| Create (Create skill) | LLM | Identifies reusable patterns from no-skill bucket and generates new skills |
| Merge (Conflict merge) | LLM | Merges two evolved versions of same-named skill |
| Dataset Synthesis (Test set generation) | LLM | Generates progressive test datasets with Checklist |
| Validate (True replay validation) | Gate | Discloses Checklist round-by-round based on initial Query, compares efficiency baseline |
| Replay Checklist (Checklist judge) | LLM | Verifies replay results satisfy Checklist item by item |
| Publish (Release) | IO | Candidates passing validation written to skill library and synced to cloud |

Nodes connected by directed edges, visualizing data flow.

### Editable Prompts

Click any LLM stage node to view and edit system Prompt used by that stage. See [Prompt Studio Guide](./08-prompt-studio.md) for details.

### Model Parameter Tuning

Each LLM stage supports independent model parameter configuration:

- **Temperature**: Sampling temperature (0.0-2.0)
- **Max Tokens**: Maximum output tokens
- **Model**: Can specify model used for that stage (empty uses global LLM config)

### Test Panel

Each LLM stage provides test panel to:

1. Select a real historical session as test input
2. Edit Prompt (temporary test, not saved)
3. Run test, view actual System Message, User Message and model output sent by that stage
4. Compare output differences before and after modification
5. Save as Prompt override when satisfied

## Skill Management

Skill management page for managing local skill library and cloud-synced skills.

### Skill List

Shows all skills, supports filtering by name, category, status:

- Skill name and description
- Category
- Version number
- Last modified time
- Injection count and effectiveness statistics
- Local/cloud sync status

### Skill Editor

Click skill to enter online editor:

- Supports Markdown format editing of SKILL.md
- Real-time preview rendering
- Edit Frontmatter metadata (name, description, category, tags)
- Effective immediately after saving
- Provides version history and rollback functionality

### Skill Import/Export

- **Import**: Supports batch importing skills via ZIP file, or drag-and-drop uploading single SKILL.md
- **Export**: Export selected skills as ZIP package
- **Cloud Sync**: Manually trigger pull/push/sync operations
  - Pull: Pull shared skills from OpenViking
  - Push: Push local skills to cloud (filtered by quality gates)
  - Sync: Bidirectional sync (pull then push)

### Skill Versions

Each skill maintains complete version history:

- Auto version numbers (v1, v2, ...)
- Version difference comparison (diff view)
- One-click rollback to any historical version
- Version notes (who modified when for what reason)

Version rollback operations generate new version record; history not deleted.

## Memory Workspace

Memory workspace for managing teamEvolver user memory space (DreamCycle required).

- **Memory List**: View all memory entries in current memory space
- **Memory Search**: Semantic search memory content
- **Memory Editor**: Manually add, modify, or delete memories
- **Memory Audit**: View modification logs to memories by DreamCycle Jobs
- **Blackboard**: DreamCycle collaboration blackboard showing current reasoning state

## DreamCycle Studio

DreamCycle Studio for configuring and monitoring memory maintenance engine.

- **Job Switches**: Enable/disable individual DreamCycle Jobs (team_overview, deduplication, cleanup, onboarding_check, consolidate)
- **Schedule Configuration**: Set active time window, rounds per window, round interval
- **Job Prompt Editor**: Customize system Prompt per Job
- **Job Parameters**: Configure model parameters per Job (temperature, max_tokens, model)
- **Run History**: View execution reports for each DreamCycle window
- **Manual Trigger**: Immediately trigger one DreamCycle run
- **Memory Health**: View memory deduplication rate, coverage, and other metrics

## Langfuse Integration Panel

Langfuse panel for configuring and monitoring Langfuse integration status.

- **Connection Status**: Shows whether can connect to Langfuse service
- **Inbound Pull Configuration**: Configure filters for pulling sessions from Langfuse (environment, user_id, tags, etc.)
- **Outbound Tracing Configuration**: Configure LLM call tracing parameters (sample rate, environment tags, content capture toggle)
- **Credential Management**: Set public_key and secret_key (keys not echoed back, only shows whether configured)
- **Manual Pull**: Manually trigger one Langfuse session pull
- **SDK Status**: Shows whether Langfuse Python SDK installed, tracing initialized

## Model Settings

Model settings page for configuring global LLM parameters:

- **Provider**: Model provider
- **Base URL**: API endpoint address
- **Model ID**: Model name
- **Max Tokens**: Maximum output tokens
- **Temperature**: Default sampling temperature
- **API Key**: API key (masked after input, plaintext not returned)

Changes take effect immediately without service restart.

## User Management

User management page (admin only) for console user account management:

- **User List**: Shows all users, roles, last login time
- **Create User**: Add new users, set username, password, role (admin/user)
- **Role Management**: Modify user roles
- **Reset Password**: Reset user passwords
- **Agent Mapping**: View and manage Agent subject to user mappings (for access control during Agent integration)
- **Disable/Enable**: Disable or enable user accounts

## Audit Log

Audit log records all important configuration changes and operations:

- Configuration item modifications (who, when, changed what, old/new values)
- Skill changes (create, modify, delete, publish, rollback)
- Candidate review decisions (approve/reject, reviewer, reason)
- User management operations (create user, modify role, reset password)
- Langfuse pull and push operations
- Manually triggered evolution/DreamCycle runs

Logs support filtering by time range, operation type, operator.

## Health Status Page

Health status page provides system internal state details:

- Component health status (LLM connection, OpenViking connection, Langfuse connection, session storage, validation Worker)
- Configuration snapshot (excluding keys)
- Queue depth and processing latency
- Recent error logs
- Resource usage

## OpenViking Workspace

If OpenViking shared backend configured, can browse cloud-shared skills and resources via console:

- Browse team space and personal space
- View shared skill details
- Import skills directly from cloud to local
- Manage Viking path prefixes and permissions

## Skill Miner Console

Skill Miner provides independent Web console for document-to-skill mining workflow; see [Skill Miner Guide](./07-skill-miner.md). Unified console also provides Skill Miner entry including:

- Mining task submission and status monitoring
- Benchmark runs and result viewing
- LIFT integration review and publishing
- Coverage report viewing
- Mining Prompt white-box configuration
