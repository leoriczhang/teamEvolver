# Managed Agents API

代码入口：`team_ma/api/__init__.py:create_router`。控制台要求登录或服务 Root Key，并选择 `X-Tenant-Id`。租户管理员可写，普通成员可读取脱敏状态。数据面发布快照只接受当前租户 Key + `user_id`，不使用连接 ID 鉴权。

## 控制台接口

| 方法 | 路径 | 请求与返回 |
| --- | --- | --- |
| GET | `/api/ma/connections` | `after`、`limit`，返回 `connections`、`next_after` |
| GET | `/api/ma/connections/{connection_id}` | 脱敏配置、revision、状态、最近业务/拉取时间与 Session 游标 |
| POST | `/api/ma/connections` | 连接配置与可选 `connection_id`，受理 discover，202 + `job_id` / `status_url` |
| PUT | `/api/ma/connections/{connection_id}` | 配置补丁 + `expected_revision`；`enabled` 控制暂停 / 恢复；开启回放或修改回放依赖配置时自动受理 discover，返回 202 + Job |
| POST | `/api/ma/connections/{connection_id}/discover` | `{}`，202 |
| POST | `/api/ma/connections/{connection_id}/collect` | `{session_ids? , from_timestamp? , to_timestamp?}`，202；时间需带时区 |
| POST | `/api/ma/connections/{connection_id}/reconcile` | `{}`，观察云端最新 Skill / Agent / Environment，外部效果对账与拥有资源清理，202 |
| GET | `/api/ma/connections/{connection_id}/resources` | `kind=skill|agent`、`after`、`limit`，成员 / 托管版本与树哈希 |
| PUT | `/api/ma/connections/{connection_id}/resources/{skill_name}/bindings` | `{expected_revision, agent_ids}`，新增关联 revision=0 |
| GET | `/api/ma/connections/{connection_id}/releases` | `limit`，完整 / 部分 release 及回读与错误事实 |
| GET | `/api/ma/skill-jobs/{job_id}` | `ma.skill` 的实际任务状态、原生版本写入结果与错误 |

创建与操作支持 `Idempotency-Key`；重用不同请求返回 409。云端写入、导入、分析与资源清理由 Worker 执行，返回统一 Jobs URL。Skill 查询在短业务事务前解析云端原生元数据与校验快照。

MA Skill 沿用 [Skill 管理 API](./09-skills-admin.md) 的查看、历史版本、编辑、新建、文件操作、回滚与删除路径，响应带 `skill_backend: "managed-agents"`，版本号为云端原生版本。可用 `connection_id` 查询参数选择连接；跨连接同名时要求显式选择。写入返回 `202`、`job_id`、`status_url`、`result_url`；上传与成员引用回读成功后才返回 published。回滚创建新云端版本，未知上传结果先对账，竞争上传暂停发布。发现 / reconcile 不从 TE 库拉取或回推 Skill。

发现完成后，采集与云端状态观察独立调度。`detail.operation_errors` 按操作保存错误、Job ID 与 `retryable`；一项成功只清除自身错误。临时网络与上游服务错误按配置频率再次调度；校验失败、鉴权失败与版本漂移暂停对应操作，修复后可手动触发。Skill 操作失败不暂停 Session 采集。未发布候选或未挂载 Skill 保留资源身份，云端 404 不复活旧 TE 导入。

## 连接字段

| 字段 | 说明 |
| --- | --- |
| `provider` | `volcengine-managed-agents` |
| `display_name`、`entry_agent_id`、`environment_id` | 必填，1–200 字符 |
| `default_user_id` | 可选，最多 200 字符；明确默认归属。留空时，缺少标签或渠道身份的会话显示“未知提交人” |
| `ma_base_url` | 默认 `https://ark.cn-beijing.volces.com/api/v3`，生产 HTTPS |
| `ma_api_key`、`tenant_token` | 一次明文输入，加密保存，回显只返回配置是否存在 |
| `ma_secret_ref`、`tenant_token_ref` | 可提供本租户已有 Secret Ref；更新省略凭证时保留 |
| `team_evolver_url` | 兼容字段，MA Skills 管理不再使用 TE pull 地址 |
| `vault_ids`、`replay_vault_ids` | 业务 Vault 列表；回放 Vault 可省略，由 Worker 自动创建；已有独立 ID 保持兼容 |
| `user_tag_prefix` | 默认 `te-user:` |
| `poll_interval_seconds`、`sync_interval_seconds` | 默认 60 / 30；范围 15–86400 |
| `replay_enabled`、`replay_environment_id` | 默认关闭；开启后回放 Environment ID 可省略，由 Worker 自动获取或创建 |
| `replay_isolation` | `{vaults?, mcp?, custom_tools?}` 显式能力声明 |
| `runtime_binding` | `{skill_name, paths, working_directory, source?, artifact?, packages?, pre_setup?, replace_setup_script?, bootstrap, ready_file, ready_probe_message?}`；`source` 默认 `embedded`，`skill_mount` 核验固定 Skill 文件；`openviking` 下载 `artifact`（`base_url/uri/sha256/bytes/files[{path,sha256,bytes}]`）固定的独立 tar.xz，校验并缓存后 bootstrap；`packages` 可声明 `apt/pip` 数组，与来源环境去重合并，交由 MA 在初始化脚本前准备；前置脚本最多 32 KiB，替换原脚本默认 false；可选只读探测先于业务任务 |

同 MA origin + entry Agent 只能被一个租户托管。配置 CAS 冲突或目标占用返回 409。下载、隔离或固定快照缺失记录为失败，不能自动放宽发布门禁。

`detail.replay_resources` 展示 `status`（`preparing/ready/credentials_missing/unsupported/disabled`）、`environment_id`、`vault_ids` 和 `missing_credentials`；仅回显非敏感状态。自动资源按租户、连接和 MA 地址标记归属，由外部效果账本对账后复用，未知创建结果不重复 POST。自动环境复制业务依赖与初始化配置，但不复制生产 TOS 输出位置。自动 Vault 可注入本租户已有的 MA Key 为限制 MA 主机的 `ARK_API_KEY`；无法读出或推断其他服务的密钥。缺失凭证时可继续采集，但回放能力保持不可执行。已配置的独立资源继续复用，生产环境或生产 Vault ID 不可用作回放资源。受理回放时固定发现记录中的实际 ID，不修改连接配置 revision。

## 数据面发布快照

`GET /sync/ma/{connection_id}?user_id=employee` 使用 `Authorization: Bearer <tenant-key>`。返回 `release_id`、`manifest_hash`、`connection_revision`、入口 ID / 固定版本、Environment ID / 配置哈希、`agents`、`skills`、可选 `runtime_tree_sha256`。`environment_alias` 是用于查找的固定业务别名，`environment_name` 是 MA 中不可变的实际名称；历史 release 可从 `base_environment` 派生别名，不要求改写已有 Environment。Skill 中的 `tree_sha256` 是规范化文件树哈希，与 ZIP 字节哈希不同。

响应支持 `ETag` 与 `If-None-Match` / 304，`Cache-Control: private, no-cache`。只有完全应用的 release 可读；同步过程中返回上一完整 release，无完整 release 返回 503。错误或不存在跨租户连接返回 404，不泄露其他租户资源。

## 回放配置

`GET /api/replay/settings` 返回 `revision` 与 `config`。`PUT /api/replay/settings` 接收 `{expected_revision, config}`，其中 `concurrency` 为 1–2、`timeout_seconds` 为 30–1800、`max_interactions` 为 1–20，要求租户管理员。代码入口为 `team_replay/api/settings.py:create_router`。

`GET /api/skill-evolution/settings` 与 PUT 的 `evolve` 支持 `bundle_text_extensions`，用于显式允许 `.mjs/.js/.json` 等文本文件；静态检查仍必须通过。流程与示例见 [MA 接入](../agent-integrations/07-managed-agents.md)。
