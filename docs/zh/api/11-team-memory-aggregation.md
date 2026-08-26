# 团队记忆聚合 API

## 1. API 实现介绍

团队记忆聚合 API（Interface 1）用于将一个 OpenViking Account 下多个 User 的个人记忆，通过 `ov compile` 聚合为 account 共享的团队记忆。产物默认落在 `viking://resources/shared-knowledge/` 下，实际输出目录可配置，account 内全员可检索。

聚合采用「两阶段 + 分层归并」模型，全程不修改 OpenViking 源码、不切换认证模式：

- **Phase 1（per-user staging）**：对每个选中的 User，以「root key + `X-OpenViking-User: <uid>` header」的身份运行 compile，只读取该用户自己的记忆（合法，无需 ROOT 跨读），产物写入该用户的 staging 根 `viking://resources/shared-knowledge/_staging/<uid>`。OKF Skill 先安装到该用户自己的 skills 空间，供同身份读取。
- **Phase 2（tree-reduce 合并）**：以 team 用户身份，将所有 staging 根按 `merge_fan_in`（默认 12，≤15）分批做多级归并，最终合并到 `viking://resources/shared-knowledge/`。分层归并保证每次 compile 源数不超过 16 的硬上限，支持 100+ 用户。

其它特性：Phase 1 并发执行（`phase1_concurrency`）、内容指纹增量跳过（未变更用户复用上次 staging）、失败隔离与断点续跑（单用户失败不影响整体，下次仅重试失败/变更项）。

代码实现：
- 路由：`teamEvolver/proxy/aggregation_routes.py`（`AggregationMixin`）
- 编排服务：`teamEvolver/aggregation/service.py`（`MemoryAggregationService`）
- compile 调用：`teamEvolver/aggregation/compile_client.py`（`CompileClient`）
- 用户枚举：`teamEvolver/aggregation/sources.py`（`AccountSourceBuilder`）
- 增量状态：`teamEvolver/aggregation/state.py`（`AggregationState`）
- OKF Skill 默认模板：`teamEvolver/aggregation/okf_skill.py`（`DEFAULT_OKF_SKILL_BODY`）

## 2. 接口和参数说明

所有 `/api/aggregation/*` 接口均需控制台**管理员**认证（admin 角色）。聚合链路内部使用 trusted/root 服务身份运行 compile。

---

### GET /api/aggregation/users

列出指定 Account 下可聚合的 User（已排除 team 服务用户）。用于控制台「输入 Account → 列出用户 → 勾选」流程。

**认证：** 控制台 Cookie（管理员）

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `account_id` | string | 否 | OpenViking Account ID；留空则使用配置中的 `sharing_viking_account` |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `account_id` | string | 实际使用的 account |
| `users` | array[string] | 可聚合的 user_id 列表 |

代码入口：`teamEvolver/proxy/aggregation_routes.py:51` (`api_aggregation_users`)

---

### GET /api/aggregation/runs

列出最近的聚合任务（按开始时间倒序，最多 20 条）。用于**刷新网页后恢复**正在进行或最近的任务进度。

**认证：** 控制台 Cookie（管理员）

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `runs` | array | 任务列表（Run 对象，见下） |

> 说明：任务状态保存在服务端内存，重启 teamEvolver 进程后列表清空（正在跑的 compile 由 OpenViking 侧继续，但本地进度不再跟踪）。

代码入口：`teamEvolver/proxy/aggregation_routes.py:69` (`api_aggregation_runs`)

---

### POST /api/aggregation/run

启动一次后台聚合任务，立即返回 202 与初始 Run 对象。任务在 worker 线程中执行，通过 `status` 接口轮询进度。

**认证：** 控制台 Cookie（管理员）

**Request Body：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `account_id` | string | 否 | 目标 account；留空则使用配置中的 `sharing_viking_account` |
| `user_ids` | array[string] | 否 | 参与聚合的用户白名单；不传则聚合该 account 全部可聚合用户 |
| `kinds` | array[string] | 否 | 参与聚合的记忆类别；不传则使用默认类别集合 |
| `mode` | string | 否 | `incremental`（默认，仅重编译变更/失败用户）或 `full`（强制全部重编译） |

**响应（202）：** 初始 Run 对象（`status` 为 `pending`/`running`）。

代码入口：`teamEvolver/proxy/aggregation_routes.py:75` (`api_aggregation_run`)

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

代码入口：`teamEvolver/proxy/aggregation_routes.py:107` (`api_aggregation_status`)

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

代码入口：`teamEvolver/proxy/aggregation_routes.py:116` (`api_aggregation_okf_skill`)

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

代码入口：`teamEvolver/proxy/aggregation_routes.py:129` (`api_aggregation_okf_skill_save`)

## 3. 使用示例

### 列出可聚合用户

```bash
curl -b "teamEvolver_console_session=<token>" \
  "http://localhost:52010/api/aggregation/users?account_id=default"
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
  -d '{"account_id": "default", "user_ids": ["chenghan", "zhangpengkun"], "mode": "incremental"}'
```

响应示例（202）：

```json
{
  "task_id": "agg_1a03c010043",
  "account_id": "default",
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
  "status": "completed",
  "groups": [
    {"group_key": "stage:chenghan", "kind": "(all)", "target_uri": "viking://resources/shared-knowledge/_staging/chenghan", "source_count": 1, "status": "ok", "detail": ""},
    {"group_key": "stage:zhangpengkun", "kind": "(all)", "target_uri": "viking://resources/shared-knowledge/_staging/zhangpengkun", "source_count": 8, "status": "ok", "detail": ""},
    {"group_key": "merge", "kind": "(all)", "target_uri": "viking://resources/shared-knowledge", "source_count": 2, "status": "ok", "detail": "merged"}
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

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 401 | `login required` | 未登录 |
| 403 | `team memory aggregation requires an administrator` | 非管理员访问 |
| 400 | `account_id is required` | 未提供 account 且无默认配置 |
| 400 | （上游错误信息） | 列出用户失败（如 OpenViking 不可达、缺少 root/trusted key） |
| 400 | `skill body must not be empty` | 保存空的 Skill 内容 |
| 404 | `unknown aggregation task` | task_id 不存在 |

### 相关配置

| 配置项（`aggregation.*`） | 默认值 | 说明 |
|------|--------|------|
| `shared_knowledge_prefix` | `shared-knowledge` | 团队记忆产物根目录前缀（在 `viking://resources/` 下） |
| `staging_dir` | `_staging` | Phase 1 暂存子目录 |
| `okf_skill_uri` | `viking://agent/skills/team-memory-okf` | 聚合 Skill 名称来源（取末段作为 skill 名）；输出格式由该 Skill 定义 |
| `kinds` | 空（用内置默认集） | 参与聚合的记忆类别 |
| `max_users_per_batch` | 12 | Phase 1 单次 compile 源数上限（< 16） |
| `phase1_concurrency` | 6 | Phase 1 并发度 |
| `merge_fan_in` | 12 | Phase 2 tree-reduce 扇入宽度（2–15） |
| `compile_runtime_timeout_seconds` | 3000 | 单次 compile 运行超时 |
| `state_dir` | 空（默认 `~/.teamEvolver/aggregation`） | 增量状态与 Skill 内容存储目录 |

配置三处登记：`teamEvolver/config_store/defaults.py`、`teamEvolver/config_store/bridge.py`、`teamEvolver/config.py`。

### 上限与规模

- `ov compile` 单任务源数硬上限为 16，产物数上限为 128。
- Phase 2 采用 tree-reduce 分层归并，保证任意用户规模下每次 compile 源数 ≤ `merge_fan_in`（≤15），已在 120 用户规模下验证不截断、不超限。
- 首次全量聚合耗时随用户数线性增长（每次 compile 约数十秒），日常使用建议用增量模式，仅重编译变更/失败用户。

### 身份与权限

- 聚合需要 trusted 服务身份。默认直接复用管理员配置的 OpenViking Key（兼容存储在 `sharing.viking_team_api_key`）；`aggregation.root_api_key` 仅作为高级覆盖项。
- Phase 1 以「service/admin key + 用户 header」模拟目标用户身份，读取各用户自己的记忆。
- 产物写入 `viking://resources/`（account 共享、任意角色可写），无需 ROOT。
