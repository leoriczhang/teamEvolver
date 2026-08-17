# Context Workspace API

## 1. API 实现介绍

Context Workspace API 为 Agent 提供统一的上下文访问接口，包括个人/团队 Memory 和 Skill 的搜索、读取、写入，以及 Context Session 的生命周期管理。所有接口使用 Agent 访问令牌认证，并且必须提供 `external_subject` 参数进行用户身份解析。

核心设计原则是**不透明引用**：`resolve` 接口返回短生命周期的 `context_ref`（格式 `ctx_<random>`），Agent 通过 `context_ref` 读取内容，永远不会接触到底层 OpenViking URI 或存储凭证。Team Memory 和 Team Skill 为只读，仅个人 Memory 支持 remember/forget 写入。

代码实现：`teamEvolver/proxy/agent_context.py`
状态管理：`teamEvolver/integrations/context_workspace.py`（`ContextStateStore`）

## 2. 接口和参数说明

所有 Context Workspace 接口：

```
Authorization: Bearer <agent_access_token>
Content-Type: application/json
```

### 通用认证说明

每个请求通过 `external_subject`（Query 参数或 JSON Body 字段）标识用户。系统通过 `integration_id + external_subject` 映射到 teamEvolver 用户，未映射返回 `403 SUBJECT_NOT_MAPPED`。

---

### GET /internal/agents/context/describe

获取当前用户的 Context 作用域描述、可用操作和预算限制。

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `external_subject` | string | 是 | Agent 侧用户标识 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `protocol_version` | string | 协议版本 `1.0` |
| `integration_id` | string | 集成 ID |
| `subject.user_id` | string | 解析后的 teamEvolver 用户 ID |
| `scopes` | object | 各作用域配置 |
| `scopes.<scope>.kind` | string | 类型：`memory` 或 `skill` |
| `scopes.<scope>.space` | string | 空间：`personal` 或 `team` |
| `scopes.<scope>.operations` | array[string] | 允许的操作列表 |
| `budgets.max_items` | integer | 单次 resolve 最大条目数（50） |
| `budgets.max_chars` | integer | 单次 resolve 最大字符数（100,000） |
| `budgets.max_skill_bytes` | integer | Skill bundle 最大字节数（500,000） |

**Scope 列表：** `personal_memory`、`team_memory`、`personal_skills`、`team_skills`

---

### POST /internal/agents/context/resolve

根据查询语句搜索相关上下文条目，返回不透明 `context_ref` 列表。

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `external_subject` | string | 是 | 用户标识 |
| `query` | string | 是 | 查询文本，最长 8,000 字符 |
| `scopes` | array[string] | 否 | 搜索范围，默认全部四个 scope |
| `max_items` | integer | 否 | 最大返回条目数，1-50，默认 12 |
| `max_chars` | integer | 否 | 最大返回字符数，500-100,000，默认 16,000 |
| `context_session_id` | string | 否 | 关联的 Context Session ID |
| `integration_id` | string | 否 | 必须与 token 绑定 ID 一致 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `schema_version` | string | `teamevolver.context-result.v1` |
| `subject` | object | 主体信息 |
| `snapshot_id` | string | 上下文快照 ID（`ctxsnap_<hash>`） |
| `items` | array | 上下文条目列表 |
| `items[].context_ref` | string | 不透明引用，用于后续 read 调用 |
| `items[].scope` | string | 所属 scope |
| `items[].kind` | string | `memory` 或 `skill` |
| `items[].title` | string | 条目标题 |
| `items[].l0` | string | 摘要内容 |
| `items[].l1` | string | 概览内容 |
| `items[].version` | string | 版本标识 |
| `items[].content_hash` | string | 内容 SHA-256 |
| `items[].selected` | boolean | 是否被选中（可能因技能去重被 shadow） |
| `items[].qualified_skill_id` | string | 技能限定 ID（仅 kind=skill），格式 `team:<name>` 或 `personal:<name>` |
| `receipts` | array | 凭证列表（含 context_ref 和元数据） |
| `warnings` | array | 警告信息（如 DUPLICATE_SKILL） |
| `budget` | object | 预算使用情况 |
| `skills_etag` | string | 技能列表 ETag |

**注意：** 跨 scope 结果按 scope 交错返回，确保单个 scope 不会耗尽预算。同一名称+描述的个人/团队 Skill 会去重，较新版本保留，另一版本标记为 `selected: false` 并附带 `shadowed_by`。

---

### POST /internal/agents/context/read

读取指定 `context_ref` 的内容。

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `context_ref` | string | 是 | resolve 返回的不透明引用 |
| `level` | string | 否 | 内容层级：`l0`（摘要）、`l1`（概览，默认）、`l2`、`full`（完整内容） |

**响应字段（memory 或 level!=full 的 skill）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `context_ref` | string | 引用 ID |
| `scope` | string | 所属 scope |
| `kind` | string | `memory` 或 `skill` |
| `level` | string | 返回的内容层级 |
| `content` | string | 文本内容 |

**响应字段（kind=skill 且 level=full）：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `bundle` | object | Skill 文件包，key 为相对路径，value 为文件内容 |

**content_ref 有效期：** 默认 900 秒（15 分钟），过期后返回 `404 CONTEXT_REF_INVALID`。Ref 仅可由同一 integration 和用户使用。

---

### GET /internal/agents/context/skills

获取技能清单（不经过语义搜索，直接列目录）。

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `external_subject` | string | 是 | 用户标识 |
| `scope` | string | 否 | `personal`、`team`、`all`（默认） |
| `context_session_id` | string | 否 | 关联 Context Session ID |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `skills` | array | 技能列表 |
| `skills[].qualified_skill_id` | string | 限定 ID（`team:<name>`/`personal:<name>`） |
| `skills[].name` | string | 技能名称 |
| `skills[].scope` | string | 所属 scope |
| `skills[].context_ref` | string | 可用于 read 的不透明引用 |
| `snapshot_id` | string | 快照 ID |
| `etag` | string | 列表 ETag |

---

### POST /internal/agents/context/remember

写入个人 Memory。仅限 `personal_memory` scope。

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `external_subject` | string | 是 | 用户标识 |
| `content` | string | 是 | Memory 内容，最大 128KB |
| `category` | string | 否 | 分类，默认 `agent`，仅允许字母数字下划线点横 |
| `idempotency_key` | string | 否 | 幂等键，默认基于 content 哈希 |
| `context_session_id` | string | 否 | 关联 Context Session ID |
| `integration_id` | string | 否 | 必须与 token 一致 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `remembered` | boolean | 是否成功写入 |
| `context_ref` | string | 新创建 Memory 的引用 |
| `receipt` | object | 凭证信息 |

---

### POST /internal/agents/context/forget

删除个人 Memory。仅限 `personal_memory` scope 的 ref。

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `context_ref` | string | 是 | 要删除的 Memory 引用 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `forgotten` | boolean | 是否成功删除 |

---

### POST /internal/agents/context/sessions/start

开始一个新的 Context Session，用于后续 append 事件和 commit 时上报使用情况。

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `external_subject` | string | 是 | 用户标识 |
| `external_session_id` | string | 是 | Agent 侧 Session ID（幂等键） |
| `integration_id` | string | 否 | 必须与 token 一致 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `context_session_id` | string | Context Session ID（`ctxs_<hash>`） |
| `created` | boolean | 是否新创建（false 表示已存在） |

Context Session 基于 `agent_id + external_session_id` 幂等，重复 start 返回同一个 ID。

---

### POST /internal/agents/context/sessions/append

向 Context Session 追加一条消息事件。事件按 sequence 编号有序追加。

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `context_session_id` | string | 是 | Context Session ID |
| `event_id` | string | 是 | 事件唯一 ID（幂等键） |
| `sequence` | integer | 是 | 事件序号，必须严格递增（从 1 开始） |
| `role` | string | 是 | 消息角色：`user`、`assistant`、`system`、`tool` |
| `content` | string | 是 | 消息内容，最大 128KB |
| `created_at` | string | 否 | ISO8601 时间戳 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `appended` | boolean | 是否成功追加 |
| `duplicate` | boolean | 是否为重复事件（相同 event_id + 相同内容） |
| `sequence` | integer | 事件序号 |

**注意：** sequence 必须严格连续递增，不按序返回 409 错误。重复 event_id 但内容不同也返回 409。已 commit 的 session 不可追加。

---

### POST /internal/agents/context/sessions/commit

提交 Context Session，上报实际使用的 context_refs，触发 OpenViking session commit。

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `context_session_id` | string | 是 | Context Session ID |
| `used_context_refs` | array[string] | 否 | 实际读取/注入的 context_ref 列表，最多 200 个 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `committed` | boolean | 是否成功提交 |
| `duplicate` | boolean | 是否重复提交（已 committed 返回 true） |
| `result_hash` | string | Commit 结果哈希 |
| `usage` | object | 使用上报统计 |
| `usage.contexts` | integer | 上报的 Memory 上下文数量 |
| `usage.skills` | integer | 上报的 Skill 数量 |
| `usage.submitted` | integer | 本次提交的使用记录数 |
| `usage.skipped` | integer | 跳过重试的记录数 |

**幂等性：** 已提交的 session 重复 commit 返回 `duplicate: true`，不会重复上报 OpenViking usage。

## 3. 使用示例

### 搜索上下文并读取

```bash
# 1. 解析上下文
curl -X POST "http://localhost:52010/internal/agents/context/resolve" \
  -H "Authorization: Bearer tev1_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "external_subject": "user-001",
    "query": "数据库连接池配置",
    "scopes": ["team_memory", "team_skills"],
    "max_items": 5
  }'

# 2. 读取其中一个条目的完整内容
curl -X POST "http://localhost:52010/internal/agents/context/read" \
  -H "Authorization: Bearer tev1_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "context_ref": "ctx_abc123...",
    "level": "full"
  }'
```

### Context Session 完整生命周期

```bash
# 1. 开始 Session
CTX_SESS=$(curl -s -X POST "http://localhost:52010/internal/agents/context/sessions/start" \
  -H "Authorization: Bearer tev1_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{"external_subject": "user-001", "external_session_id": "sess-001"}' | jq -r '.context_session_id')

# 2. 追加消息
curl -X POST "http://localhost:52010/internal/agents/context/sessions/append" \
  -H "Authorization: Bearer tev1_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d "{
    \"context_session_id\": \"$CTX_SESS\",
    \"event_id\": \"evt-1\",
    \"sequence\": 1,
    \"role\": \"user\",
    \"content\": \"帮我看看这个错误\"
  }"

# 3. 提交 Session（含实际使用的 refs）
curl -X POST "http://localhost:52010/internal/agents/context/sessions/commit" \
  -H "Authorization: Bearer tev1_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d "{
    \"context_session_id\": \"$CTX_SESS\",
    \"used_context_refs\": [\"ctx_abc123...\", \"ctx_def456...\"]
  }"
```

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 401 | `WORKSPACE_TOKEN_INVALID` | 访问令牌无效或缺少所需 scope |
| 403 | `SUBJECT_NOT_MAPPED` | external_subject 未映射到 teamEvolver 用户 |
| 403 | `CONTEXT_SCOPE_FORBIDDEN` | 请求的 scope 不在 Agent 授权范围内 |
| 403 | `integration_id does not match workspace token` | body 中 integration_id 与 token 不一致 |
| 400 | `body must be an object` | 请求体不是 JSON 对象 |
| 400 | `invalid context query` | query 为空或超过 8000 字符 |
| 400 | `unsupported content level` | level 不是 l0/l1/l2/full |
| 400 | `invalid memory content` | remember 内容为空或超过 128KB |
| 400 | `external_session_id is required` | sessions/start 缺少 external_session_id |
| 400 | `invalid context event` | sessions/append 参数无效（role/event_id/content） |
| 400 | `used_context_refs must be a list` | commit 中 used_context_refs 不是数组 |
| 400 | `unsupported skill scope` | skills 接口 scope 参数无效 |
| 404 | `CONTEXT_REF_INVALID` | context_ref 无效、过期或不属于该 integration |
| 404 | `context session not found` | context_session_id 不存在 |
| 409 | `context event sequence must be N, got M` | append 序号不连续 |
| 409 | `event id was reused with a different payload` | 相同 event_id 但内容不同 |
| 409 | `context session is already committed` | 向已 commit 的 session 追加事件 |
| 409 | `used context reference is invalid for this session` | used_context_refs 中包含不属于该 session 的 ref |
| 413 | `context event is too large` | append 内容超过 128KB |
