# 团队 Memory API

团队 Memory 把 Account 内各用户的个人 Memory 聚合为共享团队 Memory，并通过维护流水线更新。概念、流水线与配置字段见 [Memory 体系](../concepts/04-memory.md)。管理员可手动运行，或为维护阶段启用 Worker 定时触发。

代码入口：`team_memory/api/__init__.py:create_router`、`team_memory/api/skills.py:create_router`、`team_memory/api/replay.py:create_router`、`team_memory/pipeline.py`。

所有接口要求租户管理员。请求体上限 256 KiB。

## Memory 运行

### POST /api/aggregation/run

| 字段 | 说明 |
| --- | --- |
| `pipeline` | `both`（默认）、`aggregation`、`maintenance` |
| `mode` | `incremental`（默认）或 `full` |
| `user_ids` | 可选，只聚合这些用户，非空且不重复，最多 50000 |
| `kinds` | 可选，Memory 类型列表；默认取配置 `aggregation.kinds` |
| `target_uri` | 可选，默认 `viking://resources/<shared_knowledge_prefix>` |
| `account_id` | 可选，必须等于租户 Account，否则 400 `TENANT_ACCOUNT_MISMATCH` |
| `last_compile_time` | 可选，ISO 8601 |

```json
{"job_id": "memory:...", "task_id": "memory:...", "status": "queued", "status_url": "/api/jobs/memory:...",
 "target_uri": "viking://resources/shared-knowledge"}
```

支持 `Idempotency-Key`。同一 Account 同时只运行一个 Memory 任务，后到的任务在 Worker 侧等待租约。

### POST /api/memory/maintenance/run

与 `/api/aggregation/run` 相同，`pipeline` 固定为 `maintenance`。

### 查询

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/aggregation/runs` | 运行列表：`task_id`、`target_uri`、`status`、`result`、`created_at`、`finished_at`、`config_revision` |
| GET | `/api/aggregation/status/{task_id}` | 单个运行，不存在返回 404 |
| GET | `/api/memory/maintenance/status` | 最近一次维护运行，没有时返回 `{"status": "idle"}` |
| GET | `/api/memory/maintenance/dry-run` | 当前 `memory` 模块配置与 revision，不执行任何操作 |

## 设置

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/aggregation/settings` | 聚合设置、`maintenance_trigger_mode`、`maintenance_interval_seconds` 与 `revision` |
| POST | `/api/aggregation/settings` | 保存聚合与维护触发设置，必须携带整数 `expected_revision`，冲突返回 409 |

字段说明见 [Memory 体系 · 配置](../concepts/04-memory.md#配置)。

## 聚合 Skill 与维护 Skill

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/aggregation/okf-skill?stage=aggregation` | 读取 Skill：`skill_uri`、`skill_name`、`body`、`revision` |
| PUT | `/api/aggregation/okf-skill?stage=aggregation` | `{"body": "<SKILL.md>"}` 发布，返回 202 |
| POST | `/api/aggregation/users` | 列出 Account 下的 OpenViking 用户 |

`stage` 为 `aggregation` 或 `maintenance`，分别对应配置 `okf_skill_uri` 与 `maintenance_skill_uri`。发布时 `SKILL.md` 的 `name` 必须等于 URI 末段，正文不超过 128 KiB，作为 `memory.compile` 任务写入 OpenViking。

## Memory Change 与 Memory Replay

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/memory/changes?limit=100` | Memory Change 列表，`limit` 1–500 |
| POST | `/api/memory/changes/{change_id}/true-replay` | 用变更前后快照作为 Baseline / Candidate 运行 Replay，返回 202 |
| GET | `/api/memory/changes/{change_id}/true-replays?limit=100` | 该变更的 Replay 历史 |
| POST | `/api/openviking/memory/true-replay` | 直接提交 `memory_path`、`before_content`、`after_content` 及数据集，返回 202 |

Replay 请求的其余字段（数据集、`runtime`、`timeout_seconds`、`max_interactions`）与 [Replay 运行](./05-replay-branch.md#replay-运行) 一致。返回 `{"job_id", "run_id", "status_url"}`。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | 字段非法、`TENANT_ACCOUNT_MISMATCH`、Skill 名称与 URI 不一致、缺少 `expected_revision` |
| 403 | 非租户管理员 |
| 404 | 运行或 Memory Change 不存在 |
| 409 | 配置 revision 冲突 |
| 413 | 请求体超过 256 KiB |
| 503 | `memory` 模块配置缺失、OpenViking 配置不可用、Memory Replay 集成不可用 |
