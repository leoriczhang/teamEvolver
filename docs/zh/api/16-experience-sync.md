# 成功经验 API

成功经验来自 Session 分析：Worker 把 Session 中的 `exemplary`（成功做法）与 `defect`（缺陷）经验写入 `skill_experiences`，再按 Skill 编译为 `experience_patterns`，供 Skill 进化使用。启用经验同步后，Worker 把 `exemplary` 经验投影到 OpenViking 目录，供知识库挖掘等下游读取。

代码入口：`team_skills/api/experiences.py:create_router`、`team_skills/experiences/supervisor.py:experience_tick`、`team_skills/experiences/transport.py`。

读取经验与 Pattern 要求租户成员；编译、同步状态与触发要求租户管理员。受理接口返回 `{"status": "accepted", "job_id", "status_url"}`，任务 kind 为 `experience.sync`。

## 经验与 Pattern

### GET /api/skill-experiences

| 参数 | 说明 |
| --- | --- |
| `kind` | `exemplary`、`defect` 或空 |
| `skill` | Skill 名称，≤200 字符 |
| `search` | 全文匹配，≤500 字符 |
| `limit` / `offset` | `limit` 1–200，默认 50 |

返回经验文档列表、总数，以及按 Skill 与类型分组的统计。

### GET /api/skill-experience-patterns

参数同上，另有 `status`：`ready`（证据充分，可用于进化）或 `observing`（证据积累中）。

### POST /api/skill-experience-patterns/compile

```json
{"skill": "cost-report", "force": false}
```

用该 Skill 当前的经验重新编译 Pattern。输入在受理时冻结，`source_hash` 未变且 `force=false` 时 Worker 复用已有结果。

## 经验同步

同步目标保存在租户 `skills` 模块配置的 `experience_sync` 对象，没有专用 API，由运维修改 `tenant_configs` 中的 `skills` 行：

| 字段 | 说明 |
| --- | --- |
| `enabled` | 是否投影到 OpenViking，默认 false |
| `endpoint` | OpenViking 地址，HTTP(S)，不含内联凭证、query 或 fragment |
| `account_id`、`user_id` | 写入使用的 OpenViking Account 与 User |
| `secret_ref` | OpenViking Key 的 Secret Ref |
| `target_directory` | 默认 `viking://resources/agent_knowledge_workspace/input/proven_experiences` |
| `interval_seconds` | 自动扫描间隔，默认 30，最小 5 |

Worker 每轮租户刷新时执行 `experience_tick`：距上次扫描超过 `interval_seconds` 时受理一次增量扫描（从上次水位回退 120 秒）；`enabled=true` 时再派发待投递条目。该调度由租约保护，多副本 Worker 不重复执行。

每条经验写入 `<target_directory>/<tenant 哈希>/<subject_type>/<subject 哈希>.json`。写入后回读校验正文，并等待 OpenViking 的语义与向量处理完成，才计为 `synced`。

### GET /api/experience-sync/status

```json
{
  "enabled": true,
  "target_directory": "viking://resources/agent_knowledge_workspace/input/proven_experiences",
  "counts": {"pending": 2, "synced": 10, "retry": 1, "dead_letter": 0},
  "index_pending": 1,
  "retry": {"due": 0, "deferred": 1, "next_attempt_at": 1800000000.0, "last_attempt_at": null},
  "manual": {"request_id": "experience-scan:...", "operation": "sync", "state": "completed",
             "requested_at": 1800000000.0, "finished_at": 1800000005.0, "error_code": ""},
  "last_scan": 1800000000.0
}
```

| 字段 | 含义 |
| --- | --- |
| `counts.pending` | 等待投递或核对 |
| `counts.retry` | 失败后等待重试 |
| `counts.synced` | 已回读校验且索引完成 |
| `counts.dead_letter` | 超过重试次数，不再自动重试 |
| `index_pending` | 正文已写入、语义或向量处理未确认的条数 |
| `manual` | 最近一次扫描命令；`state` 取 `queued`、`running`、`completed`、`failed`、`cancelled` 等 |
| `last_scan` | 最近一次扫描的 Unix 秒时间 |

时间为 Unix 秒，未发生时为 null。响应不含经验正文或凭证。

### POST /api/experience-sync/trigger

立即受理一次增量扫描，请求体必须为空。

### POST /api/experience-sync/import

受理一次全量导入扫描（不使用水位），请求体必须为空。

### GET /api/experience-sync/jobs/{job_id}/result

扫描或编译命令的结果。

## 错误

| 状态 | detail | 原因 |
| --- | --- | --- |
| 400 | `invalid experience kind or pattern status` 等 | 参数非法，或 trigger / import 携带了请求体 |
| 400 | `invalid experience_sync configuration` 等 | 同步配置不完整或非法 |
| 403 | `TENANT_ADMIN_REQUIRED` | 非租户管理员 |
| 404 | `experience command not found` | 命令不存在 |
| 409 | `SYNC_DISABLED` | `experience_sync.enabled` 为 false |
| 503 | — | 租户 `skills` 模块配置缺失 |

运维说明见 [成功经验同步指南](../guides/16-successful-experience-sync.md)。
