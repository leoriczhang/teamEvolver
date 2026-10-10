# 控制台身份与租户权限

## API 实现介绍

控制台采用三层身份模型：

| 身份 | 范围 | 主要能力 |
|------|------|----------|
| 平台超级管理员 | 全平台 | 租户生命周期、平台配置、全局审计、登录账号管理 |
| 租户管理员 | 单个租户 | 成员、租户 Key、租户配置、团队资产发布与审批 |
| 普通成员 | 单个租户 | 工作台、Session、个人资产、团队资产读取与发布申请 |

平台超级管理员可以查看租户列表，但不会自动获得租户治理权限。进入租户后必须提供原因并开启 30 分钟、绑定当前登录 Session 的支持模式，才能执行租户管理员操作。每次平台访问、支持模式变更和权限写操作都会进入权限审计。

浏览器必须显式发送 `X-Tenant-Id` 才会进入租户。服务端在每次请求中重新校验租户状态、账号状态和成员关系，不信任本地缓存。Agent 身份不查询控制台账号或成员表，继续使用租户 Key 加请求中声明的 `user_id`。

代码入口：

- `teamEvolver/admin/identity_store.py:IdentityStore`
- `teamEvolver/admin/authorization.py:bind_console_user`
- `teamEvolver/proxy/identity_routes.py:register_identity_routes`

## 接口和参数说明

所有接口使用控制台 HttpOnly Session Cookie。租户内接口还要求 `X-Tenant-Id` 与路径中的 `tenant_id` 一致。

| 方法 | 路径 | 权限 | 说明 |
|------|------|------|------|
| GET | `/api/auth/status` | 已登录账号 | 返回平台身份、当前租户角色、Capability、可用租户和支持模式 |
| GET | `/api/identity/tenants` | 已登录账号 | 返回当前账号可选择的活跃租户 |
| POST | `/api/identity/support` | 平台超级管理员 | 为当前 Session 开启 30 分钟支持模式 |
| DELETE | `/api/identity/support` | 平台超级管理员 | 退出当前 Session 的支持模式 |
| GET | `/api/identity/audit?limit=100` | 平台超级管理员 | 查询最近权限审计，`limit` 范围为 1 到 500 |
| GET | `/api/tenants/{tenant_id}/members` | 租户管理员或支持模式 | 查询租户成员 |
| PUT | `/api/tenants/{tenant_id}/members/{user_id}` | 租户管理员或支持模式 | 新增或更新成员关系 |
| PUT | `/api/identity/users/{user_id}/access` | 平台超级管理员 | 更新平台身份或账号状态 |

开启支持模式的请求体：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `tenant_id` | string | 是 | 当前要支持的活跃租户 |
| `reason` | string | 是 | 审计原因，最多保留 500 个字符 |

成员更新请求体：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `role` | string | 否 | `tenant_admin` 或 `member`，默认 `member` |
| `status` | string | 否 | `active` 或 `disabled`，默认 `active` |

平台账号更新请求体只接受 `platform_role`（`platform_admin` / `user`）和 `status`（`active` / `disabled`）。

## 使用示例

```bash
curl -X POST 'http://localhost:52010/api/identity/support' \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: tenant-a' \
  -b 'teamEvolver_console_session=<session>' \
  --data '{"tenant_id":"tenant-a","reason":"排查租户配置异常"}'
```

```bash
curl -X PUT 'http://localhost:52010/api/tenants/tenant-a/members/alice' \
  -H 'Content-Type: application/json' \
  -H 'X-Tenant-Id: tenant-a' \
  -b 'teamEvolver_console_session=<session>' \
  --data '{"role":"tenant_admin","status":"active"}'
```

## 响应、错误与迁移

`GET /api/auth/status` 的 `user` 包含：

- `platform_role`：全局身份；
- `tenant_role`：当前显式选择租户中的角色，没有成员关系时为 `null`；
- `tenant_id`：通过校验的当前租户，没有选择或选择无效时为 `null`；
- `capabilities`：本次请求的生效能力；
- `support`：当前 Session 的支持模式记录或 `null`。

主要错误：

| 状态码 | detail | 原因 |
|--------|--------|------|
| 403 | `PLATFORM_ADMIN_REQUIRED` | 非平台超级管理员调用平台接口 |
| 403 | `TENANT_ADMIN_REQUIRED` | 普通成员执行租户治理 |
| 403 | `SUPPORT_MODE_REQUIRED` | 平台超级管理员未开启当前租户支持模式 |
| 403 | `TENANT_ACCESS_DENIED` | 选择了无权访问或已停用的租户 |
| 403 | `TENANT_MEMBERSHIP_REQUIRED` | 未显式选择租户或没有成员关系 |
| 409 | `LAST_PLATFORM_ADMIN` | 操作会移除最后一个活跃平台超级管理员 |
| 409 | `LAST_TENANT_ADMIN` | 操作会移除租户最后一个活跃管理员 |

首次运行新版本时，旧 `users.json` 或 PG `_admin/users.json` 只迁移一次：

1. 旧 `admin` 迁移为 `default` 租户的 `tenant_admin`，不会自动成为平台超级管理员。
2. 升级前设置 `TEAMEVOLVER_PLATFORM_ADMINS=<旧用户ID>[,<旧用户ID>...]`，仅这些用户在首次迁移时成为平台超级管理员。
3. 新安装的首次 bootstrap 用户直接成为平台超级管理员；自主注册用户没有隐式 `default` 成员关系。
4. 身份数据在 PG 模式写入独立控制表；Standalone 模式写入 `console_identity.sqlite3`。旧 JSON 在迁移完成后不再作为权限来源。
5. 若迁移已经完成，不要修改旧 JSON 重试；应由现有平台超级管理员通过控制台调整身份。

若迁移后没有平台超级管理员，可使用已配置的 `TEAMEVOLVER_ROOT_API_KEY` 调用
`PUT /api/identity/users/{user_id}/access` 恢复一个平台超级管理员；Root Key 不应写入浏览器或前端配置。
