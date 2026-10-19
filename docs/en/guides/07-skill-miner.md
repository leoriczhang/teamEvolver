# Knowledge Base Mining Guide

Knowledge base mining compiles a raw knowledge directory in OpenViking into a structured Wiki. Compilation is performed by OpenViking's native Compile with a mining Skill; teamEvolver handles acceptance, permissions, task persistence, cancellation and result reconciliation. The code is in `team_miner/` and the console page in `team_miner/frontend/`. API details are in [Knowledge Base Mining API](../api/22-knowledge-mining.md).

## Prerequisites

- The tenant has an OpenViking deployment, Account and Root Key configured ("Runtime Status" → "OpenViking Deployment")
- The OpenViking server supports Compile. teamEvolver installs the default mining
  Skill `viking://agent/skills/llm-wiki` when it is missing; custom Skills must
  still be installed beforehand.
- The current user is a tenant administrator
- The Worker role is running; it executes the `miner.track_compile` and `miner.rebuild_index` tasks

Without OpenViking, the console "Knowledge Base Mining" menu shows a not-configured marker and the page links to "Runtime Status".

## Directory Conventions

A mining run involves three URIs, all from the tenant `miner` module configuration:

| Field | Default | Purpose |
|------|--------|------|
| `source_uri` | `viking://resources/agent_knowledge_workspace/input/raw_knowledge_base` | Knowledge sources: Markdown converted from the original documents |
| `target_uri` | `viking://resources/agent_knowledge_workspace/output/processed_knowledge` | Knowledge base: the compiled Wiki |
| `skill_uri` | `viking://agent/skills/llm-wiki` | Mining Skill used by Compile |

`source_uri` and `target_uri` must be under `viking://resources/`, and `skill_uri` under `viking://agent/skills/`. There is no write API for this configuration; operators edit the `module='miner'` row in `tenant_configs`. If the configuration contains credential fields or unknown fields, every mining API returns 503.

Before the first browse, upload, or mining run, teamEvolver checks `source_uri`
and `target_uri` from parent to child. Missing directories are created
automatically; existing directories and their contents are reused unchanged.

## Workflow

### 1. Upload Knowledge Sources

In the "Knowledge Sources" tree, select a target directory and upload files or a folder. Supported formats: `.md`, `.markdown`, `.txt`, `.docx`, `.xlsx`, `.pdf`, `.csv`, `.pptx`. Up to 200 files per upload, each ≤20 MiB and ≤100 MiB in total.

An upload is accepted as a background task. The Worker converts each file to Markdown (extension replaced by `.md`), writes it to OpenViking and records a checkpoint per file; a retried task skips files already written. The request is rejected when two files convert to the same name.

### 2. Organize Knowledge Sources

The file tree supports previewing, editing and deleting files or directories. The root directory itself cannot be deleted. Edits are written back to OpenViking directly.

### 3. Start Compile

Optionally enter "mining instructions" (≤4000 characters), choose the incremental compile start and launch. The first run is a full compile; afterwards the start time of the last successful compile is used as the `last_compile_time` watermark by default, so only new or changed content is processed.

Compile task execution:

1. The API persists the request, enqueues `miner.track_compile` and returns a `job_id`.
2. The Worker submits Compile to OpenViking with the `job_id` as idempotency key and stores the upstream `task_id` receipt.
3. The Worker polls the upstream task until it ends. After a process restart it resumes from the receipt without resubmitting.
4. On success it advances the watermark and derives a `miner.rebuild_index` task that rebuilds the Wiki link index.

The page shows OpenViking Compile stage progress. Stopping cancels both the upstream task and the local task.

### 4. View the Knowledge Base

After compile, browse Wiki pages in the "Knowledge Base" tree and view backlinks. Generating the graph builds an HTML knowledge base graph from Wiki links.

## Concurrency and Idempotency

- All write operations accept an `Idempotency-Key` header: the same key with the same request returns the same task; a different request returns 409.
- Watermark and index updates for the same `(account_id, source_uri, target_uri, skill_uri)` scope are serialized with a PostgreSQL advisory lock.
- Only the compile task matching the current watermark writes the index, so an older task finishing late never overwrites a newer index.

## Troubleshooting

| Symptom | Action |
|------|------|
| Every API returns 503 | Check whether the `miner` module configuration is missing or contains credential or unknown fields |
| `403 TENANT_ACCOUNT_MISMATCH` | The request `account` differs from the tenant's effective OpenViking Account |
| Task stays queued | Make sure the Worker role is running; check processes with `teamEvolver status` |
| Task fails with `miner_compile_failed` | Check the upstream OpenViking task log; verify that a custom mining Skill is installed |
| Upload fails with `miner_normalized_upload_limit` | Converted Markdown exceeds 100 MiB in total; split the upload |

## Related Documents

- [Knowledge Base Mining API](../api/22-knowledge-mining.md)
- [Workspace and Platform Assets API](../api/23-workspace-assets.md)
- [Web Console User Guide](./03-console.md)
