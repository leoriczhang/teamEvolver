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
| PUT | `/api/ma/connections/{connection_id}/resources/{skill_name}/bindings` | `{expected_revision, agent_ids}`；已有云端 Skill 返回 202 + `ma.skill` Job，挂载最新版本并回读，不新增 Skill 版本；未创建 Skill 只保存发布目标，新增关联 revision=0 |
| GET | `/api/ma/connections/{connection_id}/releases` | `limit`，完整 / 部分 release 及回读与错误事实 |
| GET | `/api/ma/skill-jobs/{job_id}` | `ma.skill` 的实际任务状态、原生版本写入结果与错误 |

创建与操作支持 `Idempotency-Key`；重用不同请求返回 409。云端写入、导入、分析与资源清理由 Worker 执行，返回统一 Jobs URL。Skill 查询在短业务事务前解析云端原生元数据与校验快照。

新建 Candidate 发布时，未显式指定目标则绑定所选连接的入口 Agent；显式空目标会阻止 Candidate 发布，已有 Skill 保留当前或显式关联。手工创建云端资源仍允许未挂载。发布结果提供 `deployment_status`（`bound` / `unbound` / `deleted`）和 `bound_agents`，只有成员引用回读成功后才能报告已挂载。内容未变时也执行绑定核对，但不上传新版本。

关联接口保存 `desired_agents` 并异步执行；`agents`、`bound_version` 和 `bound_versions` 在成功回读前保持最后观测值。取消全部目标会卸载 Skill。任务失败显示实际错误，不宣称已生效；仅变更关联不重置技能内容基线、历史 Evidence 或版本来源。

MA Skill 沿用 [Skill 管理 API](./09-skills-admin.md) 的查看、历史版本、编辑、新建、文件操作、回滚与删除路径，响应带 `skill_backend: "managed-agents"`，版本号为云端原生版本。可用 `connection_id` 查询参数选择连接；跨连接同名时要求显式选择。写入返回 `202`、`job_id`、`status_url`、`result_url`；上传与成员引用回读成功后才返回 published。回滚创建新云端版本，未知上传结果先对账，竞争上传暂停发布。发现 / reconcile 不从 TE 库拉取或回推 Skill。

发现完成后，采集与云端状态观察独立调度。`detail.operation_errors` 按操作保存错误、Job ID 与 `retryable`；一项成功只清除自身错误。临时网络与上游服务错误按配置频率再次调度；校验失败、鉴权失败与版本漂移暂停对应操作，修复后可手动触发。Skill 操作失败不暂停 Session 采集。未发布候选或未挂载 Skill 保留资源身份，云端 404 不复活旧 TE 导入。

## 连接字段

| 字段 | 说明 |
| --- | --- |
| `provider` | `volcengine-managed-agents` |
| `display_name`、`entry_agent_id` | 必填，1–200 字符 |
| `environment_id` | 可省略或留空，最多 200 字符；Worker 从入口 Agent 的最新业务 Session 自动识别并持续更新；显式值用于尚无 Session 时的初始环境 |
| `default_user_id` | 可选，最多 200 字符；明确默认归属。留空时，缺少标签或渠道身份的会话显示“未知提交人” |
| `ma_base_url` | 默认 `https://ark.cn-beijing.volces.com/api/v3`，生产 HTTPS |
| `ma_api_key`、`tenant_token` | 一次明文输入，加密保存，回显只返回配置是否存在 |
| `ma_secret_ref`、`tenant_token_ref` | 可提供本租户已有 Secret Ref；更新省略凭证时保留 |
| `team_evolver_url` | 兼容字段，MA Skills 管理不再使用 TE pull 地址 |
| `vault_ids`、`replay_vault_ids` | 业务 Vault 列表；回放固定来源 Session 的引用；旧 `replay_vault_ids` 不再控制新任务 |
| `user_tag_prefix` | 默认 `te-user:` |
| `poll_interval_seconds`、`sync_interval_seconds` | 默认 60 / 30；范围 15–86400 |
| `replay_enabled`、`replay_environment_id` | 新连接默认开启；固定来源 Session 环境，旧回放环境 ID 不再控制新任务；显式关闭和暂停仍生效 |
| `replay_readonly_mcp` | 精确 MCP Server 快照 SHA-256 → 已核验只读工具名数组；仅默认拒绝且实际启用工具全部在名单内时适用，服务配置或工具权限变化后重新核验 |
| `replay_isolation` | `{mcp?}` 外部工具隔离声明；旧 `vaults/custom_tools` 字段兼容读取，不代替原生引用或工具执行器 |
| `runtime_binding` | `{skill_name, paths, working_directory, source?, artifact?, packages?, pre_setup?, replace_setup_script?, bootstrap, ready_file, ready_probe_message?}`；`source` 默认 `embedded`，`skill_mount` 核验固定 Skill 文件；`openviking` 下载 `artifact`（`base_url/uri/sha256/bytes/files[{path,sha256,bytes}]`）固定的独立 tar.xz，校验并缓存后 bootstrap；`packages` 可声明 `apt/pip` 数组，与来源环境去重合并，交由 MA 在初始化脚本前准备；前置脚本最多 32 KiB，替换原脚本默认 false；可选只读探测先于业务任务 |

同 MA origin + entry Agent 只能被一个租户托管。配置 CAS 冲突或目标占用返回 409。下载、隔离或固定快照缺失记录为失败，不能自动放宽发布门禁。

`detail.replay_resources` 展示来源配置的 `status`（`ready/unsupported/disabled`）、`environment_id`、`vault_ids`，不包含秘密。发现流程只读取来源，不创建环境、Vault 或凭证。受理回放时从来源 Session 快照固定实际环境配置与 Vault 引用；缺失项由 Worker 补采原 Session，并单独冻结恢复结果供重试复用，不改写受理请求。两分支在独立 Session 中执行相同输入，仅替换目标 Skill，共享 TOS 输出配置被移除。创建后回读核对 Vault 引用和执行配置。旧自动资源保留历史，不属于新回放清理范围。

`detail.environment_observation` 包含 `session_id`、`created_at`、`environment_id`、`environment_hash`。发现、采集和同步任务自动按 Session 创建时间识别最新业务环境；环境变化在短事务中保存连接 revision 并受理 `ma.discover`，不更换连接、租户或 Key。环境配置快照缺失时仍可识别 ID，但哈希为空。尚无 Session 且未填写初始环境时，发现任务返回可重试的 `ma_EnvironmentNotObserved`，调度器持续按采集周期重试。

尚未执行的旧环境发现／同步任务返回 `environment_superseded`，保留原受理配置并停止更新当前发布记录；历史采集继续使用原固定快照。已开始执行的发现／发布流程仍按其 checkpoint 恢复。

## 数据面发布快照

`GET /sync/ma/{connection_id}?user_id=employee` 使用 `Authorization: Bearer <tenant-key>`。返回 `release_id`、`manifest_hash`、`connection_revision`、入口 ID / 固定版本、Environment ID / 配置哈希、`agents`、`skills`、可选 `runtime_tree_sha256`。`environment_alias` 是用于查找的固定业务别名，`environment_name` 是 MA 中不可变的实际名称；历史 release 可从 `base_environment` 派生别名，不要求改写已有 Environment。Skill 中的 `tree_sha256` 是规范化文件树哈希，与 ZIP 字节哈希不同。

响应支持 `ETag` 与 `If-None-Match` / 304，`Cache-Control: private, no-cache`。只有完全应用的 release 可读；同步过程中返回上一完整 release，无完整 release 返回 503。错误或不存在跨租户连接返回 404，不泄露其他租户资源。

## 回放配置

`GET /api/replay/settings` 返回 `revision` 与 `config`。`PUT /api/replay/settings` 接收 `{expected_revision, config}`，其中 `concurrency` 为 1–2、`timeout_seconds` 为 30–1800、`max_interactions` 为 1–20，要求租户管理员。代码入口为 `team_replay/api/settings.py:create_router`。

`GET /api/skill-evolution/settings` 与 PUT 的 `evolve` 支持 `bundle_text_extensions`，用于显式允许 `.mjs/.js/.json` 等文本文件；静态检查仍必须通过。流程与示例见 [MA 接入](../agent-integrations/07-managed-agents.md)。
