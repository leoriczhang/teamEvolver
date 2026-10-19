# Knowledge Base Mining API

Knowledge base mining compiles raw knowledge directories in OpenViking into a structured Wiki. TeamEvolver handles acceptance, permissions, persisted state and result reconciliation; compilation is performed by OpenViking's native Compile with a mining Skill. Usage is in the [Knowledge Base Mining Guide](../guides/07-skill-miner.md).

Code entry points: `team_miner/api/__init__.py:create_router`, `team_miner/handlers.py`, `team_miner/config.py:load`, `team_miner/schemas.py`.

All endpoints require a tenant administrator and use the prefix `/api/knowledge-mining`.

## Configuration

The configuration is stored in the tenant `miner` module configuration (the `module='miner'` row in `tenant_configs`). There is no dedicated write API; operators edit it. Unset fields take defaults:

| Field | Default | Constraint |
| --- | --- | --- |
| `source_uri` | `viking://resources/agent_knowledge_workspace/input/raw_knowledge_base` | Must be under `viking://resources/` |
| `target_uri` | `viking://resources/agent_knowledge_workspace/output/processed_knowledge` | Must be under `viking://resources/` |
| `skill_uri` | `viking://agent/skills/llm-wiki` | Must be under `viking://agent/skills/` |
| `account_id` | Empty, takes the tenant `sharing_viking_account` | — |
| `user_id` | `team` | User used to access OpenViking |

The configuration must not contain credential fields (names containing key, password, token) or unknown fields; otherwise every endpoint returns 503. An `account` parameter in a request must equal the effective Account, otherwise `403 TENANT_ACCOUNT_MISMATCH` is returned.

Source and target directories are initialized parent-first; existing directories
and content remain unchanged. The Worker installs the default `llm-wiki` Skill
when missing, but it does not synthesize a custom `skill_uri`.

## Acceptance and Tasks

Write operations (compile, upload, edit, delete, graph generation) are all accepted as `miner.track_compile` tasks and return:

```json
{"ok": true, "job_id": "miner:...", "status": "queued", "status_url": "/api/jobs/miner:...", "account": "demo"}
```

`Idempotency-Key` is supported: the same key with the same request returns the same task; the same key with a different request returns 409. After a compile task is submitted to OpenViking, the Worker stores the upstream task receipt and polls for the result; on completion it records the `last_compile_time` watermark and derives a `miner.rebuild_index` task that rebuilds the Wiki index.

## Endpoints

| Method | Path | Description |
| --- | --- | --- |
| GET | `/config?account=` | Effective configuration, `revision` and Account |
| GET | `/capabilities?account=` | OpenViking Compile capability and the last `last_compile_time` |
| POST | `/tasks` | Start a compile, see below |
| GET | `/tasks?account=` | OpenViking compile tasks started by this tenant |
| GET | `/tasks/{identifier}?account=` | Status of one upstream task and its `job_id` |
| POST | `/tasks/{identifier}/cancel` | Cancel the upstream task and request cancellation of the local task |
| GET | `/jobs/{job_id}/result` | Task result; 404 until finished |
| GET | `/tree?kind=source|wiki` | Directory tree of `source_uri` or `target_uri` (at most 10000 nodes, 20 levels) |
| GET | `/wiki-roots` | Wiki root directories |
| GET | `/content?uri=&kind=source|wiki` | Read one file |
| POST | `/content` | `{"kind", "uri", "content"}` write one file (≤2 MiB) |
| POST | `/upload` | Upload files, see below |
| DELETE | `/entry?uri=&kind=source|wiki` | Delete a file or directory (the root cannot be deleted) |
| POST | `/graph` | Generate the Wiki graph; the result contains `graph` and `html` |

Every URI must be inside its root directory and must not contain `..`, hidden path segments or encoded characters.

### POST /api/knowledge-mining/tasks

| Field | Description |
| --- | --- |
| `account` | Optional, must equal the effective Account |
| `instruction` | Optional extra instructions, ≤4000 characters |
| `last_compile_time` | Optional, ISO 8601; when omitted, the last compile watermark is used for an incremental compile |
| `source_uri` / `target_uri` / `skill_uri` | May be omitted; when given, must equal the configured values |

Request body limit: 128 KiB.

### POST /api/knowledge-mining/upload

```json
{"kind": "source", "directory_uri": "viking://resources/.../raw_knowledge_base/policies",
 "files": [{"path": "退货政策.docx", "content_b64": "..."}]}
```

| Limit | Value |
| --- | --- |
| Files | 1–200 |
| Per file | 20 MiB |
| Total | 100 MiB |
| Formats | `.md`, `.markdown`, `.txt`, `.docx`, `.xlsx`, `.pdf`, `.csv`, `.pptx` |

The Worker converts each file to Markdown and writes it into the target directory (extension replaced by `.md`); the request is rejected when two files convert to the same name.

## Errors

| Status | Cause |
| --- | --- |
| 400 | Invalid field, URI outside root, file over limit, `instruction` too long, attempt to change the configured root or Skill |
| 403 | Not a tenant administrator, or `TENANT_ACCOUNT_MISMATCH` |
| 404 | Result not ready, or the upstream task does not belong to the current tenant |
| 409 | `Idempotency-Key` reused for a different request |
| 413 | Request body over limit |
| 503 | `miner` module configuration missing or invalid |
| 4xx/5xx | Error returned by OpenViking |
