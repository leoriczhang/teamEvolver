# Team Memory Aggregation API

## 1. API Overview

The Team Memory Aggregation API (Interface 1) aggregates the personal memories of multiple Users under one OpenViking Account into account-shared team memory via `ov compile`. Output defaults to `viking://resources/shared-knowledge/`, the actual target directory is configurable, and every user in the account can retrieve it.

Aggregation uses a two-phase, tree-reduce model. It requires no OpenViking source changes and no auth-mode switch:

- **Phase 1 (per-user staging):** for each selected User, compile runs *as that user* (root key on the wire + `X-OpenViking-User: <uid>` header) and reads only that user's own memory (legal self-read, no ROOT cross-user read). Output goes to the user's staging root `viking://resources/shared-knowledge/_staging/<uid>`. The OKF Skill is installed into that user's own skills space first so the same identity can read it.
- **Phase 2 (tree-reduce merge):** as the team user, all staging roots are merged in bounded batches of `merge_fan_in` (default 12, ≤15) across cascading levels, down to the final `viking://resources/shared-knowledge/` root. Tree-reduce keeps every compile under the 16-source hard limit, supporting 100+ users.

Additional behavior: concurrent Phase 1 (`phase1_concurrency`), content-fingerprint incremental skipping (unchanged users reuse prior staging), and failure isolation with resumable reruns (a single user's failure does not abort the run; the next run retries only failed/changed users).

Implementation:
- Routes: `teamEvolver/proxy/aggregation_routes.py` (`AggregationMixin`)
- Orchestration: `teamEvolver/aggregation/service.py` (`MemoryAggregationService`)
- Compile invocation: `teamEvolver/aggregation/compile_client.py` (`CompileClient`)
- User enumeration: `teamEvolver/aggregation/sources.py` (`AccountSourceBuilder`)
- Incremental state: `teamEvolver/aggregation/state.py` (`AggregationState`)
- Default OKF Skill: `teamEvolver/aggregation/okf_skill.py` (`DEFAULT_OKF_SKILL_BODY`)

## 2. Endpoints and Parameters

All `/api/aggregation/*` endpoints require console **administrator** authentication (admin role). The pipeline itself uses a trusted service identity to run compile, normally reusing the admin-configured OpenViking key.

---

### GET /api/aggregation/users

List the aggregatable Users under an Account (the team service user is excluded). Powers the console flow "enter Account → list users → select".

**Auth:** Console Cookie (admin)

**Query parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `account_id` | string | No | OpenViking Account ID; defaults to configured `sharing_viking_account` when empty |

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `account_id` | string | The account actually used |
| `users` | array[string] | Aggregatable user_id list |

Code entry: `teamEvolver/proxy/aggregation_routes.py:51` (`api_aggregation_users`)

---

### GET /api/aggregation/runs

List recent aggregation tasks (newest first, up to 20). Used to **recover progress after a page refresh** for in-flight or recent tasks.

**Auth:** Console Cookie (admin)

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `runs` | array | Task list (Run objects, see below) |

> Note: task state lives in server memory; restarting the teamEvolver process clears the list (any in-flight compile continues on the OpenViking side, but local progress is no longer tracked).

Code entry: `teamEvolver/proxy/aggregation_routes.py:69` (`api_aggregation_runs`)

---

### POST /api/aggregation/run

Start a background aggregation task; returns 202 with the initial Run object immediately. The task runs in a worker thread; poll progress via the `status` endpoint.

**Auth:** Console Cookie (admin)

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `account_id` | string | No | Target account; defaults to configured `sharing_viking_account` |
| `user_ids` | array[string] | No | Allowlist of users to aggregate; omit to aggregate all eligible users |
| `kinds` | array[string] | No | Memory categories to aggregate; omit to use the default set |
| `mode` | string | No | `incremental` (default, recompile only changed/failed users) or `full` (force recompile all) |

**Response (202):** initial Run object (`status` is `pending`/`running`).

Code entry: `teamEvolver/proxy/aggregation_routes.py:75` (`api_aggregation_run`)

---

### GET /api/aggregation/status/{task_id}

Query a task's live progress (group-level status).

**Auth:** Console Cookie (admin)

**Path parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | Yes | Task ID (returned by `run`) |

**Run object fields:**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | Task ID |
| `account_id` | string | Target account |
| `status` | string | `pending`, `running`, `completed`, `failed` |
| `started_at` | number | Start timestamp |
| `finished_at` | number\|null | Finish timestamp (null while running) |
| `error` | string | Failure reason (when failed) |
| `groups` | array | Group results (Group objects, see below) |

**Group object fields:**

| Field | Type | Description |
|-------|------|-------------|
| `group_key` | string | `stage:<uid>` (per-user staging), `merge:L<n>:g<i>` (intermediate merge), `merge` (final merge), `skill:<uid>` (skill install failure) |
| `kind` | string | Category marker (currently unified as `(all)`) |
| `target_uri` | string | Output URI of the group |
| `source_count` | integer | Number of sources for the group |
| `status` | string | `ok`, `skipped`, `failed` |
| `detail` | string | Note (e.g. `unchanged (reused staging)`, error message) |

Code entry: `teamEvolver/proxy/aggregation_routes.py:107` (`api_aggregation_status`)

---

### GET /api/aggregation/okf-skill

Read the currently effective Team Memory Aggregation Skill (the user-edited content, or the default template).

**Auth:** Console Cookie (admin)

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `skill_name` | string | Skill name (default `team-memory-okf`) |
| `body` | string | SKILL.md content |
| `editable` | boolean | Always true |

Code entry: `teamEvolver/proxy/aggregation_routes.py:116` (`api_aggregation_okf_skill`)

---

### PUT /api/aggregation/okf-skill

Save the user-edited Team Memory Aggregation Skill. The content is persisted to `<state_dir>/okf_skill.md` (default `~/.teamEvolver/aggregation/okf_skill.md`) and installed into each participating identity's skills space on **the next aggregation run**.

**Auth:** Console Cookie (admin)

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `body` | string | Yes | New SKILL.md content, must not be empty |

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `ok` | boolean | Whether the save succeeded |
| `body` | string | Saved content |

Code entry: `teamEvolver/proxy/aggregation_routes.py:129` (`api_aggregation_okf_skill_save`)

## 3. Usage Examples

### List aggregatable users

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/users?account_id=default"
```

Response:

```json
{
  "account_id": "default",
  "users": ["alice", "bob", "chenghan", "zhangpengkun"]
}
```

### Trigger aggregation (selected users + incremental mode)

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"account_id": "default", "user_ids": ["chenghan", "zhangpengkun"], "mode": "incremental"}'
```

Response (202):

```json
{
  "task_id": "agg_1a03c010043",
  "account_id": "default",
  "status": "running",
  "started_at": 1756100000.0,
  "finished_at": null,
  "error": "",
  "groups": []
}
```

### Poll task progress

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/status/agg_1a03c010043"
```

Response (completed):

```json
{
  "task_id": "agg_1a03c010043",
  "account_id": "default",
  "status": "completed",
  "groups": [
    {"group_key": "stage:chenghan", "kind": "(all)", "target_uri": "viking://resources/shared-knowledge/_staging/chenghan", "source_count": 1, "status": "ok", "detail": ""},
    {"group_key": "stage:zhangpengkun", "kind": "(all)", "target_uri": "viking://resources/shared-knowledge/_staging/zhangpengkun", "source_count": 8, "status": "ok", "detail": ""},
    {"group_key": "merge", "kind": "(all)", "target_uri": "viking://resources/shared-knowledge", "source_count": 2, "status": "ok", "detail": "merged"}
  ]
}
```

### Recover tasks after a refresh

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/runs"
```

### Read / edit the Team Memory Aggregation Skill

```bash
# Read the current Skill
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/okf-skill"

# Save an edited Skill
curl -X PUT -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/okf-skill" \
  -d '{"body": "---\nname: team-memory-okf\n...\n---\n# ..."}'
```

## 4. Response Contract and Error Handling

### Error codes

| HTTP status | Message | Cause |
|------------|---------|-------|
| 401 | `login required` | Not logged in |
| 403 | `team memory aggregation requires an administrator` | Non-admin access |
| 400 | `account_id is required` | No account provided and no default configured |
| 400 | (upstream message) | Listing users failed (e.g. OpenViking unreachable, missing root/trusted key) |
| 400 | `skill body must not be empty` | Saving empty Skill content |
| 404 | `unknown aggregation task` | task_id does not exist |

### Related configuration

| Config key (`aggregation.*`) | Default | Description |
|------|---------|-------------|
| `shared_knowledge_prefix` | `shared-knowledge` | Team memory output root prefix (under `viking://resources/`) |
| `staging_dir` | `_staging` | Phase 1 staging subdirectory |
| `okf_skill_uri` | `viking://agent/skills/team-memory-okf` | Source of the aggregation Skill name (trailing segment used as the skill name); output format is defined by this Skill |
| `kinds` | empty (built-in default set) | Memory categories to aggregate |
| `max_users_per_batch` | 12 | Per-compile source cap in Phase 1 (< 16) |
| `phase1_concurrency` | 6 | Phase 1 concurrency |
| `merge_fan_in` | 12 | Phase 2 tree-reduce fan-in width (2–15) |
| `compile_runtime_timeout_seconds` | 3000 | Per-compile runtime timeout |
| `state_dir` | empty (default `~/.teamEvolver/aggregation`) | Storage dir for incremental state and Skill content |

Config is registered in three places: `teamEvolver/config_store/defaults.py`, `teamEvolver/config_store/bridge.py`, `teamEvolver/config.py`.

### Limits and scale

- `ov compile` has a hard limit of 16 sources per task and 128 output pages/files.
- Phase 2 uses tree-reduce so every compile stays at or below `merge_fan_in` (≤15) sources regardless of user count; verified at 120-user scale without truncation or hitting the limit.
- The first full aggregation scales roughly linearly with user count (each compile takes tens of seconds); prefer incremental mode for routine use so only changed/failed users are recompiled.

### Identity and permissions

- Aggregation requires a trusted service identity. By default it directly reuses the admin-configured OpenViking key (stored in `sharing.viking_team_api_key` for compatibility); `aggregation.root_api_key` is only an advanced override.
- Phase 1 reads each user's own memory as "service/admin key + user header", simulating the target user identity.
- Output is written to `viking://resources/` (account-shared, writable by any role); no ROOT required.
