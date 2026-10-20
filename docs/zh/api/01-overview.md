# API 概览

所有 HTTP 接口由 `api` 角色在同一端口（默认 52010）提供。完整路由清单见 [端点索引](./00-endpoints.md)，该页由代码生成。

| 项目 | 值 |
| --- | --- |
| Base URL | `http://<host>:52010` |
| Content-Type | `application/json`（文件上传也使用 JSON + base64 字段） |
| 字符编码 | UTF-8 |
| 长任务 | `202 Accepted`，返回 `job_id` 与 `status_url` |

## 认证方式

认证由 `teamEvolver/admin/authn.py:build_console_authorizer` 统一完成，每个请求按以下顺序识别一种身份：

| 身份 | 请求头 | 租户来源 | 可访问范围 |
| --- | --- | --- | --- |
| 服务 Root Key | `Authorization: Bearer <TEAMEVOLVER_ROOT_API_KEY>` | `X-Tenant-Id`，缺省 `default` | 全部接口，按平台管理员服务身份处理 |
| 租户 Key | `Authorization: Bearer tevt_...` | 由 Key 反查 | 仅 Agent 数据面路径 |
| 控制台 Session | Cookie `teamEvolver_console_session_<hash>` + `X-Tenant-Id` | 显式 `X-Tenant-Id`，每次请求重新校验成员关系 | 控制台接口，按能力授权 |
| 匿名 | 无 | — | `/`、`/console`、`/healthz`、`/readyz`、`/api/auth/*`、静态资源 |

### 服务 Root Key

`TEAMEVOLVER_ROOT_API_KEY` 是部署级密钥，供运维脚本和 CLI 使用（`teamEvolver datasource` 即通过它调用 API）。它对应平台管理员服务身份，可通过 `X-Tenant-Id` 选择任一 active 租户；租户不存在或已停用时返回 `403 TENANT_ACCESS_DENIED`。

只有 Root Key 能读写租户 Python 源码：`/api/datasource/code`、`/api/replay-adapter/code` 及其 `test` 接口。Root Key 不能下发到浏览器。

### 租户 Key

租户 Key 以 `tevt_` 开头，是 Agent 数据面唯一的机器凭证。服务端只保存其 SHA-256 摘要，由 `teamEvolver/tenants/registry.py:TenantRegistry` 反查租户。Key 无效或租户停用返回 `401 invalid tenant token`；携带的 `X-Tenant-Id` 与 Key 所属租户不同返回 `403 tenant mismatch`。

租户 Key 只能访问下列路径（`teamEvolver/admin/authn.py` 中的 `_AGENT_MACHINE_PATHS`）：

- `/ingest_session`
- `/internal/agents/context/*`
- `/sync/skills`
- `/api/datasource/pull`
- `/status`、`/history`

访问其他路径返回 `403 console admin required`。调用方在请求中声明 `user_id`，主体为 `(tenant_id, user_id)`，规则见 [协议 v2](../agent-integrations/06-protocol-v2.md)。

签发与轮换：

- 平台管理员创建租户时由 `POST /api/tenants` 返回一次明文 Key。
- 租户管理员通过 `POST /api/tenants/{tenant_id}/rotate-token` 轮换，旧 Key 立即失效。
- `default` 租户不由控制台签发，使用环境变量 `TEAMEVOLVER_TENANT_TOKEN` 配置唯一一条 Key（必须以 `tevt_` 开头）；轮换即修改该值后重启。

### 控制台 Session

`POST /api/auth/login` 成功后设置 HttpOnly Cookie，名称为 `teamEvolver_console_session_<host:port:root_path 的 12 位哈希>`，有效期 24 小时并在使用中续期。浏览器每次请求都必须显式携带 `X-Tenant-Id`，服务端用 `teamEvolver/admin/authorization.py:resolve_console_membership` 校验成员关系：

- 普通成员获得 `workspace.use`、`sessions.use`、`personal.manage`、`team.read`、`team.submit`。
- 租户管理员另获得 `tenant.manage`、`members.manage`、`team.publish`。
- 平台管理员可进入任一 active 租户并获得租户管理员能力，另有 `platform.manage`、`users.manage`、`audit.read`。

没有有效租户时，只有登录、身份和租户管理等控制面路径可用，其余返回 `403 TENANT_MEMBERSHIP_REQUIRED` 或 `403 TENANT_ACCESS_DENIED`。详见 [控制台身份与租户权限](./20-console-rbac.md)。

## 异步任务

写入外部系统或耗时的操作只在一个短事务内写入领域记录和 `worker_tasks`，然后返回：

```json
{"job_id": "...", "status": "queued", "status_url": "/api/jobs/<job_id>"}
```

各模块的受理响应还会附带自身字段（如 `ingestion_id`、`run_id`）。用 [统一任务接口](./07-health-status.md#统一任务接口) 查询、取消或重试；各模块的 `.../jobs/{job_id}/result` 返回业务结果。

带 `Idempotency-Key` 请求头的受理接口，相同 Key 重复提交返回第一次受理的任务；同一 Key 携带不同请求体返回 409。Key 最长 200 字符。

## 错误格式

错误统一为 HTTP 状态码 + JSON：

```json
{"detail": "TENANT_ADMIN_REQUIRED"}
```

常见错误：

| 状态 | detail | 含义 |
| --- | --- | --- |
| 400 | `USER_ID_REQUIRED` / `USER_ID_INVALID` | Agent 请求缺少或使用了非法 `user_id` |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED` | Session envelope 的 `protocol_version` 不是 `2.0` |
| 401 | `TENANT_TOKEN_REQUIRED` | Agent 数据面未使用有效租户 Key |
| 401 | `login required` | 控制台接口未登录 |
| 403 | `TENANT_ADMIN_REQUIRED` / `PLATFORM_ADMIN_REQUIRED` | 能力不足 |
| 403 | `CONTEXT_SCOPE_FORBIDDEN` | Context 作用域不允许该操作 |
| 404 | `CONTEXT_REF_INVALID` | `context_ref` 不存在、已撤销或不属于当前用户 |
| 409 | — | 版本冲突（`expected_revision` / `expected_sha256` 不匹配）或状态不允许 |
| 413 | — | 请求体超过接口上限 |
| 503 | — | 租户模块配置缺失、外部依赖未配置或不可用 |

## 请求上限

| 接口 | 上限 |
| --- | --- |
| `POST /ingest_session` 请求体 | 32 MiB，`TEAMEVOLVER_MAX_SESSION_BODY_BYTES` 可调，最小 1 KiB |
| Context 请求体 | 256 KiB |
| Context `resolve.query` | 8000 字符 |
| Context `remember.content` | 128 KiB |
| Context `read` 单次内容 | 500 KiB；Skill bundle 最多 100 个文件 |
| `context_usage` 每类引用 | 200 个 |
| Replay / 数据集请求体 | 96 MiB |
| Skill 库写入请求体 | 96 MiB |
| Memory 请求体 | 256 KiB |

## API 分组

| 文档 | 内容 |
| --- | --- |
| [Session 上报与数据源](./03-session-ingest.md) | `POST /ingest_session`、数据源 Adapter 与定时拉取 |
| [Agent Context](./04-context-workspace.md) | Context 九个端点 |
| [Replay 与实验工作台](./05-replay-branch.md) | Replay Adapter、数据集、Replay 运行、工作台 |
| [Skill 拉取](./06-skill-sync.md) | `GET /sync/skills` |
| [健康、状态与任务](./07-health-status.md) | `/healthz`、`/readyz`、`/status`、`/api/jobs/*` |
| [Session 查询](./08-sessions-api.md) | 控制台 Session 列表、内容、导出 |
| [团队 Skill 库](./09-skills-admin.md) | Skill CRUD、版本、回滚、投递记录 |
| [候选与进化](./10-validation.md) | Skill Candidate、进化触发、Prompt 与进化设置 |
| [团队 Memory](./11-team-memory-aggregation.md) | Memory 运行、维护、变更与 Memory Replay |
| [服务日志状态](./15-logging-status.md) | `/api/logging/status` |
| [成功经验](./16-experience-sync.md) | 经验、Pattern 与经验同步 |
| [Skill 导入导出](./18-skill-transfer.md) | ZIP、市场、Git |
| [控制台身份与租户权限](./20-console-rbac.md) | 登录、用户、租户、成员、平台设置 |
| [知识库挖掘](./22-knowledge-mining.md) | `/api/knowledge-mining/*` |
| [Workspace 与平台资产](./23-workspace-assets.md) | `/api/openviking/workspace/*`、`/api/platform-assets/*` |
| [文档维护](./99-docs-maintenance.md) | 文档规范、端点索引与检查脚本 |
