# Session 查询 API

查询 PostgreSQL 中的正式 Session（`sessions` 表）。正式 Session 只在 normalize、analyze、archive 三个阶段全部成功后写入，`decision` 为 `valuable` 或 `skipped`。每条记录同时提供业务时间 `business_time`（来自上报或上游）和入库时间 `ingested_at`。

代码入口：`session_ingestion/api/console.py:create_router`、`session_ingestion/queries.py:SessionQueries`。

所有接口要求租户管理员（控制台登录或 Root Key）。

## GET /api/sessions

Keyset 分页列表，按 `business_time` 倒序。

| 参数 | 说明 |
| --- | --- |
| `limit` | 默认 50，最大 200 |
| `before` | 上一页最后一条的 `business_time`，取更早的记录 |

```json
{"sessions": [{"session_id": "session_...", "source": "push", "external_id": "task-001", "user_id": "alice",
               "decision": "valuable", "judge": {}, "business_time": "...", "ingested_at": "..."}],
 "count": 1}
```

## GET /api/sessions/page

控制台列表，按 `ingested_at` 倒序，支持过滤。

| 参数 | 说明 |
| --- | --- |
| `limit` | 1–200，默认 20 |
| `offset` | ≥0 |
| `search` | 匹配 `session_id` 或元数据 |
| `decision` | `valuable` / `skipped` |
| `skill` | 匹配元数据中的 Skill 名称 |
| `start` / `end` | `business_time` 范围（ISO 8601） |

```json
{"sessions": [{"session_id": "...", "user_id": "alice", "source": "push", "decision": "valuable",
               "judge": {}, "meta": {}, "title": "...", "user_alias": "alice", "status": "archived",
               "timestamp": "...", "business_time": "...", "ingested_at": "..."}],
 "total": 120, "limit": 20, "offset": 0, "has_more": true}
```

## GET /api/sessions/analysis

按 `decision` 统计：

```json
{"stats": {"total": 120, "decisions": {"valuable": 80, "skipped": 40}, "modes": {"model": 120}}}
```

## GET /api/sessions/export

| 参数 | 说明 |
| --- | --- |
| `format` | `json`（默认）或 `csv` |
| `ids` | 逗号分隔的 `session_id`，为空导出过滤结果 |
| `search` / `decision` | 同 `/api/sessions/page` |

单次最多导出 200 条，响应头 `X-Export-Count` 为实际条数。CSV 列为 `session_id, user_id, decision, business_time, ingested_at`。

## GET /api/sessions/{session_id}

单条正式 Session 的元数据，含 `ingestion_id`、`account_id`、`content_ref`、`content_hash`、`judge`、`filter_audit`、`consumption`、`meta`、`business_time`、`ingested_at`。不存在返回 404。

## GET /api/sessions/{session_id}/content

读取归档后的完整 Session 内容（`content_ref` 指向的不可变 payload），附加 `meta`、`judge`、`summary`（`title`、`user_alias`、`status`、`num_turns`）和两个时间字段。

## GET /api/sessions/{session_id}/stages

各处理阶段的结果：

```json
{"stages": [
  {"stage": "normalize", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "analyze", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "trace_analyze", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "archive", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""}
]}
```

## 进化历史

`GET /history?session_id=&limit=20` 返回 Skill 进化周期记录 `{"reachable": true, "cycles": [...]}`，可按 Session 过滤。接受控制台登录、Root Key 与租户 Key。代码入口：`team_skills/api/candidates.py:create_router`。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | `limit` / `offset` 越界，或 `format` 不是 `json` / `csv` |
| 403 | 非租户管理员 |
| 404 | `Session not found` |
