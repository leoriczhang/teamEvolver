# Team Memory Aggregation API

## 1. API Overview

The Team Memory Aggregation API (Interface 1) aggregates the personal memories of multiple Users under one OpenViking Account into account-shared team memory via `ov compile`. Output defaults to `viking://resources/shared-knowledge/`, the actual target directory is configurable, and every user in the account can retrieve it.

Aggregation uses a two-phase, tree-reduce model. It requires no OpenViking source changes:

- **Phase 1 (per-user staging):** for each selected User, compile uses the OpenViking Admin Key supplied in the request and reads that user's Memory. Output goes to a work root beside the final directory, for example `viking://resources/shared-knowledge-staging/<uid>`. The aggregation Skill is installed into that user's own Skill space first so the same identity can read it.
- **Phase 2 (tree-reduce merge):** as the team user, all staging roots are merged in bounded batches of `merge_fan_in` (default 12, ≤15) across cascading levels, down to the final `viking://resources/shared-knowledge/` root. Tree-reduce keeps every compile under the 16-source hard limit, supporting 100+ users.

Additional behavior: concurrent Phase 1 (`phase1_concurrency`), content-fingerprint incremental skipping (unchanged users reuse prior staging), and failure isolation with resumable reruns. Staging and `_merge` paths stay under the work root and never pollute the final team-Memory root or its L0/L1 summaries.

Implementation:
- Routes: `teamEvolver/proxy/aggregation_routes.py` (`AggregationMixin`)
- Orchestration: `teamEvolver/aggregation/service.py` (`MemoryAggregationService`)
- Compile invocation: `teamEvolver/aggregation/compile_client.py` (`CompileClient`)
- User enumeration: `teamEvolver/aggregation/sources.py` (`AccountSourceBuilder`)
- Incremental state: `teamEvolver/aggregation/state.py` (`AggregationState`)
- Default OKF Skill: `teamEvolver/aggregation/okf_skill.py` (`DEFAULT_OKF_SKILL_BODY`)

## 2. Endpoints and Parameters

All `/api/aggregation/*` endpoints require console **administrator** authentication (admin role). User enumeration and aggregation runs also require an OpenViking Admin Key in the request body. The Key is not persisted and never appears in task state or responses.

---

### POST /api/aggregation/users

List the aggregatable Users under an Account (the team service user is excluded). Powers the console flow "enter Account → list users → select".

**Auth:** Console Cookie (admin)

**Request Body:**

| Field | Type | Required | Description |
|-----------|------|----------|-------------|
| `account_id` | string | No | OpenViking Account ID; defaults to `sharing.viking_account` when empty |
| `admin_key` | string | Yes | OpenViking Admin Key; scoped to this request, never persisted or returned |

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `account_id` | string | The account actually used |
| `users` | array[string] | Aggregatable user_id list |

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_users`)

---

### GET /api/aggregation/runs

List recent aggregation tasks (newest first, up to 20). Used to **recover progress after a page refresh** for in-flight or recent tasks.

**Auth:** Console Cookie (admin)

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `runs` | array | Task list (Run objects, see below) |

> Note: task state lives in server memory. Restarting teamEvolver clears the list, and in-process work is not guaranteed to continue. Persisted incremental fingerprints are unaffected.

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_runs`)

---

### POST /api/aggregation/run

Start a background aggregation task; returns 202 with the initial Run object immediately. The task runs in a worker thread; poll progress via the `status` endpoint.

**Auth:** Console Cookie (admin)

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `account_id` | string | No | Target account; defaults to `sharing.viking_account` |
| `admin_key` | string | Yes | OpenViking Admin Key; retained only while the background task executes and excluded from the Run object |
| `target_uri` | string | No | Final output URI for this run; must be under `viking://resources/<path>`; defaults to the configured output root |
| `user_ids` | array[string] | No | Allowlist of users to aggregate; omit to aggregate all eligible users |
| `kinds` | array[string] | No | Memory categories to aggregate; omit to use the default set |
| `mode` | string | No | `incremental` (default, recompile only changed/failed users) or `full` (force recompile all) |

**Response (202):** initial Run object (`status` is `pending`/`running`).

`target_uri` is scoped to this run and does not mutate persisted settings. The service derives a separate sibling work root and incremental state for each target URI, so staging data and fingerprints are not reused across output locations. `admin_key` is excluded from the 202 response, task list, and status response.

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_run`)

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
| `target_uri` | string | Normalized final output URI for this run |
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

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_status`)

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

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_okf_skill`)

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

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_okf_skill_save`)

---

### GET /api/aggregation/settings

Read aggregation settings and their computed paths.

**Auth:** Console Cookie (admin)

| Response field | Type | Description |
|----------------|------|-------------|
| `enabled` | boolean | Aggregation marker in configuration |
| `shared_knowledge_prefix` | string | Default output prefix used when a run omits `target_uri` |
| `target_root` | string | Computed final root, such as `viking://resources/shared-knowledge` |
| `staging_dir` | string | Work-root suffix |
| `work_root` | string | Computed sibling work root, such as `viking://resources/shared-knowledge-staging` |
| `okf_skill_uri` | string | Aggregation Skill identifier |
| `key_seed` | string | Compatibility field; the current runtime does not derive user keys from it |
| `kinds` | array[string] | Explicit Memory categories; empty means the built-in set |

---

### POST /api/aggregation/settings

Persist editable aggregation settings through `ConfigStore`, then hot-reload OpenViking, DreamCycle, and embedded evolution integrations.

**Auth:** Console Cookie (admin)

| Request Body field | Type | Required | Description |
|--------------------|------|----------|-------------|
| `shared_knowledge_prefix` | string | No | Default output prefix, used only when a run omits `target_uri` |
| `staging_dir` | string | No | Sibling work-root suffix |
| `okf_skill_uri` | string | No | Aggregation Skill identifier |
| `kinds` | array[string] | No | Memory category list; empty items are removed |

The response contains every `GET /api/aggregation/settings` field plus `"ok": true`.

## 3. Usage Examples

### List aggregatable users

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/users" \
  -d '{"account_id":"default","admin_key":"<openviking-admin-key>"}'
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
  -d '{"account_id":"default","admin_key":"<openviking-admin-key>","target_uri":"viking://resources/engineering-memory","user_ids":["chenghan","zhangpengkun"],"mode":"incremental"}'
```

Response (202):

```json
{
  "task_id": "agg_1a03c010043",
  "account_id": "default",
  "target_uri": "viking://resources/engineering-memory",
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
  "target_uri": "viking://resources/engineering-memory",
  "status": "completed",
  "groups": [
    {"group_key": "stage:chenghan", "kind": "(all)", "target_uri": "viking://resources/engineering-memory-staging/chenghan", "source_count": 1, "status": "ok", "detail": ""},
    {"group_key": "stage:zhangpengkun", "kind": "(all)", "target_uri": "viking://resources/engineering-memory-staging/zhangpengkun", "source_count": 8, "status": "ok", "detail": ""},
    {"group_key": "merge", "kind": "(all)", "target_uri": "viking://resources/engineering-memory", "source_count": 2, "status": "ok", "detail": "merged"}
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

### Specify the output URI for one run

`POST /api/aggregation/run` accepts a complete target URI without changing global settings:

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"account_id":"default","target_uri":"viking://resources/engineering-memory","user_ids":["alice","bob"]}'
```

### Inspect and change the default output directory

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/settings"

curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/settings" \
  -d '{"shared_knowledge_prefix": "engineering-memory"}'
```

## 4. Response Contract and Error Handling

### Error codes

| HTTP status | Message | Cause |
|------------|---------|-------|
| 401 | `login required` | Not logged in |
| 403 | `team memory aggregation requires an administrator` | Non-admin access |
| 400 | `aggregation users body must be an object` | User-enumeration body is not a JSON object |
| 400 | `aggregation run body must be an object` | Run body is not a JSON object |
| 400 | `admin_key is required` | No valid OpenViking Admin Key was supplied |
| 400 | `target_uri must be a string` | `target_uri` is not a string |
| 400 | `target_uri must be a valid path under viking://resources/<path>` | URI is invalid, targets the resources root, or leaves the shared resources namespace |
| 400 | (upstream message) | OpenViking is unreachable or the Admin Key is unauthorized |
| 400 | `skill body must not be empty` | Saving empty Skill content |
| 400 | `aggregation settings body must be an object` | Settings body is not a JSON object |
| 400 | `shared_knowledge_prefix is required` | Output prefix is empty |
| 400 | `shared_knowledge_prefix must be at most 120 characters` | Output prefix is too long |
| 404 | `unknown aggregation task` | task_id does not exist |

### Related configuration

| Config key (`aggregation.*`) | Default | Description |
|------|---------|-------------|
| `shared_knowledge_prefix` | `shared-knowledge` | Default team-memory output prefix when a run omits `target_uri` |
| `staging_dir` | `staging` | Sibling work-root suffix; combined as `<prefix>-<staging_dir>` |
| `okf_skill_uri` | `viking://agent/skills/team-memory-okf` | Source of the aggregation Skill name (trailing segment used as the skill name); output format is defined by this Skill |
| `kinds` | empty (built-in default set) | Memory categories to aggregate |
| `max_users_per_batch` | 12 | Per-compile source cap in Phase 1 (< 16) |
| `phase1_concurrency` | 6 | Phase 1 concurrency |
| `merge_fan_in` | 12 | Phase 2 tree-reduce fan-in width (2–15) |
| `compile_runtime_timeout_seconds` | 3000 | Per-compile runtime timeout |
| `state_dir` | empty (default `~/.teamEvolver/aggregation`) | Storage dir for incremental state and Skill content; state is isolated by Account and target URI |

Config is registered in three places: `teamEvolver/config_store/defaults.py`, `teamEvolver/config_store/bridge.py`, `teamEvolver/config.py`.

### Limits and scale

- `ov compile` has a hard limit of 16 sources per task and 128 output pages/files.
- Phase 2 uses tree-reduce so every compile stays at or below `merge_fan_in` (≤15) sources regardless of user count; verified at 120-user scale without truncation or hitting the limit.
- The first full aggregation scales roughly linearly with user count (each compile takes tens of seconds); prefer incremental mode for routine use so only changed/failed users are recompiled.

### Identity and permissions

- `admin_key` is required for every user-enumeration and aggregation-run request and exists only for the request/background-worker lifetime. It is never persisted or returned.
- Aggregation does not read a fallback credential from persisted configuration.
- Phase 1 uses the Admin Key with the target user identity to read each user's Memory.
- Final output is written to the run's `target_uri`, or to `viking://resources/<shared_knowledge_prefix>/` when omitted; intermediate artifacts stay in that target's sibling work root.
