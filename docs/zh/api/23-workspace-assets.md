# Workspace 与平台资产 API

控制台「个人与团队资产」通过 Workspace 接口浏览和编辑 OpenViking 中的 Memory、Skill 与 Resource；「平台资产」只读浏览 PostgreSQL 中的任务制品。目录布局见 [存储空间与目录布局](../concepts/09-storage-layout.md)。

代码入口：`teamEvolver/platform_docs/api/workspace.py:create_router`、`teamEvolver/platform_docs/workspace_core.py:_scope_map`、`teamEvolver/platform_docs/api/assets.py:create_router`、`teamEvolver/platform_docs/api/console.py:create_router`。

## 控制台入口

`GET /` 与 `GET /console` 返回构建后的控制台 `index.html`，未构建时返回 `503 console assets are not built`。静态资源挂载在 `/assets`，文档图片挂载在 `/docs-assets`。

## Workspace

### 权限

- 所有接口要求控制台登录（或 Root Key）。`user_id` 缺省为当前用户；操作其他用户的空间要求租户管理员。
- 个人作用域（`personal_*`、`personal_workspace`）本人可写；团队作用域（`team_*`、`team_workspace`）只有租户管理员可写，其他用户写入返回 `403 only administrators can modify team OpenViking scopes`。
- URI 必须位于作用域根目录内，越界返回 403；作用域根目录本身不能修改。
- 请求使用租户 Account 与作用域对应的 OpenViking User，Root Key 留在服务端。

### 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/openviking/workspace/config?user_id=` | 是否启用、部署方式、OpenViking 地址、Account、各作用域的根 URI 与可写性、CLI 是否可用 |
| GET | `/api/openviking/workspace/list?scope=&uri=&user_id=` | 列出目录（最多 2000 项） |
| GET | `/api/openviking/workspace/tree?scope=&uri=&user_id=` | 递归目录树（最多 10000 个节点） |
| GET | `/api/openviking/workspace/content?scope=&uri=&user_id=` | 读取文件，返回内容与内容哈希 |
| GET | `/api/openviking/workspace/level?scope=&uri=&level=&user_id=` | L0 摘要（`l0` / `abstract`）或 L1 概览（`l1` / `overview`） |
| POST | `/api/openviking/workspace/content` | `{"scope", "uri", "content", "mode", "user_id"?}` 写入单个文件，`mode` 为 `replace`（默认）、`create`、`append`，≤2 MiB |
| POST | `/api/openviking/workspace/batch-content` | 条件批量写，见下文 |
| POST | `/api/openviking/workspace/mkdir` | `{"scope", "uri", "user_id"?}` 创建目录 |
| DELETE | `/api/openviking/workspace?scope=&uri=&user_id=` | 删除文件或目录 |
| GET | `/api/openviking/workspace/aggregate?kind=all&user_id=` | 合并个人与团队的 Skill、Memory、Resource 列表，每项标注 `source`（`personal` / `team`）；`kind` 为 `skills`、`memory`、`resources`、`all` |
| POST | `/api/openviking/workspace/cli` | `{"scope", "current_uri", "command", "user_id"?}` 执行 OpenViking CLI |

### 条件批量写

```json
{"user_id": "alice", "changes": [
  {"scope": "personal_memory", "uri": "viking://user/alice/memories/profile.md",
   "original_content": "...", "content": "..."}
]}
```

- 最多 100 个文件，单文件 ≤2 MiB，合计 ≤16 MiB，只允许 Memory 与 Skill 作用域。
- 服务端先读取每个文件的当前内容，与 `original_content` 比较；任一文件已被他人修改则整批返回 `409 workspace files changed since editing began`，不写入任何文件。
- 同一请求中重复的 URI 返回 400。

### CLI

CLI 需要服务器安装 OpenViking CLI（`OPENVIKING_CLI_BIN` 指定路径）。命令前缀 `ov` / `openviking` 可省略；`ls`、`tree` 不带参数时作用于 `current_uri`。

同时是平台管理员与租户管理员的用户可执行任意命令。其他用户只能执行 `abstract`、`find`、`glob`、`grep`、`health`、`ls`、`overview`、`read`、`search`、`session`、`stat`、`status`、`tree`、`version`，不能使用 `--account`、`--user`、`--sudo`，命令中的 `viking://` URI 必须位于所选作用域内，检索范围限定到 `current_uri`。输出上限 512 KiB，超时 300 秒。

### OpenViking 不可用时

OpenViking 不可达时读写返回 503；L0 / L1 返回空内容。

## 平台资产

平台资产只读展示 PostgreSQL ObjectStore 中的不可变任务 payload：接入原文（`ingest`）、Session 分阶段结果（`session`）、Memory Compile 结果（`memory`）。要求租户管理员，只能看到当前租户的对象。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/platform-assets/tree` | 来源、按类别统计与条目列表 |
| GET | `/api/platform-assets/content?uri=` | 预览一个对象，`uri` 形如 `platform://artifacts/<key>`，≤4 MiB |

| 状态 | 原因 |
| --- | --- |
| 400 | `invalid platform asset URI` / `select a platform asset file` |
| 403 | 非租户管理员，或路径未公开 |
| 404 | 对象不存在 |
| 413 | 超过 4 MiB 预览上限 |
| 503 | 存储不可用 |
