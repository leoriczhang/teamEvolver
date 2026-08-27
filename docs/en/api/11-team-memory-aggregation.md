# Team Memory Aggregation API

## 1. API Overview

The Team Memory Aggregation API (Interface 1) aggregates the personal memories of multiple Users under one OpenViking Account into account-shared team memory via `ov compile`. Output defaults to `viking://resources/shared-knowledge/`, the actual target directory is configurable, and every user in the account can retrieve it.

Aggregation uses an account-shared Skill with a two-phase, tree-reduce model:

- **Shared Skill preparation:** the aggregation Skill is published once at `viking://agent/skills/team-memory-okf`. A run pins its content-addressed revision; the Skill is used only by Phase 2 compile tasks.
- **Phase 1 (deterministic staging):** each User identity reads its visible Memory text and writes a stable JSONL snapshot containing source URIs and content hashes. This phase invokes neither a model nor a Skill. A snapshot is written to a temporary directory and atomically moved to an immutable source-fingerprint path.
- **Phase 2 (tree-reduce merge):** as the team/admin identity and with the pinned Skill revision, staging snapshots are merged in bounded batches of `merge_fan_in` (default 4, ≤15). Above `partition_threshold`, output is published into stable hash partitions below the final root.

Additional behavior: concurrent Phase 1 (`phase1_concurrency`), source-fingerprint incremental skipping, failure isolation, and resumable reruns. Staging and `_merge` live under the merge identity's private Resources tree; only final team Memory is written to account-shared `viking://resources/...`. Skill changes invalidate merge output without recopying user Memory.

Implementation:
- Routes: `teamEvolver/proxy/aggregation_routes.py` (`AggregationMixin`)
- Orchestration: `teamEvolver/aggregation/service.py` (`MemoryAggregationService`)
- Deterministic staging: `teamEvolver/aggregation/staging.py` (`DeterministicStagingClient`)
- Compile invocation: `teamEvolver/aggregation/compile_client.py` (`CompileClient`)
- User enumeration: `teamEvolver/aggregation/sources.py` (`AccountSourceBuilder`)
- Incremental state: `teamEvolver/aggregation/state.py` (`AggregationState`)
- Default OKF Skill: `teamEvolver/aggregation/okf_skill.py` (`DEFAULT_OKF_SKILL_BODY`)

`CompileClient` calls OpenViking over HTTP. It reads or publishes the account-shared
Skill through `GET/POST/PUT /api/v1/skills`, then creates revision-pinned tasks
through `POST /bot/v1/compile` and polls `GET /bot/v1/compile/{task_id}`.
The teamEvolver host does not need the `ov` CLI, access to
`/app/.venv/bin/ov`, or a shared temporary directory with the OpenViking container.

## 2. Endpoints and Parameters

The reusable execution surface, `POST /api/aggregation/users`, `POST /api/aggregation/run`, and `GET /api/aggregation/status/{task_id}`, does not depend on TeamEvolver Cookies, users, or roles. External callers provide exactly one request-scoped credential: `root_key` for Trusted mode or `admin_key` for API-key mode. Credentials are never persisted or included in task state or responses. The TeamEvolver console omits both fields and, for an authenticated administrator, falls back to the configured Trusted Root Key. `runs`, `settings`, and Skill editing remain console-management endpoints protected by TeamEvolver administrator authentication.

---

### POST /api/aggregation/users

List the aggregatable Users under an Account (the team service user is excluded). Powers the console flow "enter Account → list users → select".

**Auth:** External callers need no TeamEvolver identity and provide exactly one of `root_key` or `admin_key`; console administrators may omit both and use the configured Root Key

**Request Body:**

| Field | Type | Required | Description |
|-----------|------|----------|-------------|
| `endpoint` | string | No | OpenViking HTTP(S) endpoint; defaults to `sharing.viking_endpoint` |
| `account_id` | string | No | OpenViking Account ID; defaults to `sharing.viking_account` when empty |
| `root_key` | string | One of two | Root Key for Trusted mode |
| `admin_key` | string | One of two | Admin Key for non-Trusted/API-key mode |

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `endpoint` | string | Normalized OpenViking endpoint actually used |
| `account_id` | string | The account actually used |
| `auth_mode` | string | `trusted` or `api_key` |
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

**Auth:** External callers need no TeamEvolver identity and provide exactly one of `root_key` or `admin_key`; console administrators may omit both and use the configured Root Key

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `endpoint` | string | No | OpenViking HTTP(S) endpoint for this run; defaults to `sharing.viking_endpoint` |
| `account_id` | string | No | Target account; defaults to `sharing.viking_account` |
| `root_key` | string | One of two | Root Key for Trusted mode |
| `admin_key` | string | One of two | Admin Key for non-Trusted/API-key mode |
| `target_uri` | string | No | Final output URI for this run; must be under `viking://resources/<path>`; defaults to the configured output root |
| `user_ids` | array[string] | No | Allowlist of users to aggregate; omit to aggregate all eligible users |
| `kinds` | array[string] | No | Memory categories to aggregate; omit to use the default set |
| `mode` | string | No | `incremental` (default, recompile only changed/failed users) or `full` (force recompile all) |

**Response (202):** initial Run object (`status` is `pending`/`running`).

`target_uri` is scoped to this run and does not mutate persisted settings. Incremental state is isolated by endpoint, Account, authentication mode, and target URI. Neither credential appears in the 202 response, task list, or status response.

API-key mode requires the OpenViking Admin user list to return complete `api_key` values. If the target deployment hashes API Keys and returns only `key_prefix`, the run fails before any compile starts. teamEvolver never regenerates User Keys automatically.

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_run`)

---

### GET /api/aggregation/status/{task_id}

Query a task's live progress (group-level status).

**Auth:** No TeamEvolver authentication; possession of the unguessable `task_id` grants access to that task

**Path parameters:**

| Parameter | Type | Required | Description |
|-----------|------|----------|-------------|
| `task_id` | string | Yes | Random task ID returned by `run`; treat it as the status-query credential |

**Run object fields:**

| Field | Type | Description |
|-------|------|-------------|
| `task_id` | string | Task ID |
| `endpoint` | string | OpenViking endpoint actually used by this run |
| `account_id` | string | Target account |
| `auth_mode` | string | `trusted` or `api_key` |
| `target_uri` | string | Normalized final output URI for this run |
| `work_root` | string | Actual private staging/intermediate root owned by the merge identity |
| `skill_uri` | string | Account-shared Skill URI used by the run |
| `skill_revision` | string | Content revision pinned by the run |
| `status` | string | `pending`, `running`, `completed`, `failed` |
| `started_at` | number | Start timestamp |
| `finished_at` | number\|null | Finish timestamp (null while running) |
| `error` | string | Failure reason (when failed) |
| `source_user_count` | integer | Number of selected source users |
| `publish_mode` | string | `single` or `partitioned` |
| `partition_count` | integer | Active publish partitions |
| `estimated_merge_tasks` | integer | Planned merge compile count |
| `group_total` | integer | Total processed groups |
| `group_counts` | object | Group totals by `ok`, `skipped`, and `failed` |
| `groups_truncated` | boolean | Whether detail rows were capped |
| `groups` | array | Group results (Group objects, see below) |

**Group object fields:**

| Field | Type | Description |
|-------|------|-------------|
| `group_key` | string | `stage:<uid>` (per-user staging), `merge:L<n>:g<i>` (intermediate merge), or `merge` (final merge) |
| `kind` | string | Category marker (currently unified as `(all)`) |
| `target_uri` | string | Output URI of the group |
| `source_count` | integer | Number of sources for the group |
| `status` | string | `ok`, `skipped`, `failed` |
| `detail` | string | Note (e.g. `unchanged (reused deterministic snapshot)`, error message) |

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_status`)

---

### GET /api/aggregation/okf-skill

Read the currently effective Team Memory Aggregation Skill (the user-edited content, or the default template).

**Auth:** Console Cookie (admin)

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `skill_name` | string | Skill name (default `team-memory-okf`) |
| `skill_uri` | string | Account-shared Skill URI |
| `revision` | string | Current content revision |
| `source` | string | `openviking` or `local_fallback` before first publication |
| `body` | string | SKILL.md content |
| `editable` | boolean | Always true |

Code entry: `teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_okf_skill`)

---

### PUT /api/aggregation/okf-skill

Save the edited Team Memory Aggregation Skill. The content is immediately published to `viking://agent/skills/<name>` with a path-scoped version snapshot. `<state_dir>/okf_skill.md` remains only as the first-publish fallback. The next aggregation reads the shared Skill and pins its revision.

**Auth:** Console Cookie (admin)

**Request Body:**

| Field | Type | Required | Description |
|-------|------|----------|-------------|
| `body` | string | Yes | New SKILL.md content, must not be empty |
| `version_message` | string | No | Version description |

**Response fields:**

| Field | Type | Description |
|-------|------|-------------|
| `ok` | boolean | Whether the save succeeded |
| `body` | string | Saved content |
| `skill_uri` | string | Shared Skill URI |
| `revision` | string | Published content revision |

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
| `staging_dir` | string | Directory segment within the private work root |
| `work_root` | string | Private work-root template, such as `viking://user/{merge_user}/resources/teamEvolver/staging/<target-hash>` |
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
| `staging_dir` | string | No | Directory segment within the private work root |
| `okf_skill_uri` | string | No | Aggregation Skill identifier |
| `kinds` | array[string] | No | Memory category list; empty items are removed |

The response contains every `GET /api/aggregation/settings` field plus `"ok": true`.

## 3. Usage Examples

### List aggregatable users

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/users" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","admin_key":"<openviking-admin-key>"}'
```

Response:

```json
{
  "endpoint": "https://openviking.example.com",
  "account_id": "default",
  "auth_mode": "api_key",
  "users": ["alice", "bob", "chenghan", "zhangpengkun"]
}
```

### Trigger aggregation (selected users + incremental mode)

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","admin_key":"<openviking-admin-key>","target_uri":"viking://resources/engineering-memory","user_ids":["chenghan","zhangpengkun"],"mode":"incremental"}'
```

Response (202):

```json
{
  "task_id": "agg_r4nd0m-capability-token",
  "endpoint": "https://openviking.example.com",
  "account_id": "default",
  "auth_mode": "api_key",
  "target_uri": "viking://resources/engineering-memory",
  "work_root": "viking://user/admin/resources/teamEvolver/staging/8f3a2d4c6b7e90123456",
  "status": "running",
  "started_at": 1756100000.0,
  "finished_at": null,
  "error": "",
  "groups": []
}
```

### Poll task progress

```bash
curl \
  "http://localhost:52010/api/aggregation/status/agg_r4nd0m-capability-token"
```

Response (completed):

```json
{
  "task_id": "agg_r4nd0m-capability-token",
  "endpoint": "https://openviking.example.com",
  "account_id": "default",
  "auth_mode": "api_key",
  "target_uri": "viking://resources/engineering-memory",
  "work_root": "viking://user/admin/resources/teamEvolver/staging/8f3a2d4c6b7e90123456",
  "status": "completed",
  "groups": [
    {"group_key": "stage:chenghan", "kind": "(all)", "target_uri": "viking://user/admin/resources/teamEvolver/staging/8f3a2d4c6b7e90123456/users/chenghan/snapshots/12ab...", "source_count": 1, "status": "ok", "detail": "copied 1 source files into 1 JSONL chunks"},
    {"group_key": "stage:zhangpengkun", "kind": "(all)", "target_uri": "viking://user/admin/resources/teamEvolver/staging/8f3a2d4c6b7e90123456/users/zhangpengkun/snapshots/34cd...", "source_count": 8, "status": "ok", "detail": "copied 8 source files into 1 JSONL chunks"},
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
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","admin_key":"<openviking-admin-key>","target_uri":"viking://resources/engineering-memory","user_ids":["alice","bob"]}'
```

### Use a Trusted Root Key

For an external call to a Trusted deployment, replace `admin_key` with
`root_key`:

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","root_key":"<openviking-root-key>","target_uri":"viking://resources/engineering-memory","user_ids":["alice","bob"]}'
```

Console requests omit both fields. After TeamEvolver administrator
authentication, the server uses its configured Root Key and keeps Trusted mode
as the default.

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
| 401 | `login required` | An unauthenticated caller accessed `runs`, `settings`, or a Skill-management endpoint |
| 403 | `team memory aggregation requires an administrator` | A non-admin caller accessed a management endpoint |
| 400 | `aggregation users body must be an object` | User-enumeration body is not a JSON object |
| 400 | `aggregation run body must be an object` | Run body is not a JSON object |
| 400 | `exactly one of root_key or admin_key is required` | An external request omitted its credential |
| 400 | `root_key and admin_key are mutually exclusive` | Both credential types were supplied |
| 400 | `root_key must be a string` | `root_key` has the wrong type |
| 400 | `admin_key must be a string` | `admin_key` has the wrong type |
| 400 | `trusted root key is not configured` | The console Trusted path has no configured Root Key |
| 400 | `endpoint must be a string` | `endpoint` is not a string |
| 400 | `endpoint is required` | The request omitted the endpoint and no default endpoint is configured |
| 400 | `endpoint must be a valid HTTP(S) URL` | The endpoint scheme or URL structure is invalid |
| 400 | `api_key mode requires plaintext per-user API keys...` | User records contain only Key prefixes or omit User Keys; no rotation was attempted |
| 400 | `admin_key owner could not be identified...` | The Admin Key owner could not be identified from the user list |
| 400 | `admin list-users reached safety limit...` | The user count exceeded `account_user_limit` |
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
| `staging_dir` | `staging` | Work-directory segment under the merge identity's private Resources |
| `okf_skill_uri` | `viking://agent/skills/team-memory-okf` | Source of the aggregation Skill name (trailing segment used as the skill name); output format is defined by this Skill |
| `kinds` | empty (built-in default set) | Memory categories to aggregate |
| `max_users_per_batch` | 12 | Compatibility field; deterministic staging does not consume it |
| `account_user_limit` | 50000 | Maximum users accepted for one Account-wide run |
| `account_user_page_size` | 1000 | Stable Admin user-list page size |
| `phase1_concurrency` | 6 | Maximum concurrent per-user snapshot copies |
| `merge_fan_in` | 4 | Phase 2 tree-reduce fan-in width (2–15) |
| `merge_concurrency` | 4 | Concurrent merge groups |
| `partition_threshold` | 512 | User count that enables partitioned publication |
| `partition_count` | 256 | Fixed hash partition count |
| `run_detail_limit` | 2000 | Maximum retained status detail rows |
| `compile_runtime_timeout_seconds` | 3000 | Per-compile runtime timeout |
| `state_dir` | empty (default `~/.teamEvolver/aggregation`) | Storage dir for incremental state and Skill content; state is isolated by endpoint, Account, and target URI |

Config is registered in three places: `teamEvolver/config_store/defaults.py`, `teamEvolver/config_store/bridge.py`, `teamEvolver/config.py`.

### Limits and scale

- `ov compile` has a hard limit of 16 sources per task and 128 output pages/files.
- Phase 2 keeps every compile at or below `merge_fan_in` (≤15) sources. Runs above 512 users publish stable partitions so the 128-page per-compile output limit does not cap the complete team corpus.
- Phase 1 makes no model calls. Incremental runs recopy only changed/failed users and recompile only affected merge branches.
- A deterministic 10,000-user planning test verifies bounded worker count, all 256 stable partitions, and roughly 3,600 merge tasks for a full initial run. This is operationally resumable, not instantaneous.
- Skill uploads and compile submissions retry connection-establishment failures up to three times with exponential backoff. Other POST failures are not replayed because the upstream may already have accepted the request. Compile-status polling is an idempotent GET and retries transient transport failures.

### Compile capacity diagnostics

OpenViking deployments that expose capacity status can be queried with an
authenticated request:

```bash
curl -H "X-API-Key: <openviking-key>" \
  -H "X-OpenViking-Account: <account-id>" \
  -H "X-OpenViking-User: <user-id>" \
  "https://openviking.example.com/bot/v1/compile/status"
```

Key fields:

- `worker_model=in_process_asyncio`: compile runs as in-process tasks inside the VikingBot gateway; there is no separate compile worker process.
- `available_execution_slots`: currently free execution slots; a sustained zero means capacity is exhausted.
- `running_tasks` / `queued_tasks`: tasks executing and waiting for a slot.
- `queue_wait_seconds`: maximum time a task may wait for an execution slot.

When many tasks fail in `queued` while `available_execution_slots=0`, reduce
caller-wide concurrency or increase tested OpenViking capacity before
investigating the VLM. The current local source deployment aligns 40 admitted
tasks and 10 execution slots by raising queue wait from one hour to four hours,
covering four worst-case execution waves.

### Identity and permissions

- External calls provide exactly one credential: `root_key` selects `trusted`; `admin_key` selects `api_key`. Neither credential is persisted or returned.
- The console remains backward compatible: an administrator session may omit credentials and use `sharing.viking_team_api_key` as the Trusted Root Key.
- `endpoint` can be overridden per request and is not persisted. It must be an HTTP(S) URL without user information, query parameters, or fragments.
- In API-key mode, teamEvolver reads existing plaintext Keys from the Admin user list. Each User's inventory and content reads use only that User's own Key.
- Snapshots are written to the merge identity's private Resources. API-key mode uses the actual Admin User; Trusted mode uses the configured team User.
- User Keys exist only in background-worker memory. They never enter HTTP responses, Runs, logs, incremental state, or persisted configuration.
- Deployments with API Key hashing enabled cannot use this compatibility path. The service fails closed and never regenerates or replaces User Keys automatically.
- `task_id` is a high-entropy random value. Callers without a TeamEvolver identity use it to query one task; listing all tasks remains restricted to console administrators.
- Expose execution endpoints only over HTTPS or a trusted network so the Admin Key is protected in transit.
- Final output is written to the run's `target_uri`, or to `viking://resources/<shared_knowledge_prefix>/` when omitted; raw snapshots and intermediates remain under the merge identity's private work root.
