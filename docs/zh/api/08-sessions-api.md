# Session 查询 API

## 1. API 实现介绍

Session 查询接口用于查看队中的待处理 Session 和已处理的会话历史。这些接口主要供 Web 控制台使用，需要控制台 Session Cookie 认证（登录后获得）。`/sessions` 和 `/conversations` 端点也可无认证访问（设计为内网部署），但响应数据可能受限。

代码实现：`teamEvolver/proxy/routes.py`（`dashboard_sessions`、`dashboard_conversations`、`dashboard_conversation_detail`）
会话存储：`teamEvolver/session_store.py`

## 2. 接口和参数说明

---

### GET /sessions

列出队列中等待进化处理的 Session。

**认证：** 控制台 Cookie（推荐），也可无认证内网访问

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `limit` | integer | 否 | 每页数量，1-200，默认 20 |
| `offset` | integer | 否 | 分页偏移，默认 0 |
| `refresh` | boolean | 否 | 是否强制刷新缓存，默认 false |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `reachable` | boolean | Session 存储是否可达 |
| `sessions` | array | 当前页 Session 列表 |
| `pending` | integer | 待处理总数 |
| `total` | integer | 队列总数 |
| `limit` | integer | 当前页大小 |
| `offset` | integer | 当前偏移 |
| `has_more` | boolean | 是否有更多数据 |

**缓存：** 队列列表缓存 5 秒。

---

### GET /conversations

列出已处理的会话历史（已归档的对话）。

**认证：** 控制台 Cookie（推荐），也可无认证内网访问

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `limit` | integer | 否 | 每页数量，1-200，默认 20 |
| `offset` | integer | 否 | 分页偏移，默认 0 |
| `refresh` | boolean | 否 | 是否强制刷新缓存，默认 false |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `reachable` | boolean | 存储是否可达 |
| `conversations` | array | 当前页会话列表 |
| `total` | integer | 会话总数 |
| `limit` | integer | 当前页大小 |
| `offset` | integer | 当前偏移 |
| `has_more` | boolean | 是否有更多数据 |
| `reason` | string | 不可达原因 |

**缓存：** 会话列表缓存 15 秒。

---

### GET /conversations/{session_id}

获取单个会话的详细信息。

**认证：** 控制台 Cookie（推荐）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | Session ID |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `meta.title` | string | 会话标题 |
| `meta.user_alias` | string | 用户别名 |
| `meta.status` | string | 处理状态 |
| `meta.num_turns` | integer | 轮次数 |
| `turns_available` | boolean | 是否有轮次详情 |
| `turns_source` | string | 轮次来源（`archive`） |
| `system_prompt` | string | 系统提示词 |
| `injected_skills` | array | 注入的 Skill 列表 |
| `used_skills` | array | 使用的 Skill 列表 |
| `metrics` | object | 会话指标 |
| `turns` | array | 轮次详情 |
| `value_judge` | object | 价值分类结果 |

---

### GET /conversations/{session_id}/process

获取指定会话的进化处理历史（cycle 记录）。

**认证：** 控制台 Cookie（推荐）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_id` | string | 是 | Session ID |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `cycles` | array | 进化周期记录列表 |

---

### POST /conversations/status

批量查询多个 Session 的处理状态。

**认证：** 控制台 Cookie

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `session_ids` | array[string] | 是 | Session ID 列表，最多 500 个 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `reachable` | boolean | 存储是否可达 |
| `statuses` | object | Session ID -> 状态映射 |
| `reason` | string | 不可达原因 |

---

### GET /history

获取进化周期历史记录（从 `evolve_history.jsonl` 或归档 Session 中读取）。

**认证：** 无（内网接口）

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `limit` | integer | 否 | 返回数量，默认 50 |
| `session_id` | string | 否 | 过滤指定 Session 的记录 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `cycles` | array | 进化周期列表 |

## 3. 使用示例

### 查看待处理队列

```bash
curl "http://localhost:52010/sessions?limit=5"
```

响应示例：

```json
{
  "reachable": true,
  "sessions": [
    {
      "session_id": "sess-20240115-001",
      "user_alias": "alice",
      "status": "queued",
      "ingested_at": "2024-01-15T10:30:00Z",
      "value_judge": {"decision": "valuable", "confidence": 0.92}
    }
  ],
  "pending": 3,
  "total": 3,
  "limit": 5,
  "offset": 0,
  "has_more": false
}
```

### 查看会话历史

```bash
curl "http://localhost:52010/conversations?limit=10&offset=0"
```

### 查看会话详情

```bash
curl "http://localhost:52010/conversations/sess-20240115-001"
```

### 查看进化处理历史

```bash
curl "http://localhost:52010/conversations/sess-20240115-001/process"
```

响应示例：

```json
{
  "cycles": [
    {
      "timestamp": "2024-01-15T10:35:00Z",
      "session_ids": ["sess-20240115-001"],
      "sessions": 1,
      "judge": {
        "overall_score": 0.85,
        "decision": "accept",
        "rationale": "Skill optimization improves efficiency"
      },
      "evolutions": [
        {
          "skill_name": "database-debugging",
          "action": "update",
          "version": 4
        }
      ],
      "status": "published"
    }
  ]
}
```

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 400 | `session_id is required` | session_id 参数为空或无效字符 |
| 401 | `login required` | 未登录访问需要控制台认证的接口（`/api/*` 路径） |
| 404 | `session not found` | 指定的 session_id 不存在 |
| 503 | 存储错误信息 | Session 存储不可用 |

### 分页约定

- `limit` 范围 1-200，超出范围自动截断；
- `offset` 从 0 开始；
- `has_more: true` 表示还有更多数据可翻页；
- `total` 为符合条件的总记录数，可用于计算总页数。

### 注意事项

1. 会话详情接口需要先登录控制台（获取 Cookie）。`/sessions` 和 `/conversations` 在非 `/api/` 路径下，为简化内网部署允许无认证访问，但建议在生产环境通过反向代理添加认证层。
2. Session ID 仅允许字母、数字、下划线、点、横杠，其他字符会被替换为 `-`。
3. 处理历史优先从 `evolve_history.jsonl` 读取，文件不存在时回退到归档 Session 数据。
