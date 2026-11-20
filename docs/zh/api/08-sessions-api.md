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

列表提供 `used_skills`、`available_skills` 和对应的 `used_skill_versions`、`available_skill_versions`。版本摘要包含 `name`、`skill_id`、`version`、`agent_id`、`agent_name`、`unknown_reason`，只读取会话归档中的固定版本。实际使用按调用证据统计，可用列表包含已配置的引用，不代表实际调用。控制台直接显示名称与版本，未确认调用的可用项单独显示，超过三项可展开；版本缺失时显示「版本未知」。旧记录按页补充并缓存派生元数据，缓存写入检查源引用、内容哈希与原元数据，不修改归档内容、分析结果或两个时间字段。

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

`skill_versions` 用于详情页的版本表：每项包含 `name`、`skill_id`、`version`、`agent_id`、`agent_name`、`agent_role`（`main/member/unknown`）、`agent_version`、`usage`（`used/configured`）、`unknown_reason` 和归档 `event_ids`。版本只取本会话 Agent 图中的固定引用，名称按同一资源 ID 的受管记录或历史 release 解析；不采用云端当前版本。主、子 Agent 的同名 Skill 分开显示。调用记录重复出现在父轮次和子线程时去重；调用无法唯一对应引用、缺快照或未固定版本时显示未知。此表不声称中途升级而没有分段快照的会话具有逐轮版本记录。

## GET /api/sessions/{session_id}/stages

按标准化、会话切分、会话分析、Trace 分析、归档的处理顺序返回阶段状态。列表不加载阶段产物；会话详情的「进化过程」以可展开时间线展示：

```json
{"stages": [
  {"stage": "normalize", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "split", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "analyze", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "trace_analyze", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""},
  {"stage": "archive", "status": "succeeded", "created_at": "...", "updated_at": "...", "error_code": ""}
]}
```

## GET /api/sessions/{session_id}/stages/{stage}

展开阶段时按需读取其不可变产物，返回 `stage`、`status`、`created_at`、`updated_at`、`error_code`、`detail`（执行记录）与 `result`（阶段结果）。标准化显示轮数与工具调用数；切分显示各会话概况；分析显示摘要、价值判断与评分；Trace 分析显示主、子 Agent 的问题记录；归档显示分析结论与会话概况。

`result_available` 表示实际产物是否可读取；缺少或无法读取产物时保留阶段状态，返回 `result:null` 与 `result_unavailable_reason`。返回结果不包含运行环境凭证、原始对话或完整工具参数。`created_at`、`updated_at` 是阶段记录时间，不能当作执行起止时间计算耗时。会话或阶段不存在返回 404，权限与列表一致。

## 进化历史

`GET /history?session_id=&limit=20` 返回 Skill 进化周期记录 `{"reachable": true, "cycles": [...]}`，可按 Session 过滤。接受控制台登录、Root Key 与租户 Key。代码入口：`team_skills/api/candidates.py:create_router`。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | `limit` / `offset` 越界，或 `format` 不是 `json` / `csv` |
| 403 | 非租户管理员 |
| 404 | `Session not found` |
