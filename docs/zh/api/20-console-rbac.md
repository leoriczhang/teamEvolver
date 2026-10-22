# 控制台身份与租户权限 API

控制台使用三层身份：

| 身份 | 范围 | 能力 |
| --- | --- | --- |
| 平台管理员（`platform_role=platform_admin`） | 全平台 | 创建与停用租户、平台设置、账号管理、权限审计；可进入任一 active 租户并拥有租户管理员能力 |
| 租户管理员（成员 `role=tenant_admin`） | 单个租户 | 成员、租户 Key、租户配置、团队资产发布与审批 |
| 普通成员（成员 `role=member`） | 单个租户 | 工作台、个人资产、团队资产读取与发布申请 |

浏览器每次请求都必须显式发送 `X-Tenant-Id`，服务端用 `teamEvolver/admin/authorization.py:resolve_console_membership` 重新校验租户状态、账号状态与成员关系。能力集合见 [API 概览 · 控制台 Session](./01-overview.md#控制台-session)。Agent 不使用控制台账号，见 [协议 v2](../agent-integrations/06-protocol-v2.md)。

账号、成员关系与审计保存在 PostgreSQL，由 `teamEvolver/admin/identity_store.py:IdentityStore` 读写。

代码入口：

- 认证：`teamEvolver/admin/authn.py:build_console_authorizer`
- 登录、用户、身份、成员：`teamEvolver/admin/api/__init__.py:create_router`
- 租户：`teamEvolver/tenants/api/__init__.py:create_router`
- 平台与租户设置：`teamEvolver/admin/api/settings.py:create_router`

## 登录

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/auth/status` | 匿名 | 登录状态、当前用户、可用租户、`needs_setup`、`self_registration_enabled`、`customer_mode` |
| POST | `/api/auth/bootstrap` | 匿名（仅无账号时） | 创建首个账号，成为平台管理员；密码至少 12 个字符 |
| POST | `/api/auth/login` | 匿名 | `{"username", "password"}`，设置 Session Cookie |
| POST | `/api/auth/register` | 见下文 | 自主注册普通账号 |
| POST | `/api/auth/logout` | 已登录 | 撤销 Session 并清除 Cookie |

`/api/auth/status` 中的 `user` 包含 `platform_role`、`tenant_id`（通过校验的当前租户，否则为 null）、`tenant_role`、`capabilities`。

### 自主注册

`POST /api/auth/register` 接收 `username`、`password`，可选 `display_name`、`email`，创建的账号始终为普通用户且没有任何租户成员关系。

- 启动配置 `self_registration_enabled=true`（环境变量 `TEAMEVOLVER_SELF_REGISTRATION_ENABLED`）时允许匿名注册。
- 关闭时只有服务 Root Key 可以调用，其他请求返回 `403 SELF_REGISTRATION_DISABLED`。
- 尚未初始化首个账号返回 409；用户名已存在返回 409。

```bash
curl -X POST http://localhost:52010/api/auth/register \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"username": "new-user", "password": "replace-with-a-strong-password"}'
```

## 账号

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/users` | 已登录 | 平台管理员看到全部账号；租户管理员看到本租户 active 成员；普通成员只看到自己 |
| GET | `/api/users/{user_id}` | 本人或平台管理员 | 账号详情 |
| POST | `/api/users` | 平台管理员 | 创建普通账号，不能指定 `role` 或 `platform_role` |
| PUT | `/api/users/{user_id}/profile` | 本人或平台管理员 | 修改资料 |
| DELETE | `/api/users/{user_id}` | 平台管理员 | 删除账号 |
| PUT | `/api/identity/users/{user_id}/access` | 平台管理员 | `{"platform_role": "platform_admin"|"user", "status": "active"|"disabled"}` |
| GET | `/api/identity/tenants` | 已登录 | 当前账号可进入的 active 租户 |
| GET | `/api/identity/audit?limit=100` | 平台管理员 | 最近的权限审计，`limit` 1–500 |

移除最后一个 active 平台管理员返回 `409 LAST_PLATFORM_ADMIN`。没有可用平台管理员时，用 Root Key 调用 `PUT /api/identity/users/{user_id}/access` 恢复。

## 租户与成员

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/tenants` | 已登录 | 平台管理员看到全部租户，其他用户看到自己所在租户；附带 `machine_credential`（`default` 租户 Key 是否已配置） |
| POST | `/api/tenants` | 平台管理员 | `{"display_name", "account_id"?}` 创建租户 |
| POST | `/api/tenants/{tenant_id}/status` | 平台管理员 | `{"status": "active"|"disabled"}` |
| POST | `/api/tenants/{tenant_id}/rotate-token` | 该租户管理员 | 轮换租户 Key，返回新的 `agent_token`；`default` 租户不可轮换 |
| GET | `/api/tenants/{tenant_id}/config` | 该租户管理员 | 租户配置覆盖（凭证字段只返回是否存在）与可编辑键 |
| PUT | `/api/tenants/{tenant_id}/config` | 该租户管理员 | `{"overrides": {...}}` 合并写入；值为 `null` 删除该键 |
| GET | `/api/tenants/{tenant_id}/members` | 该租户管理员 | 成员列表 |
| PUT | `/api/tenants/{tenant_id}/members/{user_id}` | 该租户管理员 | `{"role": "tenant_admin"|"member", "status": "active"|"disabled"}` 新增或更新成员 |

路径中的 `tenant_id` 必须等于当前 `X-Tenant-Id`，否则返回 `403 TENANT_MISMATCH`。

创建租户时：

1. 在 PostgreSQL 写入租户行，并为 5 个模块（`session`、`skills`、`replay`、`memory`、`miner`）写入默认配置行。
2. 尽力创建同名 OpenViking Account，并写入 `sharing_viking_account`；失败时不回滚租户，响应的 `openviking_account.error` 给出原因。
3. 尽力初始化 OpenViking 目录骨架，结果在 `openviking_dirs` 中返回。
4. 返回一次明文 `agent_token`，服务端只保存其 SHA-256。

租户配置覆盖使用 `TeamEvolverConfig` 的扁平字段名。租户管理员只能写入模型相关字段（`llm_provider`、`llm_api_base`、`llm_model_id`、`llm_api_key`、`llm_max_tokens`、`llm_temperature`、`llm_max_concurrency`、`llm_queue_capacity`、`llm_request_timeout_seconds`）与配额（`max_concurrent_sessions`、`max_evolve_per_day`），其他字段返回 `403 PLATFORM_CONFIG_REQUIRED`；平台管理员可写入除服务级字段外的全部字段，未知字段返回 400。模块配置（`tenant_configs`）由各模块接口维护，不经过此接口。

## 平台与租户设置

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET / PUT | `/api/model-settings` | 租户管理员 | 租户进化模型：`model`、`base_url`、`temperature`、`max_tokens`、`api_key` / `clear_api_key`，保存在 `skills` 模块配置的 `model` |
| POST | `/api/model-settings/test` | 租户管理员 | 测试模型连通性，返回 202 |
| GET | `/api/sharing-config` | 租户管理员 | OpenViking 部署方式、地址、Account、团队 User、Key 是否存在 |
| PUT | `/api/sharing-config` | 平台管理员 | 修改上述字段；`api_key` 以 Secret Ref 保存在全局配置 |
| POST | `/api/sharing-config/bootstrap-dirs` | 平台管理员 | `{"account"?}` 初始化 OpenViking 目录骨架，返回 202 |
| GET / PUT | `/api/team-settings` | 平台管理员 | 控制台显示名称 `display_name` |
| GET / PUT | `/api/langfuse-tracing-config` | 平台管理员 | 出站 Langfuse 链路观测配置 |
| POST | `/api/langfuse-tracing-config/test` | 平台管理员 | 测试链路观测，返回 202 |
| GET | `/api/storage/status` | 租户管理员 | PostgreSQL 连接池状态 |
| GET | `/api/openviking-accounts` | 平台管理员 | OpenViking Account 列表 |
| GET | `/api/openviking-accounts/{account}/users` | 平台管理员 | Account 下的用户 |
| POST | `/api/openviking-accounts/{account}/import-users` | 租户管理员 | `{"user_ids": [...]}` 导入为控制台账号 |

全局设置保存在 `teamevolver_config`，租户设置保存在 `tenant_configs`，写入使用 revision 乐观锁。凭证以 Secret Ref 加密保存，需要 `TEAMEVOLVER_SECRET_MASTER_KEY`（未设置时使用 `TEAMEVOLVER_ROOT_API_KEY` 作为主密钥），两者都缺失返回 503。

保存 OpenViking 设置只确认配置已持久化，不表示服务可达。目录检查由 Worker 读取数据库中的生效配置并解析凭证引用后执行；检查结果以任务结果中的 `openviking_dirs.action` 和 `errors` 为准，`endpoint` 表示实际连接的地址。`/api/storage/status` 返回的是 PostgreSQL 状态，不能用于判断 OpenViking 连通性。

## 个人 Skill 与发布申请

个人 Skill 保存在 OpenViking 个人空间（`personal_skills` 作用域）。

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/users/{user_id}/skills?space=personal` | 本人或租户管理员 | 列出个人或 OpenViking 团队目录（`space=team`）中的 Skill |
| GET / DELETE | `/api/users/{user_id}/skills/{name}` | 本人或租户管理员 | 读取 / 删除 |
| POST | `/api/users/{user_id}/skills` | 本人或租户管理员 | 保存：`space`、`name`、`skill_md` 或 `description` / `category` / `body` |
| POST | `/api/users/{user_id}/share` | 本人或租户管理员 | `{"direction": "personal_to_team"|"team_to_personal", "skill_names": [...]}` 复制；`personal_to_team` 仅租户管理员 |
| POST | `/api/users/{user_id}/publish-requests` | 本人或租户管理员 | `{"skill_names", "note"}` 申请发布到团队 |
| GET | `/api/skill-publish-requests` | 已登录 | 租户管理员看到全部，普通成员看到自己的申请 |
| POST | `/api/skill-publish-requests/{request_id}/approve` | 租户管理员 | 批准，把个人 Skill 复制到 OpenViking 团队目录 |
| POST | `/api/skill-publish-requests/{request_id}/reject` | 租户管理员 | 拒绝 |

批准与共享写入 OpenViking 的团队 Skill 目录（`team_skills` 作用域），供 Context 检索。Agent 通过 `/sync/skills` 拉取的团队 Skill 来自 PostgreSQL [团队 Skill 库](./09-skills-admin.md)，两者分别维护。

## 错误

| 状态 | detail | 原因 |
| --- | --- | --- |
| 401 | `login required` | 未登录 |
| 401 | `invalid username or password` | 登录失败 |
| 403 | `USER_DISABLED` | 账号已停用 |
| 403 | `PLATFORM_ADMIN_REQUIRED` | 非平台管理员调用平台接口 |
| 403 | `TENANT_ADMIN_REQUIRED` | 非租户管理员执行租户治理 |
| 403 | `TENANT_ACCESS_DENIED` | 选择了无权访问或已停用的租户 |
| 403 | `TENANT_MEMBERSHIP_REQUIRED` | 未选择租户或没有成员关系 |
| 403 | `TENANT_MISMATCH` | 路径租户与 `X-Tenant-Id` 不一致 |
| 403 | `PLATFORM_CONFIG_REQUIRED` | 租户管理员写入了平台字段 |
| 409 | `LAST_PLATFORM_ADMIN` / `LAST_TENANT_ADMIN` | 操作会移除最后一个 active 平台管理员或租户管理员 |
| 409 | `users already exist` | 已初始化后再次调用 bootstrap |
