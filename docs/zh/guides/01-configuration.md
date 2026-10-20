# 配置参考

TeamEvolver 的配置分两层：

| 层 | 来源 | 修改方式 | 生效 |
| --- | --- | --- | --- |
| 启动配置（bootstrap） | 环境变量 + 可选只读 YAML | 修改后重启进程 | 进程级 |
| 在线配置 | PostgreSQL：`teamevolver_config`（全局）、`tenant_configs`（每租户 5 个模块）、`tenants.config`（租户覆盖） | 控制台或 API，带 revision 乐观锁 | 新受理的任务固定当时的 revision |

启动配置只读，CLI 不提供写入命令。在线配置的写入入口见各 API 页面。

## 启动配置

### 加载顺序

`teamEvolver/bootstrap.py:load_bootstrap`：

1. `TEAMEVOLVER_CONFIG_FILE` 指定的 YAML（可选）。顶层键必须是 `teamEvolver/config.py:TeamEvolverConfig` 的扁平字段名，出现未知键或旧的分节 / 点路径写法即拒绝启动。
2. 下表环境变量覆盖对应字段。
3. 未设置 `TEAMEVOLVER_PG_DSN` 时，由 `TEAMEVOLVER_PG_HOST` 等变量拼出 DSN。
4. 有 DSN 时 `storage_pg_enabled` 自动为 true。

查看生效结果（凭证已脱敏）：

```bash
teamEvolver config show
```

### 环境变量

| 环境变量 | 字段 | 说明 |
| --- | --- | --- |
| `TEAMEVOLVER_PG_DSN` | `storage_pg_dsn` | PostgreSQL DSN，必需 |
| `TEAMEVOLVER_PG_HOST`、`TEAMEVOLVER_PG_PORT`、`TEAMEVOLVER_PG_DATABASE`、`TEAMEVOLVER_PG_USERNAME`、`TEAMEVOLVER_PG_PASSWORD` | — | 未设置 DSN 时用于拼接，默认端口 5432、库名与用户名 `teamevolver` |
| `TEAMEVOLVER_PG_SCHEMA` | `storage_pg_schema` | 默认 `teamevolver` |
| `TEAMEVOLVER_OV_ENDPOINT` | `sharing_viking_endpoint` | OpenViking 地址 |
| `TEAMEVOLVER_OV_API_KEY` | `sharing_viking_api_key` | OpenViking Root Key |
| `TEAMEVOLVER_LLM_BASE_URL` | `llm_api_base` | OpenAI 兼容端点 |
| `TEAMEVOLVER_LLM_API_KEY` | `llm_api_key` | 模型 Key |
| `TEAMEVOLVER_LLM_MODEL` | `llm_model_id` | 模型 ID |
| `TEAMEVOLVER_LLM_CONCURRENCY` | `llm_max_concurrency` | 单进程模型并发，默认 8 |
| `TEAMEVOLVER_LLM_QUEUE_CAPACITY` | `llm_queue_capacity` | 模型调用排队上限，默认 64 |
| `TEAMEVOLVER_TENANT_TOKEN` | `tenant_machine_token` | `default` 租户的租户 Key，必须以 `tevt_` 开头 |
| `TEAMEVOLVER_SELF_REGISTRATION_ENABLED` | `self_registration_enabled` | 是否允许匿名注册，默认 true |
| `TEAMEVOLVER_API_HOST` / `TEAMEVOLVER_API_PORT` | `proxy_host` / `proxy_port` | API 监听地址，默认 `0.0.0.0:52010` |

不映射到配置字段、由代码直接读取的变量：

| 环境变量 | 说明 |
| --- | --- |
| `TEAMEVOLVER_CONFIG_FILE` | 启动 YAML 路径 |
| `TEAMEVOLVER_ROLE` | `api`、`worker` 或 `all`，`teamEvolver start` 未传 `--role` 时使用，默认 `api` |
| `TEAMEVOLVER_ENV` | 运行环境标签；`all` 角色只允许在 `local`（默认值）下运行 |
| `TEAMEVOLVER_ROOT_API_KEY` | 服务 Root Key，见 [API 概览](../api/01-overview.md#服务-root-key) |
| `TEAMEVOLVER_SECRET_MASTER_KEY` | Secret Ref 加密主密钥；未设置时使用 Root Key |
| `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` | Session 上报请求体上限，默认 32 MiB |
| `TEAMEVOLVER_LOG_*` | 日志参数，见 [服务日志状态](../api/15-logging-status.md#日志参数) |
| `TEAMEVOLVER_MULTI_REPLICA`、`TEAMEVOLVER_INSTANCE_ID` | 多副本时按实例分开日志目录 |
| `TEAMEVOLVER_CUSTOMER_MODE` | 为 `1` 时 `/api/auth/status` 返回 `customer_mode=true`，控制台按客户模式展示 |
| `TEAMEVOLVER_CONFIG_DIR` | 容器内 YAML 目录（默认 `/app/deploy/config`），也是本机身份库的默认目录 |
| `TEAMEVOLVER_AGENT_<PROFILE>_REPLAY_API_KEY` | Replay 传输的认证 Key |
| `OPENVIKING_CLI_BIN` | OpenViking CLI 路径，Workspace CLI 使用 |
| `LANGFUSE_PULL_HOST`、`LANGFUSE_PULL_PUBLIC_KEY`、`LANGFUSE_PULL_SECRET_KEY` | Langfuse 数据源 Adapter 读取 |
| `APP_PORT`、`TEAMEVOLVER_PORT` | 容器入口 `container_start.sh` 传给 `--port` |

### 常用字段

YAML 示例见仓库 `config/config.yaml`。PostgreSQL TLS 通过 YAML 字段 `storage_pg_ssl` 设置。

| 字段 | 默认 | 说明 |
| --- | --- | --- |
| `team_display_name` | `Team` | 控制台显示名称初始值（在线值由 `/api/team-settings` 修改） |
| `llm_api_base`、`llm_model_id`、`llm_max_tokens`、`llm_temperature` | `https://api.openai.com/v1`、`gpt-4o-mini`、16384、0.4 | 模型默认值；租户可通过 `/api/model-settings` 覆盖 |
| `sharing_viking_deployment` | `cloud` | `cloud` 或 `local`（`local` 时控制台显示 OpenViking Studio 链接） |
| `sharing_viking_account` / `sharing_viking_user` | `default` / `team` | `default` 租户的 Account 与团队 User |
| `sharing_viking_root_prefix` | `team-skill-evolver` | 团队 Skill 与个人 Skill 目录前缀 |
| `storage_pg_schema` | `teamevolver` | Schema 名 |
| `storage_pg_pool_min` / `storage_pg_pool_max` | 2 / 20 | 业务连接池 |
| `storage_pg_control_pool_max` | 5 | 控制面连接池 |
| `storage_pg_command_timeout_seconds` | 30 | 单条 SQL 超时 |
| `storage_pg_ssl` | `prefer` | PostgreSQL TLS 模式 |
| `skills_runtime_cache_dir` | `~/.teamEvolver/runtime/skills` | 团队 Skill 本机缓存，可随时删除重建 |
| `self_registration_enabled` | true | 匿名注册开关 |
| `langfuse_tracing_*` | 关闭 | 出站链路观测凭证；开关与采样等在线值由 `/api/langfuse-tracing-config` 修改 |
| `logging` | 见日志参数 | 日志设置对象 |

## 在线配置

### 全局配置

`teamevolver_config` 保存全局设置，由平台管理员修改：

| 键 | 接口 |
| --- | --- |
| `sharing`（OpenViking 地址、Account、团队 User、Key 的 Secret Ref） | `PUT /api/sharing-config` |
| `display_name` | `PUT /api/team-settings` |
| `tracing` | `PUT /api/langfuse-tracing-config` |

全局 `sharing` 中的值覆盖启动配置的 `sharing_viking_*`。

### 租户模块配置

每个租户在 `tenant_configs` 中有 5 行模块配置，创建租户或执行 `teamEvolver schema apply` 时写入默认行。模块配置缺失时相关接口返回 503，不回退到 YAML。

| 模块 | 内容 | 写入接口 |
| --- | --- | --- |
| `session` | `datasource_schedule` 等 | `PUT /api/datasource/schedule` |
| `skills` | Bundle 上限、进化参数、`model`、`prompt_overrides`、`stage_settings`、`experience_sync` | `/api/skill-evolution/settings`、`/api/skill-evolution/prompts/*`、`/api/model-settings`；经验同步目标无专用接口 |
| `replay` | `concurrency`（默认 2）、`timeout_seconds`（600）、`max_interactions`（4） | 无专用接口 |
| `memory` | `aggregation` 对象 | `POST /api/aggregation/settings` |
| `miner` | 知识库挖掘根目录与 Skill | 无专用接口 |

没有专用接口的模块由运维在 `tenant_configs` 中更新对应行的 `config` 并递增 `revision`。

### 租户覆盖

`tenants.config` 保存租户对启动配置字段的覆盖（扁平字段名），通过 `PUT /api/tenants/{tenant_id}/config` 修改，在请求时与启动配置合并。创建租户时自动写入 `sharing_viking_account`。可写字段见 [控制台身份与租户权限](../api/20-console-rbac.md#租户与成员)。

### 凭证

在线配置中的凭证（模型 Key、OpenViking Key、Langfuse Secret、Git / 市场 Token）以 Secret Ref（`sref_<32 hex>`）保存，明文用 `TEAMEVOLVER_SECRET_MASTER_KEY` 加密存于 `secret_refs`。接口只返回“是否已配置”。更换主密钥后旧 Secret Ref 无法解密，需要重新录入凭证。

## 控制台自主注册

`self_registration_enabled`（`TEAMEVOLVER_SELF_REGISTRATION_ENABLED`）是启动配置，修改后重启生效。关闭时只有 Root Key 可调用 `POST /api/auth/register`，见 [自主注册](../api/20-console-rbac.md#自主注册)。
