# Agent Context API

Agent Context 为运行时提供个人与团队的 Memory、Skill、Resource 检索、读取和个人 Memory 写入，并把 Agent 的会话事件写入 OpenViking Session。所有端点使用租户 Key + `user_id`，前缀 `/internal/agents/context`。

代码入口：`teamEvolver/context/api/__init__.py:create_router`；引用与快照存储：`teamEvolver/context/repository.py:ContextRepository`。

## 身份与作用域

每个请求由 `teamEvolver/security/agent_principal.py:resolve_agent_principal` 解析为 `(tenant_id, account_id, user_id)`：

- 必须使用 `Authorization: Bearer tevt_...`，否则返回 `401 TENANT_TOKEN_REQUIRED`。
- `user_id` 在 GET 请求中为 Query 参数，在 POST 请求中为 Body 字段。
- OpenViking Account 来自租户有效配置 `sharing_viking_account`，未配置返回 `503 TENANT_ACCOUNT_REQUIRED`。

六个作用域及根 URI 见 [存储空间与目录布局](../concepts/09-storage-layout.md#作用域)：

| 作用域 | 操作 |
| --- | --- |
| `personal_memory` | resolve、read、remember、forget |
| `team_memory` | resolve、read |
| `personal_skills` | resolve、read |
| `team_skills` | resolve、read |
| `personal_resources` | resolve、read |
| `team_resources` | resolve、read |

服务端向 OpenViking 发送请求时使用租户 Account 和作用域对应的 OpenViking User，Root Key 留在服务端。

## 引用与快照

`resolve`、`skills`、`remember` 为每个条目签发 `context_ref`（形如 `ctx_...`）和 receipt。引用绑定 `(tenant_id, user_id)`，15 分钟后过期，`forget` 后撤销。每次 `resolve` 和 `skills` 生成一个 `snapshot_id`（`ctxsnap_...`），记录本次返回的条目。Agent 上报 Session 时在 `context_usage` 中引用这些 ID，服务端据此校验使用记录，见 [Session 上报](./03-session-ingest.md#context_usage)。

## 请求上限

| 项目 | 上限 |
| --- | --- |
| 请求体 | 256 KiB |
| `resolve.query` | 1–8000 字符 |
| `resolve.max_items` | 1–50，默认 12 |
| `resolve.max_chars` | 500–100000，默认 16000 |
| `remember.content` | 128 KiB |
| `sessions/append.content` | 128 KiB |
| `read` 返回内容 | 500 KiB；Skill bundle 最多 100 个文件 |
| `external_session_id` | 1000 字符 |
| `event_id` | 200 字符 |

## 端点

### GET /internal/agents/context/describe

Query：`user_id`。返回协议版本、主体、作用域与预算：

```json
{
  "protocol_version": "2.0",
  "subject": {"tenant_id": "demo", "user_id": "alice"},
  "scopes": {"personal_memory": {"kind": "memory", "space": "personal", "operations": ["resolve", "read", "remember", "forget"]}},
  "budgets": {"max_items": 50, "max_chars": 100000, "max_skill_bytes": 500000}
}
```

### POST /internal/agents/context/resolve

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 用户 ID |
| `query` | 是 | 检索文本 |
| `scopes` | 否 | 作用域列表，默认全部六个；出现未知作用域返回 `403 CONTEXT_SCOPE_FORBIDDEN` |
| `max_items` | 否 | 返回条数上限 |
| `max_chars` | 否 | `l0` + `l1` 总字符预算 |
| `context_session_id` | 否 | 关联的 Context Session |

服务端在各作用域并行调用 OpenViking 检索，按作用域轮转合并，直到达到条数或字符预算。Skill 条目归并到 Skill 根目录；同名同描述的 Skill 在个人与团队之间只保留修改时间较新的一份，时间相同时个人优先。

```json
{
  "schema_version": "teamevolver.context-result",
  "snapshot_id": "ctxsnap_...",
  "items": [{
    "context_ref": "ctx_...", "receipt": {}, "scope": "team_skills", "kind": "skill",
    "title": "cost-report", "l0": "...", "l1": "...", "skill_metadata": {"name": "cost-report", "description": "..."},
    "content_hash": "...", "selected": true, "path_alias": "team_skills:1a2b3c4d5e6f"
  }],
  "subject": {"tenant_id": "demo", "user_id": "alice"},
  "receipts": [],
  "warnings": [],
  "skills_etag": "...",
  "budget": {"max_items": 12, "max_chars": 16000, "used_items": 1, "used_chars": 820, "truncated": false}
}
```

结果 Schema 见 [agent-context-result-v2.schema.json](../../schemas/agent-context-result-v2.schema.json)，请求 Schema 见 [agent-context-request-v2.schema.json](../../schemas/agent-context-request-v2.schema.json)。

### POST /internal/agents/context/read

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 用户 ID |
| `context_ref` | 是 | 来自 resolve / skills / remember 的引用 |
| `level` | 否 | `l0`、`l1`（默认）、`l2`、`full` |

`l0` 读取摘要，`l1` 读取概览，`l2` 与 `full` 读取正文。Skill 引用使用 `full` 时返回整个 bundle：`{"bundle": {"SKILL.md": "...", "scripts/run.py": "..."}}`。引用不存在、过期或已撤销返回 `404 CONTEXT_REF_INVALID`。

### GET /internal/agents/context/skills

Query：`user_id`、`scope`（`personal`、`team`、`all`，默认 `all`）、`context_session_id`。列出当前用户可见的 Skill 及其引用：

```json
{
  "skills": [{"name": "cost-report", "description": "...", "modified_at": "...", "scope": "team_skills",
              "context_ref": "ctx_...", "receipt": {}, "qualified_skill_id": "team:cost-report"}],
  "snapshot_id": "ctxsnap_...",
  "etag": "..."
}
```

团队 Skill 的权威版本在 PostgreSQL，通过 [Skill 拉取](./06-skill-sync.md) 获取完整 bundle；本端点列出 OpenViking 中的 Skill 目录，用于检索与引用。

### POST /internal/agents/context/remember

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 用户 ID |
| `content` | 是 | Memory 正文，≤128 KiB |
| `idempotency_key` | 否 | 幂等键；缺省以内容计算 |
| `context_session_id` | 否 | 关联的 Context Session |

写入 `personal_memory` 下的 `agent/<hash>.md`。相同幂等键写入同一文件。返回 `{"remembered": true, "context_ref": "...", "receipt": {...}}`。

### POST /internal/agents/context/forget

Body：`user_id`、`context_ref`。只能删除 `personal_memory` 中的条目，其他作用域返回 `403 CONTEXT_SCOPE_FORBIDDEN`。删除后撤销该引用，返回 `{"forgotten": true}`。

### POST /internal/agents/context/sessions/start

Body：`user_id`、`external_session_id`。同一用户的同一 `external_session_id` 映射到同一个 `context_session_id`（`ctxs_...`），并在 OpenViking 中创建对应 Session。返回 `{"context_session_id": "ctxs_...", "created": true}`。

### POST /internal/agents/context/sessions/append

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `user_id` | 是 | 用户 ID |
| `context_session_id` | 是 | `sessions/start` 返回的 ID |
| `event_id` | 是 | 事件 ID，≤200 字符 |
| `sequence` | 是 | 从 1 开始连续递增 |
| `role` | 是 | `user`、`assistant`、`system`、`tool` |
| `content` | 否 | 消息内容，≤128 KiB |
| `created_at` | 否 | 事件时间 |

相同 `event_id` 和相同内容重放返回 `duplicate: true`；同一 `event_id` 携带不同内容、序号不连续或 Session 已提交返回 409。

### POST /internal/agents/context/sessions/commit

Body：`user_id`、`context_session_id`、`used_context_refs`（引用 ID 列表，可选）。服务端把使用过的 Context 作为一条 assistant 消息写入 OpenViking Session，再提交 Session。重复提交返回 `{"committed": true, "duplicate": true}`。

## 错误

| 状态 | detail | 原因 |
| --- | --- | --- |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | `user_id` 缺失或非法 |
| 400 | `invalid context query` | `query` 为空或超过 8000 字符 |
| 400 | `invalid context budget` | `max_items` / `max_chars` 不是整数 |
| 400 | `unsupported content level` / `unsupported skill scope` | 参数取值非法 |
| 400 | `invalid memory content` | `remember.content` 为空或超限 |
| 401 | `TENANT_TOKEN_REQUIRED` | 未使用有效租户 Key |
| 403 | `CONTEXT_SCOPE_FORBIDDEN` | 未知作用域，或对非个人 Memory 执行 forget |
| 404 | `CONTEXT_REF_INVALID` | 引用不存在、过期或已撤销 |
| 409 | `context session not found` 等 | Context Session 不属于当前用户、事件冲突或已提交 |
| 413 | `Context body exceeds 256 KiB` 等 | 请求或读取内容超限 |
| 503 | `TENANT_ACCOUNT_REQUIRED` | 租户未配置 OpenViking Account |
| 4xx/5xx | `OpenViking HTTP <code>` | OpenViking 返回错误（检索 404 视为空结果） |

## 示例

```bash
curl -X POST http://localhost:52010/internal/agents/context/resolve \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"user_id": "alice", "query": "月度成本报告怎么做", "scopes": ["team_skills", "personal_memory"], "max_items": 5}'
```

快照 Schema 见 [agent-context-snapshot-v2.schema.json](../../schemas/agent-context-snapshot-v2.schema.json)。
