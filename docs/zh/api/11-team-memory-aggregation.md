# 团队记忆聚合 API

## 1. API 实现介绍

团队记忆聚合 API（Interface 1）用于将一个 OpenViking Account 下多个 User 的个人记忆，通过 `ov compile` 聚合为 account 共享的团队记忆。产物默认落在 `viking://resources/shared-knowledge/` 下，实际输出目录可配置，account 内全员可检索。

聚合采用「两阶段 + 分层归并」模型，全程不修改 OpenViking 源码：

- **Phase 1（per-user staging）**：Trusted 模式使用 Root Key + User 身份；API-key 模式先用 Admin Key 获取现存用户 Key，再使用每个 User 自己的 Key 读取 Memory、安装 Skill 和运行 compile。产物写入最终目录的同级工作根，例如 `viking://resources/shared-knowledge-staging/<uid>`。
- **Phase 2（tree-reduce 合并）**：以 team 用户身份，将所有 staging 根按 `merge_fan_in`（默认 12，≤15）分批做多级归并，最终合并到 `viking://resources/shared-knowledge/`。分层归并保证每次 compile 源数不超过 16 的硬上限，支持 100+ 用户。

其它特性：Phase 1 并发执行（`phase1_concurrency`）、内容指纹增量跳过（未变更用户复用上次 staging）、失败隔离与断点续跑（单用户失败不影响整体，下次仅重试失败/变更项）。中转和 `_merge` 目录始终位于工作根，不会进入最终团队 Memory 根或污染其 L0/L1 摘要。

代码实现：
- 路由：`teamEvolver/proxy/aggregation_routes.py`（`AggregationMixin`）
- 编排服务：`teamEvolver/aggregation/service.py`（`MemoryAggregationService`）
- compile 调用：`teamEvolver/aggregation/compile_client.py`（`CompileClient`）
- 用户枚举：`teamEvolver/aggregation/sources.py`（`AccountSourceBuilder`）
- 增量状态：`teamEvolver/aggregation/state.py`（`AggregationState`）
- OKF Skill 默认模板：`teamEvolver/aggregation/okf_skill.py`（`DEFAULT_OKF_SKILL_BODY`）

`CompileClient` 直接调用 OpenViking HTTP 接口：通过 `POST /api/v1/skills`
以内联内容安装聚合 Skill，再通过 `POST /bot/v1/compile` 创建任务并轮询
`GET /bot/v1/compile/{task_id}`。teamEvolver 宿主机不需要安装 `ov` CLI，
也不需要访问 OpenViking 容器内的 `/app/.venv/bin/ov` 或共享临时目录。

## 2. 接口和参数说明

可复用执行面 `POST /api/aggregation/users`、`POST /api/aggregation/run` 和 `GET /api/aggregation/status/{task_id}` 不依赖 TeamEvolver Cookie、用户或角色。外部调用在请求体中二选一传入 `root_key`（Trusted 模式）或 `admin_key`（API-key 模式）；凭据不持久化，也不会进入任务状态或响应。TeamEvolver 控制台不传凭据，由已登录管理员身份回退到系统配置的 Trusted Root Key。`runs`、`settings` 和 Skill 编辑仍属于控制台管理面，需要 TeamEvolver 管理员认证。

---

### POST /api/aggregation/users

列出指定 Account 下可聚合的 User（已排除 team 服务用户）。用于控制台「输入 Account → 列出用户 → 勾选」流程。

**认证：** 外部调用无需 TeamEvolver 认证，`root_key` 与 `admin_key` 二选一；控制台管理员可省略并使用系统 Root Key

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `endpoint` | string | 否 | OpenViking HTTP(S) Endpoint；留空则使用 `sharing.viking_endpoint` |
| `account_id` | string | 否 | OpenViking Account ID；留空则使用 `sharing.viking_account` |
| `root_key` | string | 二选一 | Trusted 模式 Root Key |
| `admin_key` | string | 二选一 | 非 Trusted / API-key 模式 Admin Key |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `endpoint` | string | 规范化后的实际 OpenViking Endpoint |
| `account_id` | string | 实际使用的 account |
| `auth_mode` | string | `trusted` 或 `api_key` |
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

**认证：** 外部调用无需 TeamEvolver 认证，`root_key` 与 `admin_key` 二选一；控制台管理员可省略并使用系统 Root Key

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `endpoint` | string | 否 | 本次任务使用的 OpenViking HTTP(S) Endpoint；留空则使用 `sharing.viking_endpoint` |
| `account_id` | string | 否 | 目标 account；留空则使用 `sharing.viking_account` |
| `root_key` | string | 二选一 | Trusted 模式 Root Key |
| `admin_key` | string | 二选一 | 非 Trusted / API-key 模式 Admin Key |
| `target_uri` | string | 否 | 本次任务的最终输出 URI，必须位于 `viking://resources/<path>`；不传则使用全局默认目录 |
| `user_ids` | array[string] | 否 | 参与聚合的用户白名单；不传则聚合该 account 全部可聚合用户 |
| `kinds` | array[string] | 否 | 参与聚合的记忆类别；不传则使用默认类别集合 |
| `mode` | string | 否 | `incremental`（默认，仅重编译变更/失败用户）或 `full`（强制全部重编译） |

**响应（202）：** 初始 Run 对象（`status` 为 `pending`/`running`）。

`target_uri` 是运行级参数，不修改持久化设置。服务会按 Endpoint、Account、认证模式和目标 URI 隔离增量状态。`root_key` 和 `admin_key` 都不会出现在 202 响应、任务列表或状态查询结果中。

API-key 模式要求 OpenViking Admin 用户列表返回完整 `api_key`。如果目标部署启用了 API Key 哈希、只返回 `key_prefix`，任务会在任何 compile 启动前失败；teamEvolver 不会自动 regenerate 用户 Key。

代码入口：`teamEvolver/proxy/aggregation_routes.py` (`api_aggregation_run`)

---

### GET /api/aggregation/status/{task_id}

查询指定任务的实时进度（分组级状态）。

**认证：** 无 TeamEvolver 认证；持有不可猜测的 `task_id` 即可查询对应任务

**路径参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `task_id` | string | 是 | `run` 返回的随机任务 ID；应作为状态查询凭据妥善保管 |

**Run 对象字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `task_id` | string | 任务 ID |
| `endpoint` | string | 本次任务实际使用的 OpenViking Endpoint |
| `account_id` | string | 目标 account |
| `auth_mode` | string | `trusted` 或 `api_key` |
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
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/users" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","admin_key":"<openviking-admin-key>"}'
```

响应示例：

```json
{
  "endpoint": "https://openviking.example.com",
  "account_id": "default",
  "auth_mode": "api_key",
  "users": ["alice", "bob", "chenghan", "zhangpengkun"]
}
```

### 触发聚合（选定用户 + 增量模式）

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","admin_key":"<openviking-admin-key>","target_uri":"viking://resources/engineering-memory","user_ids":["chenghan","zhangpengkun"],"mode":"incremental"}'
```

响应示例（202）：

```json
{
  "task_id": "agg_r4nd0m-capability-token",
  "endpoint": "https://openviking.example.com",
  "account_id": "default",
  "auth_mode": "api_key",
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
curl \
  "http://localhost:52010/api/aggregation/status/agg_r4nd0m-capability-token"
```

响应示例（完成）：

```json
{
  "task_id": "agg_r4nd0m-capability-token",
  "endpoint": "https://openviking.example.com",
  "account_id": "default",
  "auth_mode": "api_key",
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
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","admin_key":"<openviking-admin-key>","target_uri":"viking://resources/engineering-memory","user_ids":["alice","bob"]}'
```

### 使用 Trusted Root Key

外部调用 Trusted 部署时，将 `admin_key` 替换为 `root_key`：

```bash
curl -X POST \
  -H "Content-Type: application/json" \
  "http://localhost:52010/api/aggregation/run" \
  -d '{"endpoint":"https://openviking.example.com","account_id":"default","root_key":"<openviking-root-key>","target_uri":"viking://resources/engineering-memory","user_ids":["alice","bob"]}'
```

控制台调用不提交这两个字段，由 TeamEvolver 管理员会话授权后使用系统配置的 Root Key，默认保持 Trusted 模式。

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
| 401 | `login required` | 未登录却调用 `runs`、`settings` 或 Skill 管理接口 |
| 403 | `team memory aggregation requires an administrator` | 非管理员调用管理接口 |
| 400 | `aggregation users body must be an object` | 用户枚举请求体不是 JSON object |
| 400 | `aggregation run body must be an object` | 运行请求体不是 JSON object |
| 400 | `exactly one of root_key or admin_key is required` | 外部请求未提供凭据 |
| 400 | `root_key and admin_key are mutually exclusive` | 同时提供了两种凭据 |
| 400 | `root_key must be a string` | `root_key` 类型错误 |
| 400 | `admin_key must be a string` | `admin_key` 类型错误 |
| 400 | `trusted root key is not configured` | 控制台 Trusted 路径没有可用的系统 Root Key |
| 400 | `endpoint must be a string` | `endpoint` 不是字符串 |
| 400 | `endpoint is required` | 请求未传 Endpoint，且系统未配置默认 Endpoint |
| 400 | `endpoint must be a valid HTTP(S) URL` | Endpoint 协议或 URL 结构非法 |
| 400 | `api_key mode requires plaintext per-user API keys...` | 用户列表只返回 Key 前缀或缺少用户 Key；未执行 Key 轮换 |
| 400 | `admin_key owner could not be identified...` | 无法从用户列表确认 Admin Key 对应的管理员身份 |
| 400 | `admin list-users reached safety limit...` | 用户数达到 10000 安全上限，结果可能被截断 |
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
| `phase1_concurrency` | 6 | TeamEvolver 服务实例级 compile 总并发上限，多个聚合 Run 共享 |
| `merge_fan_in` | 12 | Phase 2 tree-reduce 扇入宽度（2–15） |
| `compile_runtime_timeout_seconds` | 3000 | 单次 compile 运行超时 |
| `state_dir` | 空（默认 `~/.teamEvolver/aggregation`） | 增量状态与 Skill 内容存储目录；状态按 Endpoint、Account 与目标 URI 隔离 |

配置三处登记：`teamEvolver/config_store/defaults.py`、`teamEvolver/config_store/bridge.py`、`teamEvolver/config.py`。

### 上限与规模

- `ov compile` 单任务源数硬上限为 16，产物数上限为 128。
- Phase 2 采用 tree-reduce 分层归并，保证任意用户规模下每次 compile 源数 ≤ `merge_fan_in`（≤15），已在 120 用户规模下验证不截断、不超限。
- 首次全量聚合耗时随用户数线性增长（每次 compile 约数十秒），日常使用建议用增量模式，仅重编译变更/失败用户。
- Skill 上传和 compile 提交只对连接建立失败做最多 3 次指数退避重试，避免重复提交已被上游接受的 POST；compile 状态轮询是幂等 GET，可对瞬时传输错误重试。

### Compile 容量诊断

支持容量状态接口的 OpenViking 部署可通过鉴权请求查询：

```bash
curl -H "X-API-Key: <openviking-key>" \
  -H "X-OpenViking-Account: <account-id>" \
  -H "X-OpenViking-User: <user-id>" \
  "https://openviking.example.com/bot/v1/compile/status"
```

重点字段：

- `worker_model=in_process_asyncio`：compile 由 VikingBot gateway 进程内任务执行，不存在独立 compile worker 进程。
- `available_execution_slots`：当前空闲执行槽；持续为 0 表示容量已耗尽。
- `running_tasks` / `queued_tasks`：正在执行及等待执行的任务数。
- `queue_wait_seconds`：任务获取执行槽前允许等待的最长时间。

若大量任务在 `queued` 阶段失败且 `available_execution_slots=0`，应优先降低调用方总并发或扩充经过压测的 OV 容量，而不是排查 VLM。当前本地源码部署将 40 个接纳任务、10 个执行槽对应的 queue wait 从 1 小时调整为 4 小时，可覆盖四个最坏执行波次。

### 身份与权限

- 外部调用必须二选一：`root_key` 对应 `trusted`，`admin_key` 对应 `api_key`；服务端不持久化，也不通过任何响应返回凭据。
- 控制台保持兼容：管理员会话可不传凭据，使用 `sharing.viking_team_api_key` 作为 Trusted Root Key。
- `endpoint` 可按请求覆盖且不持久化，只接受不含用户信息、查询参数或片段的 HTTP(S) URL。
- API-key 模式从 Admin 用户列表中读取现存明文 Key；每个用户的 probe、Skill 安装和 compile 只使用该用户自己的 Key。
- 最终 merge 使用 Admin Key 及其实际管理员 User 的 Skill 空间，不使用固定 `team` 身份。
- 用户 Key 只存在于后台 worker 内存，不进入 HTTP 响应、Run、日志、增量状态或持久化配置。
- 开启 API Key 哈希的部署不支持该兼容路径；服务会失败关闭，绝不自动 regenerate 或替换用户 Key。
- `task_id` 使用高熵随机值；无 TeamEvolver 身份的调用方凭该 ID 查询单个任务。任务列表仍只对控制台管理员开放。
- 部署方应通过 HTTPS 或受信网络暴露执行接口，避免 Admin Key 在传输过程中泄露。
- 最终产物写入运行请求的 `target_uri`，未传时回退到 `viking://resources/<shared_knowledge_prefix>/`；中间产物只写入该目标的同级工作根。
