# Context Workspace API v2

所有接口位于 `/internal/agents/context`，使用 `tevt_` 与 `user_id`。
GET 在 Query 传身份，POST 在 JSON Body 传身份，包括只带 ref/session ID 的操作。
主体、Account 和安全规范见 [协议 v2](../agent-integrations/06-protocol-v2.md)。
实现：[路由](../../../teamEvolver/proxy/agent_context.py)、
[主体解析](../../../teamEvolver/integrations/agent_principal.py)、
[状态存储](../../../teamEvolver/integrations/context_workspace.py)。

## 接口

| Method | Path suffix | Fields beyond user_id |
| --- | --- | --- |
| GET | `/describe` | — |
| POST | `/resolve` | `query`, optional `context_session_id`, scopes/budget |
| POST | `/read` | `context_ref`, `level` (`l0/l1/l2`) |
| GET | `/skills` | optional `scope`, `context_session_id` |
| POST | `/remember` | `content`, optional `title`, `category` |
| POST | `/forget` | `context_ref` |
| POST | `/sessions/start` | `external_session_id` |
| POST | `/sessions/append` | `context_session_id`, `event_id`, `sequence`, `role`, `content` |
| POST | `/sessions/commit` | `context_session_id`, optional `used_context_refs` |


`resolve` 返回短期 `context_ref`、分层内容及快照标识；`read` 展开对应层级。
v2 subject 只有 `tenant_id` 与 `user_id`。客户端不能指定 OpenViking URI 或凭证。
`remember/forget` 仅写个人 Memory；团队 Memory 和 Skill 为只读。

新 Session ID 由 tenant、user、外部 Session ID 一起生成。
已有 Session ID 保持不变；迁移只补充主体字段。启动同一个用户的同一外部 Session 是幂等的。
append 的 sequence 从 1 递增，同 event_id/内容重试不重复写入；复用 event_id 改内容返回 409。
commit 提交实际使用的 refs；已提交 Session 再次提交返回 duplicate。

## 示例

```bash
curl --fail "$TEAMEVOLVER_URL/internal/agents/context/describe?user_id=alice"   -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"

curl --fail "$TEAMEVOLVER_URL/internal/agents/context/resolve"   -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"   -H "Content-Type: application/json"   -d '{"user_id":"alice","query":"成本计算方法"}'
```

## 错误与隔离

| HTTP | 原因 |
| --- | --- |
| 401 | 租户凭证缺失或失效 |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` 或请求字段错误 |
| 403 | 非个人 Memory 写入或作用域受限 |
| 404 | ref/session 不存在、过期、撤销，或不属于该主体 |
| 409 | event 顺序或重复内容冲突；部分 Session 归属错误也返回 409 |

持有租户 Key 的调用方可以声明该租户任意 user；服务端仍要求请求中的 user 与资源所有者匹配。
切换 user 不能跨租户读取。缓存、快照、审计和提交均保留 tenant/user 绑定。
v1 只在兼容窗口使用，Schema 不被原地改写。

---

以下保留完整的 V1 Context Workspace API 参考，供兼容窗口内使用。

# Context Workspace API（V1）

## 1. API 实现介绍

Context Workspace API 为 Agent 提供统一的上下文访问接口，包括个人/团队 Memory 和 Skill 的搜索、读取、写入，以及 Context Session 的生命周期管理。所有接口只接受**租户机器凭证**（`tevt_`，Agent 数据面唯一的机器凭证），并且必须提供 `external_subject` 参数进行用户身份解析、以及 `integration_id` 声明调用方身份。

核心设计原则是**不透明引用**：`resolve` 接口返回短生命周期的 `context_ref`（格式 `ctx_<random>`），Agent 通过 `context_ref` 读取内容，永远不会接触到底层 OpenViking URI 或存储凭证。Team Memory 和 Team Skill 为只读，仅个人 Memory 支持 remember/forget 写入。

代码实现：`teamEvolver/proxy/agent_context.py`
状态管理：`teamEvolver/integrations/context_workspace.py`（`ContextStateStore`）

## 2. 接口和参数说明

所有 Context Workspace 接口：

```
Authorization: Bearer <租户机器凭证 tevt_<random>>
Content-Type: application/json
```

### 通用认证说明

每个请求通过 `external_subject`（Query 参数或 JSON Body 字段）标识用户。系统通过 `integration_id + external_subject` 映射到 teamEvolver 用户，未映射返回 `403 SUBJECT_NOT_MAPPED`。

| 凭证 | 格式 | 说明 |
|------|------|------|
| 租户机器凭证 | `tevt_<random>` | Agent 数据面唯一的机器凭证，租户级全权，不做 capability → scope 收敛。调用 Context Workspace 时**必须声明 `integration_id`**：两条 GET 路由（`describe`、`skills`）使用 Query 参数，七条 POST 路由（`resolve`、`read`、`remember`、`forget`、`sessions/start`、`sessions/append`、`sessions/commit`）使用 JSON Body 字段。服务端校验声明的 `integration_id` 必须是当前租户下已注册且状态为 `active` 的 Agent，并以它作为 `context_ref` 签发/校验、Context Session 与审计记录的归属键（**声明即归属**）。未声明返回 `400 INTEGRATION_ID_REQUIRED`；不属于本租户返回 `403 UNKNOWN_INTEGRATION_ID`；已注册但状态不是 `active` 返回 `403 INTEGRATION_DISABLED`。 |

多租户部署下该凭证由控制台在创建租户时签发并可在租户页轮换；单租户部署（未启用 `storage_pg`）下由运维通过 `TEAMEVOLVER_TENANT_TOKEN` 环境变量（或 config.yaml 中的 `tenant.machine_token`）配置唯一一条以 `tevt_` 开头的凭证，服务端将其解析为 `default` 租户。该凭证在单租户部署的机器路径上等同于管理员。

**安全权衡：** 在租户内部不再有按 Agent 的隔离——租户机器凭证可以以该租户任意已注册 Agent 的身份操作，因此能够读取/写入归属其他 Agent 的 ref、Context Session 与快照；又因用户注册表是服务级全局的，`remember`/`forget` 可以作用于任意已映射用户的个人 Memory。跨租户访问仍然不可能：声明其他租户的 `integration_id` 一律返回 403。

---

### GET /internal/agents/context/describe

获取当前用户的 Context 作用域描述、可用操作和预算限制。

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `external_subject` | string | 是 | Agent 侧用户标识 |
| `integration_id` | string | 是 | 声明调用方身份：必须是当前租户下已注册且 `active` 的 Agent；作为 ref、Context Session 与审计记录的归属键 |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
| `integration_id` | string | 是 | 声明即归属：必须是当前租户下已注册且 `active` 的 Agent |

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
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "external_subject": "user-001",
    "integration_id": "my-agent:prod",
    "query": "数据库连接池配置",
    "scopes": ["team_memory", "team_skills"],
    "max_items": 5
  }'

# 2. 读取其中一个条目的完整内容
curl -X POST "http://localhost:52010/internal/agents/context/read" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "integration_id": "my-agent:prod",
    "context_ref": "ctx_abc123...",
    "level": "full"
  }'
```


### Context Session 生命周期示例

```bash
# 1. 开始 Session
CTX_SESS=$(curl -s -X POST "http://localhost:52010/internal/agents/context/sessions/start" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{"external_subject": "user-001", "integration_id": "my-agent:prod", "external_session_id": "sess-001"}' | jq -r '.context_session_id')

# 2. 追加消息
curl -X POST "http://localhost:52010/internal/agents/context/sessions/append" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d "{
    \"integration_id\": \"my-agent:prod\",
    \"context_session_id\": \"$CTX_SESS\",
    \"event_id\": \"evt-1\",
    \"sequence\": 1,
    \"role\": \"user\",
    \"content\": \"帮我看看这个错误\"
  }"

# 3. 提交 Session（含实际使用的 refs）
curl -X POST "http://localhost:52010/internal/agents/context/sessions/commit" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d "{
    \"integration_id\": \"my-agent:prod\",
    \"context_session_id\": \"$CTX_SESS\",
    \"used_context_refs\": [\"ctx_abc123...\", \"ctx_def456...\"]
  }"
```

### 使用租户机器凭证（`tevt_`）声明 integration_id

租户机器凭证调用时必须在每条请求中声明 `integration_id`，GET 路由为 Query 参数，POST 路由为 Body 字段：

```bash
# GET 路由：integration_id 作为 Query 参数
curl "http://localhost:52010/internal/agents/context/describe?external_subject=user-001&integration_id=my-agent:prod" \
  -H "Authorization: Bearer tevt_abcdef1234567890"

# POST 路由：integration_id 作为 Body 字段
curl -X POST "http://localhost:52010/internal/agents/context/resolve" \
  -H "Authorization: Bearer tevt_abcdef1234567890" \
  -H "Content-Type: application/json" \
  -d '{
    "external_subject": "user-001",
    "integration_id": "my-agent:prod",
    "query": "数据库连接池配置",
    "scopes": ["team_memory", "team_skills"],
    "max_items": 5
  }'
```

`my-agent:prod` 必须是该租户下已注册且状态为 `active` 的 Agent，否则返回 `403 UNKNOWN_INTEGRATION_ID` 或 `403 INTEGRATION_DISABLED`；未声明 `integration_id` 返回 `400 INTEGRATION_ID_REQUIRED`。

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 401 | `WORKSPACE_TOKEN_INVALID` | 未提供有效的租户机器凭证（`tevt_`） |
| 403 | `SUBJECT_NOT_MAPPED` | external_subject 未映射到 teamEvolver 用户 |
| 403 | `CONTEXT_SCOPE_FORBIDDEN` | 请求的 scope 不在 Agent 授权范围内 |
| 400 | `INTEGRATION_ID_REQUIRED` | 未声明 integration_id |
| 403 | `UNKNOWN_INTEGRATION_ID` | 声明的 integration_id 不是当前租户下已注册的 Agent |
| 403 | `INTEGRATION_DISABLED` | 声明的 integration_id 已注册但状态不是 active |
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
