# Workspace and Platform Assets API

The console "Personal & Team Assets" page browses and edits Memory, Skills and Resources in OpenViking through the Workspace API; "Platform Assets" browses task artifacts in PostgreSQL read-only. The directory layout is in [Storage Spaces and Layout](../concepts/09-storage-layout.md).

Code entry points: `teamEvolver/platform_docs/api/workspace.py:create_router`, `teamEvolver/platform_docs/workspace_core.py:_scope_map`, `teamEvolver/platform_docs/api/assets.py:create_router`, `teamEvolver/platform_docs/api/console.py:create_router`.

## Console Entry

`GET /` and `GET /console` return the built console `index.html`, or `503 console assets are not built` when not built. Static assets are mounted at `/assets` and documentation images at `/docs-assets`.

## Workspace

### Permissions

- Every endpoint requires console sign-in (or the Root Key). `user_id` defaults to the current user; operating on another user's space requires a tenant administrator.
- Personal scopes (`personal_*`, `personal_workspace`) are writable by the owner; team scopes (`team_*`, `team_workspace`) are writable only by tenant administrators, and other users get `403 only administrators can modify team OpenViking scopes`.
- A URI must be inside the scope root, otherwise 403; the scope root itself cannot be modified.
- Requests use the tenant Account and the OpenViking User of the scope; the Root Key stays on the server.

### Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/openviking/workspace/config?user_id=` | Whether enabled, deployment mode, OpenViking address, Account, root URI and writability of each scope, CLI availability |
| GET | `/api/openviking/workspace/list?scope=&uri=&user_id=` | List a directory (at most 2000 entries) |
| GET | `/api/openviking/workspace/tree?scope=&uri=&user_id=` | Recursive directory tree (at most 10000 nodes) |
| GET | `/api/openviking/workspace/content?scope=&uri=&user_id=` | Read a file with content and content hash |
| GET | `/api/openviking/workspace/level?scope=&uri=&level=&user_id=` | L0 summary (`l0` / `abstract`) or L1 overview (`l1` / `overview`) |
| POST | `/api/openviking/workspace/content` | `{"scope", "uri", "content", "mode", "user_id"?}` write one file; `mode` is `replace` (default), `create` or `append`; ≤2 MiB |
| POST | `/api/openviking/workspace/batch-content` | Conditional batch write, see below |
| POST | `/api/openviking/workspace/mkdir` | `{"scope", "uri", "user_id"?}` create a directory |
| DELETE | `/api/openviking/workspace?scope=&uri=&user_id=` | Delete a file or directory |
| GET | `/api/openviking/workspace/aggregate?kind=all&user_id=` | Merged personal and team Skill, Memory and Resource lists, each tagged with `source` (`personal` / `team`); `kind` is `skills`, `memory`, `resources` or `all` |
| POST | `/api/openviking/workspace/cli` | `{"scope", "current_uri", "command", "user_id"?}` run an OpenViking CLI command |

### Conditional Batch Write

```json
{"user_id": "alice", "changes": [
  {"scope": "personal_memory", "uri": "viking://user/alice/memories/profile.md",
   "original_content": "...", "content": "..."}
]}
```

- At most 100 files, each ≤2 MiB and ≤16 MiB in total; only Memory and Skill scopes are allowed.
- The server reads the current content of each file and compares it with `original_content`; if any file was changed by someone else, the whole batch returns `409 workspace files changed since editing began` and nothing is written.
- A duplicate URI in the same request returns 400.

### CLI

The CLI requires the OpenViking CLI on the server (`OPENVIKING_CLI_BIN` sets the path). The `ov` / `openviking` prefix may be omitted; `ls` and `tree` without arguments act on `current_uri`.

Users who are both platform administrator and tenant administrator can run any command. Other users can run only `abstract`, `find`, `glob`, `grep`, `health`, `ls`, `overview`, `read`, `search`, `session`, `stat`, `status`, `tree`, `version`, cannot use `--account`, `--user` or `--sudo`, every `viking://` URI in the command must be inside the selected scope, and searches are restricted to `current_uri`. Output is limited to 512 KiB with a 300-second timeout.

### When OpenViking Is Unavailable

When OpenViking is unreachable, reads and writes return 503; L0 / L1 return empty content.

## Platform Assets

Platform assets show, read-only, immutable task payloads in the PostgreSQL ObjectStore: ingest originals (`ingest`), Session stage results (`session`) and Memory Compile results (`memory`). A tenant administrator is required, and only objects of the current tenant are visible.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/platform-assets/tree` | Sources, per-category counts and entry list |
| GET | `/api/platform-assets/content?uri=` | Preview one object; `uri` looks like `platform://artifacts/<key>`, ≤4 MiB |

| Status | Cause |
| --- | --- |
| 400 | `invalid platform asset URI` / `select a platform asset file` |
| 403 | Not a tenant administrator, or the path is not exposed |
| 404 | Object does not exist |
| 413 | Over the 4 MiB preview limit |
| 503 | Storage unavailable |
