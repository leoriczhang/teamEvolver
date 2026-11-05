# Managed Agents API

代码入口：`team_ma/api/__init__.py:create_router`。控制台要求登录或服务 Root Key，并选择 `X-Tenant-Id`。租户管理员可写，普通成员可读取脱敏状态。数据面发布快照只接受当前租户 Key + `user_id`，不使用连接 ID 鉴权。

## 控制台接口

| 方法 | 路径 | 请求与返回 |
| --- | --- | --- |
| GET | `/api/ma/connections` | `after`、`limit`，返回 `connections`、`next_after` |
| GET | `/api/ma/connections/{connection_id}` | 脱敏配置、revision、状态、最近业务/拉取时间与 Session 游标 |
| POST | `/api/ma/connections` | 连接配置与可选 `connection_id`，受理 discover，202 + `job_id` / `status_url` |
| PUT | `/api/ma/connections/{connection_id}` | 配置补丁 + `expected_revision`；`enabled` 控制暂停 / 恢复 |
| POST | `/api/ma/connections/{connection_id}/discover` | `{}`，202 |
| POST | `/api/ma/connections/{connection_id}/collect` | `{session_ids? , from_timestamp? , to_timestamp?}`，202；时间需带时区 |
| POST | `/api/ma/connections/{connection_id}/reconcile` | `{}`，受理 pull 同步、外部效果对账与拥有资源的清理，202 |
| GET | `/api/ma/connections/{connection_id}/resources` | `kind=skill|agent`、`after`、`limit`，成员 / 托管版本与树哈希 |
| PUT | `/api/ma/connections/{connection_id}/resources/{skill_name}/bindings` | `{expected_revision, agent_ids}`，新增关联 revision=0 |
| GET | `/api/ma/connections/{connection_id}/releases` | `limit`，完整 / 部分 release 及回读与错误事实 |

创建与操作支持 `Idempotency-Key`；重用不同请求返回 409。所有外部 HTTP、下载、导入、分析和同步由 Worker 执行。使用返回的统一 Jobs URL 检查结果。

## 连接字段

| 字段 | 说明 |
| --- | --- |
| `provider` | `volcengine-managed-agents` |
| `display_name`、`entry_agent_id`、`environment_id`、`default_user_id` | 必填，1–200 字符 |
| `ma_base_url` | 默认 `https://ark.cn-beijing.volces.com/api/v3`，生产 HTTPS |
| `ma_api_key`、`tenant_token` | 一次明文输入，加密保存，回显只返回配置是否存在 |
| `ma_secret_ref`、`tenant_token_ref` | 可提供本租户已有 Secret Ref；更新省略凭证时保留 |
| `team_evolver_url` | Worker 的 pull 地址，默认 `http://127.0.0.1:52010` |
| `vault_ids`、`replay_vault_ids` | 显式 Vault ID 列表 |
| `user_tag_prefix` | 默认 `te-user:` |
| `poll_interval_seconds`、`sync_interval_seconds` | 默认 60 / 30；范围 15–86400 |
| `replay_enabled`、`replay_environment_id` | 默认关闭；开启后必须指定独立回放 Environment |
| `replay_isolation` | `{vaults?, mcp?, custom_tools?}` 显式能力声明 |
| `runtime_binding` | `{skill_name, paths, working_directory, source?, pre_setup?, replace_setup_script?, bootstrap, ready_file, ready_probe_message?}`；`source` 默认 `embedded`，`skill_mount` 从固定 Skill 挂载逐文件核验 SHA-256 后复制，缩短初始化脚本；前置脚本最多 32 KiB，替换原脚本默认 false；可选只读探测先于业务任务 |

同 MA origin + entry Agent 只能被一个租户托管。配置 CAS 冲突或目标占用返回 409。下载、隔离或固定快照缺失记录为失败，不能自动放宽发布门禁。

## 数据面发布快照

`GET /sync/ma/{connection_id}?user_id=employee` 使用 `Authorization: Bearer <tenant-key>`。返回 `release_id`、`manifest_hash`、`connection_revision`、入口 ID / 固定版本、Environment ID / 配置哈希、`agents`、`skills`、可选 `runtime_tree_sha256`。Skill 中的 `tree_sha256` 是规范化文件树哈希，与 ZIP 字节哈希不同。

响应支持 `ETag` 与 `If-None-Match` / 304，`Cache-Control: private, no-cache`。只有完全应用的 release 可读；同步过程中返回上一完整 release，无完整 release 返回 503。错误或不存在跨租户连接返回 404，不泄露其他租户资源。

## 回放配置

`GET /api/replay/settings` 返回 `revision` 与 `config`。`PUT /api/replay/settings` 接收 `{expected_revision, config}`，其中 `concurrency` 为 1–2、`timeout_seconds` 为 30–1800、`max_interactions` 为 1–20，要求租户管理员。代码入口为 `team_replay/api/settings.py:create_router`。

`GET /api/skill-evolution/settings` 与 PUT 的 `evolve` 支持 `bundle_text_extensions`，用于显式允许 `.mjs/.js/.json` 等文本文件；静态检查仍必须通过。流程与示例见 [MA 接入](../agent-integrations/07-managed-agents.md)。
