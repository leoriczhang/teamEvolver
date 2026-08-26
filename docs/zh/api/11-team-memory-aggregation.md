# 团队记忆聚合 API

## 1. API 实现介绍

团队记忆聚合 API（Interface 1）用于将一个 OpenViking Account 下多个 User 的个人记忆，通过 `ov compile` 聚合为 account 共享的团队记忆。产物默认落在 `viking://resources/shared-knowledge/` 下，实际输出目录可配置，account 内全员可检索。

聚合采用「两阶段 + 分层归并」模型，全程不修改 OpenViking 源码：

- **Phase 1（per-user staging）**：对每个选中的 User，使用请求传入的 OpenViking Admin Key 运行 compile 并读取该用户的记忆。产物写入最终目录的同级工作根，例如 `viking://resources/shared-knowledge-staging/<uid>`。OKF Skill 先安装到该用户自己的 skills 空间，供同身份读取。
- **Phase 2（tree-reduce 合并）**：以 team 用户身份，将所有 staging 根按 `merge_fan_in`（默认 12，≤15）分批做多级归并，最终合并到 `viking://resources/shared-knowledge/`。分层归并保证每次 compile 源数不超过 16 的硬上限，支持 100+ 用户。

其它特性：Phase 1 并发执行（`phase1_concurrency`）、内容指纹增量跳过（未变更用户复用上次 staging）、失败隔离与断点续跑（单用户失败不影响整体，下次仅重试失败/变更项）。中转和 `_merge` 目录始终位于工作根，不会进入最终团队 Memory 根或污染其 L0/L1 摘要。

代码实现：
- 路由：`teamEvolver/proxy/aggregation_routes.py`（`AggregationMixin`）
- 编排服务：`teamEvolver/aggregation/service.py`（`MemoryAggregationService`）
- compile 调用：`teamEvolver/aggregation/compile_client.py`（`CompileClient`）
- 用户枚举：`teamEvolver/aggregation/sources.py`（`AccountSourceBuilder`）
- 增量状态：`teamEvolver/aggregation/state.py`（`AggregationState`）
- OKF Skill 默认模板：`teamEvolver/aggregation/okf_skill.py`（`DEFAULT_OKF_SKILL_BODY`）

## 2. 接口和参数说明

所有 `/api/aggregation/*` 接口均需控制台**管理员**认证（admin 角色）。用户枚举和聚合运行还要求在请求体中提交 OpenViking Admin Key；该 Key 不持久化，也不会进入任务状态或响应。

---

### POST /api/aggregation/users

列出指定 Account 下可聚合的 User（已排除 team 服务用户）。用于控制台「输入 Account → 列出用户 → 勾选」流程。

**认证：** 控制台 Cookie（管理员）

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `account_id` | string | 否 | OpenViking Account ID；留空则使用 `sharing.viking_account` |
| `admin_key` | string | 是 | OpenViking Admin Key；仅用于本次请求，不持久化、不返回 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `account_id` | string | 实际使用的 account |
| `users` | array[string] | 可聚合的 user_id 列表 |

代码入口：`teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_users`)

---

### GET /api/aggregation/runs

列出最近的聚合任务（按开始时间倒序，最多 20 条）。用于**刷新网页后恢复**正在进行或最近的任务进度。

**认证：** 控制台 Cookie（管理员）

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `runs` | array | 任务列表（Run 对象，见下） |

> 说明：任务状态保存在服务端内存；重启 teamEvolver 后列表清空，进程内任务也不保证继续执行。磁盘上的增量指纹状态不受影响。

代码入口：`teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_runs`)

---

### POST /api/aggregation/run

启动一次后台聚合任务，立即返回 202 与初始 Run 对象。任务在 worker 线程中执行，通过 `status` 接口轮询进度。

**认证：** 控制台 Cookie（管理员）

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `account_id` | string | 否 | 目标 account；留空则使用 `sharing.viking_account` |
| `admin_key` | string | 是 | OpenViking Admin Key；仅在后台任务执行期间保留，不进入 Run 对象 |
| `target_uri` | string | 否 | 本次任务的最终输出 URI，必须位于 `viking://resources/<path>`；不传则使用全局默认目录 |
| `user_ids` | array[string] | 否 | 参与聚合的用户白名单；不传则聚合该 account 全部可聚合用户 |
| `kinds` | array[string] | 否 | 参与聚合的记忆类别；不传则使用默认类别集合 |
| `mode` | string | 否 | `incremental`（默认，仅重编译变更/失败用户）或 `full`（强制全部重编译） |

**响应（202）：** 初始 Run 对象（`status` 为 `pending`/`running`）。

`target_uri` 是运行级参数，不修改持久化设置。服务会为每个目标 URI 派生独立的同级工作目录和增量状态；因此不同输出位置之间不会错误复用 staging 或指纹。`admin_key` 不会出现在 202 响应、任务列表或状态查询结果中。

代码入口：`teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_run`)

---

### GET /api/aggregation/status/{task_id}

查询指定任务的实时进度（分组级状态）。

**认证：** 控制台 Cookie（管理员）

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | string | 是 | 任务 ID（`run` 接口返回的 `task_id`） |

**Run 对象字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | string | 任务 ID |
| `account_id` | string | 目标 account |
| `target_uri` | string | 本次任务规范化后的最终输出 URI |
| `status` | string | `pending`、`running`、`completed`、`failed` |
| `started_at` | number | 开始时间戳 |
| `finished_at` | number\|null | 结束时间戳（未结束为 null） |
| `error` | string | 失败原因（失败时） |
| `groups` | array | 分组结果列表（Group 对象，见下） |

**Group 对象字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `group_key` | string | 分组标识：`stage:<uid>`（用户暂存）、`merge:L<n>:g<i>`（中间归并）、`merge`（最终合并）、`skill:<uid>`（skill 安装失败时） |
| `kind` | string | 类别标记（当前统一为 `(all)`） |
| `target_uri` | string | 该分组的输出 URI |
| `source_count` | integer | 该分组的源数量 |
| `status` | string | `ok`、`skipped`、`failed` |
| `detail` | string | 备注（如 `unchanged (reused staging)`、错误信息等） |

代码入口：`teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_status`)

---

### GET /api/aggregation/okf-skill

读取当前生效的团队记忆聚合 Skill（用户编辑后的内容，或默认模板）。

**认证：** 控制台 Cookie（管理员）

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `skill_name` | string | Skill 名称（默认 `team-memory-okf`） |
| `body` | string | SKILL.md 内容 |
| `editable` | boolean | 是否可编辑（恒为 true） |

代码入口：`teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_okf_skill`)

---

### PUT /api/aggregation/okf-skill

保存用户编辑后的团队记忆聚合 Skill。内容持久化到 `<state_dir>/okf_skill.md`（默认 `~/.teamEvolver/aggregation/okf_skill.md`），**下一次聚合运行时**自动安装到各参与身份的 skills 空间生效。

**认证：** 控制台 Cookie（管理员）

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `body` | string | 是 | 新的 SKILL.md 内容，不可为空 |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 是否保存成功 |
| `body` | string | 保存后的内容 |

代码入口：`teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_okf_skill_save`)

---

### GET /api/aggregation/settings

读取聚合设置和计算后的实际路径。

**认证：** 控制台 Cookie（管理员）

| 响应字段 | 类型 | 说明 |
|----------|------|------|
| `enabled` | boolean | 配置中的聚合启用标记 |
| `shared_knowledge_prefix` | string | 未显式传 `target_uri` 时使用的默认输出目录前缀 |
| `target_root` | string | 最终根，例如 `viking://resources/shared-knowledge` |
| `staging_dir` | string | 工作根后缀 |
| `work_root` | string | 同级工作根，例如 `viking://resources/shared-knowledge-staging` |
| `okf_skill_uri` | string | 聚合 Skill 标识 |
| `key_seed` | string | 兼容保留字段；当前运行时不使用它生成用户 Key |
| `kinds` | array[string] | 显式配置的 Memory 类别；空数组表示使用内置集合 |

---

### POST /api/aggregation/settings

保存可编辑聚合设置。接口通过 `ConfigStore` 持久化配置，随后热重载 OpenViking、DreamCycle 和嵌入式进化集成。

**认证：** 控制台 Cookie（管理员）

| Request Body 字段 | 类型 | 必填 | 说明 |
|-------------------|------|------|------|
| `shared_knowledge_prefix` | string | 否 | 默认输出前缀；仅在运行请求未传 `target_uri` 时使用 |
| `staging_dir` | string | 否 | 同级工作根后缀 |
| `okf_skill_uri` | string | 否 | 聚合 Skill 标识 |
| `kinds` | array[string] | 否 | Memory 类别列表；空项会被过滤 |

响应包含 `GET /api/aggregation/settings` 的全部字段，并增加 `"ok": true`。

## 3. 使用示例

### 列出可聚合用户

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/users" \
  -d '{"account_id":"default","admin_key":"<openviking-admin-key>"}'
```

响应示例：

```json
{
  "account_id": "default",
  "users": ["alice", "bob", "chenghan", "zhangpengkun"]
}
```

### 触发聚合（选定用户 + 增量模式）

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"account_id":"default","admin_key":"<openviking-admin-key>","target_uri":"viking://resources/engineering-memory","user_ids":["chenghan","zhangpengkun"],"mode":"incremental"}'
```

响应示例（202）：

```json
{
  "task_id": "agg_1a03c010043",
  "account_id": "default",
  "target_uri": "viking://resources/engineering-memory",
  "status": "running",
  "started_at": 1756100000.0,
  "finished_at": null,
  "error": "",
  "groups": []
}
```

### 轮询任务进度

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/status/agg_1a03c010043"
```

响应示例（完成）：

```json
{
  "task_id": "agg_1a03c010043",
  "account_id": "default",
  "target_uri": "viking://resources/engineering-memory",
  "status": "completed",
  "groups": [
    {"group_key": "stage:chenghan", "kind": "(all)", "target_uri": "viking://resources/engineering-memory-staging/chenghan", "source_count": 1, "status": "ok", "detail": ""},
    {"group_key": "stage:zhangpengkun", "kind": "(all)", "target_uri": "viking://resources/engineering-memory-staging/zhangpengkun", "source_count": 8, "status": "ok", "detail": ""},
    {"group_key": "merge", "kind": "(all)", "target_uri": "viking://resources/engineering-memory", "source_count": 2, "status": "ok", "detail": "merged"}
  ]
}
```

### 恢复刷新前的任务

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/runs"
```

### 读取 / 编辑团队记忆聚合 Skill

```bash
# 读取当前 Skill
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/okf-skill"

# 保存编辑后的 Skill
curl -X PUT -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/okf-skill" \
  -d '{"body": "---\nname: team-memory-okf\n...\n---\n# ..."}'
```

### 指定本次输出 URI

`POST /api/aggregation/run` 可直接指定完整 URI，不需要先修改全局设置：

```bash
curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"account_id":"default","target_uri":"viking://resources/engineering-memory","user_ids":["alice","bob"]}'
```

### 查看并修改默认输出目录

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/settings"

curl -X POST -b "teamEvolver_console_session=<token>" \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/settings" \
  -d '{"shared_knowledge_prefix": "engineering-memory"}'
```

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 401 | `login required` | 未登录 |
| 403 | `team memory aggregation requires an administrator` | 非管理员访问 |
| 400 | `aggregation users body must be an object` | 用户枚举请求体不是 JSON object |
| 400 | `aggregation run body must be an object` | 运行请求体不是 JSON object |
| 400 | `admin_key is required` | 未提供有效的 OpenViking Admin Key |
| 400 | `target_uri must be a string` | `target_uri` 不是字符串 |
| 400 | `target_uri must be a valid path under viking://resources/<path>` | URI 非法、指向资源根或不在共享资源命名空间 |
| 400 | （上游错误信息） | OpenViking 不可达或 Admin Key 无权访问 |
| 400 | `skill body must not be empty` | 保存空的 Skill 内容 |
| 400 | `aggregation settings body must be an object` | settings 请求体不是 JSON object |
| 400 | `shared_knowledge_prefix is required` | 输出前缀为空 |
| 400 | `shared_knowledge_prefix must be at most 120 characters` | 输出前缀过长 |
| 404 | `unknown aggregation task` | task_id 不存在 |

### 相关配置

| 配置项（`aggregation.*`） | 默认值 | 说明 |
|------|--------|------|
| `shared_knowledge_prefix` | `shared-knowledge` | 运行请求未传 `target_uri` 时的默认团队记忆目录前缀 |
| `staging_dir` | `staging` | 同级工作根后缀；与前缀组合为 `<prefix>-<staging_dir>` |
| `okf_skill_uri` | `viking://agent/skills/team-memory-okf` | 聚合 Skill 名称来源（取末段作为 skill 名）；输出格式由该 Skill 定义 |
| `kinds` | 空（用内置默认集） | 参与聚合的记忆类别 |
| `max_users_per_batch` | 12 | Phase 1 单次 compile 源数上限（< 16） |
| `phase1_concurrency` | 6 | Phase 1 并发度 |
| `merge_fan_in` | 12 | Phase 2 tree-reduce 扇入宽度（2–15） |
| `compile_runtime_timeout_seconds` | 3000 | 单次 compile 运行超时 |
| `state_dir` | 空（默认 `~/.teamEvolver/aggregation`） | 增量状态与 Skill 内容存储目录；状态按 Account 与目标 URI 隔离 |

配置三处登记：`teamEvolver/config_store/defaults.py`、`teamEvolver/config_store/bridge.py`、`teamEvolver/config.py`。

### 上限与规模

- `ov compile` 单任务源数硬上限为 16，产物数上限为 128。
- Phase 2 采用 tree-reduce 分层归并，保证任意用户规模下每次 compile 源数 ≤ `merge_fan_in`（≤15），已在 120 用户规模下验证不截断、不超限。
- 首次全量聚合耗时随用户数线性增长（每次 compile 约数十秒），日常使用建议用增量模式，仅重编译变更/失败用户。

### 身份与权限

- `admin_key` 是每次用户枚举和聚合运行的必填输入，只在请求及后台 worker 生命周期内使用；服务端不持久化，也不通过任何响应返回。
- 聚合不会从持久化配置读取备用凭据。
- Phase 1 使用 Admin Key 和目标用户身份读取各用户记忆。
- 最终产物写入运行请求的 `target_uri`，未传时回退到 `viking://resources/<shared_knowledge_prefix>/`；中间产物只写入该目标的同级工作根。
