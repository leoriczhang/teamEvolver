# API 概览

Agent 数据面统一使用 `tevt_ + user_id`，完整身份约定见 [v2 协议](../agent-integrations/06-protocol-v2.md)。
控制台管理员使用登录 Session 或服务 Root Key。租户 Key 不能调用管理员 API。

| 能力 | API |
| --- | --- |
| Session 上报 | [POST /ingest_session](./03-session-ingest.md) |
| Context 九个端点 | [Context Workspace](./04-context-workspace.md) |
| Skill bundle 拉取 | [GET /sync/skills](./06-skill-sync.md) |
| Replay 绑定与源码管理 | [Replay adapter](./05-replay-branch.md) |
| Session 管理 | [Sessions](./08-sessions-api.md) |
| Skill 管理 | [Skills](./09-skills-admin.md) |
| 验证管理 | [Validation](./10-validation.md) |
| Memory | [Team Memory](./12-team-memory.md) |

身份缺失/非法分别返回 `400 USER_ID_REQUIRED` / `400 USER_ID_INVALID`；
缺少有效租户凭证返回 401。Context 资源还校验 tenant/user 与有效期。
旧协议退役步骤见 [升级指南](../guides/11-agent-deregistration.md)。

| 项目 | 值 |
|------|-----|
| 默认 Base URL | `http://<host>:52010` |
| Content-Type | `application/json`（除文件上传外） |
| 字符编码 | UTF-8 |

teamEvolver 统一使用单端口 `52010` 承载所有 HTTP 接口，包括健康检查、Agent 协议 API、控制台 API 和 Web 控制台静态资源。

## 认证方式

teamEvolver API 使用三种认证机制，并保留少量无认证的健康与兼容端点：

| 认证方式 | 适用场景 | Header 格式 |
|----------|---------|-------------|
| 控制面密钥 | Agent 注册 (`/internal/agents/register`) | `Authorization: Bearer <EVOLVE_INGEST_API_KEY>` |
| 租户机器凭证 | Agent 数据面与协议 API（Session 上报、Context Workspace、数据源拉取、状态查询） | `Authorization: Bearer <tenant_agent_token>` |
| 控制台 Session Cookie | 控制台管理 API（`/api/*`） | Cookie: `teamEvolver_console_session=<token>` |
| 无认证 | 健康检查、状态查询 | 无需认证 |

### 控制面密钥

环境变量 `EVOLVE_INGEST_API_KEY` 配置的密钥，用于注册新 Agent（注册控制面）与遗留（非 V1）Session 上报通道。权限最高，必须妥善保管。未配置此变量时，V1 注册端点不做认证、注册请求直接放行（生产环境务必配置）；请求携带的密钥与配置不匹配时返回 401。

代码入口：`teamEvolver/proxy/routes.py:_check_ingest_api_key`

### 租户机器凭证

Agent 数据面**唯一的机器凭证**（格式 `tevt_<random>`）：注册接口不再签发任何按 Agent 的访问令牌，客户端任何以 `tev1_` 开头的 bearer 都会被拒绝，返回 `401 {"detail": "AGENT_ACCESS_TOKEN_RETIRED"}`（便于诊断 Agent 配置中残留旧凭证的临时迁移提示，计划在后续小版本移除）。

它是**租户级全权机器凭证**：租户身份由服务端从凭证推导（客户端传 `X-Tenant-Id` 与服务端推导结果不一致会被拒绝），可调用以下机器路径：

- `/ingest_session`（含 Agent Protocol V1 上报）
- `/internal/agents/context/*`（Context Workspace 全部 9 条路由）
- `/langfuse/pull`、`/api/datasource/pull`、`/trigger`、`/status`、`/history`、`/sessions`、`/conversations`、`/storage/status`

该凭证不做 capability → scope 收敛（全权），但**调用 Context Workspace 时必须在请求中声明 `integration_id`**（GET 路由为 query 参数，POST 路由为 body 字段），服务端会校验该 Agent 属于当前租户且处于 active 状态。声明即归属：`context_ref`、Context Session、审计记录都以声明的 `integration_id` 为归属键。

**签发与轮换：**

- 多租户部署：由控制台「租户管理」创建租户时签发，仅此一次完整展示，服务端只存储 SHA-256 哈希；轮换接口为 `POST /api/tenants/{tenant_id}/rotate-token`，旧凭证立即失效。
- 单租户部署（未启用 `storage_pg`）：控制台不签发凭证，由运维通过 `TEAMEVOLVER_TENANT_TOKEN` 环境变量（或 config.yaml 中的 `tenant.machine_token`）配置**唯一一条**凭证。该值必须以 `tevt_` 开头，服务端将其解析为 `default` 租户。生成方式：`TEAMEVOLVER_TENANT_TOKEN="tevt_$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"`；轮换即修改该值并重启进程。控制台租户页会显示其是否已配置（未配置时展示上述生成命令）。单租户部署下该凭证在机器路径上等同于管理员。

管理接口（`/api/*`）与 Agent 注册接口（`/internal/agents/register`）**不接受**该凭证。`default` 租户不携带控制台签发的凭证：`POST /api/tenants/default/rotate-token` 会被拒绝（400 `default is reserved`），控制台也不会为其显示轮换入口。

代码入口：`teamEvolver/tenants/registry.py:resolve_by_agent_token`

### 控制台 Session

通过 `/api/auth/login` 登录后获得 HttpOnly Cookie，有效期 24 小时。`/api/*` 路径下的管理接口需要此认证，管理员用户额外执行权限检查。

代码入口：`teamEvolver/proxy/routes.py:require_console_auth` 中间件

## 版本控制

Agent 协议 API 使用 `protocol_version` 字段进行版本控制。当前版本为 `1.0`。未知主版本号返回 `PROTOCOL_VERSION_UNSUPPORTED` 错误。

- 注册时在 payload 顶层指定 `protocol_version`
- Session 上报时在 `runtime.protocol_version` 指定
- Replay 请求/响应均包含 `schema_version` 和 `protocol_version` 字段

## 错误格式

所有 API 错误统一使用 HTTP 状态码 + JSON 响应体：

```json
{
  "detail": "错误描述信息"
}
```

部分 Agent 协议接口返回结构化错误码（字符串常量），例如：

- `SUBJECT_NOT_MAPPED` -- 主体未映射（403）
- `PROTOCOL_VERSION_UNSUPPORTED` -- 协议版本不支持（400）
- `INVALID_PAYLOAD` -- 请求体格式错误（400）
- `WORKSPACE_TOKEN_INVALID` -- 访问令牌无效（401）
- `TENANT_TOKEN_REQUIRED` -- V1 Session 上报缺少租户机器凭证（401）
- `AGENT_ACCESS_TOKEN_RETIRED` -- 请求携带了已废弃的 `tev1_` Agent 访问令牌（401）
- `INTEGRATION_ID_REQUIRED` -- 使用租户机器凭证但未声明 integration_id（400）
- `UNKNOWN_INTEGRATION_ID` -- 声明的 integration_id 不属于当前租户或未注册（403）
- `INTEGRATION_DISABLED` -- 声明的 Agent 已被禁用（403）
- `CONTEXT_REF_INVALID` -- 上下文引用无效或过期（404）
- `CONTEXT_SCOPE_FORBIDDEN` -- 上下文范围无权限（403）

## 限流说明

- Session 上报请求体最大 32MB（可通过 `TEAMEVOLVER_MAX_SESSION_BODY_BYTES` 环境变量调整，最小 1KB）
- Context resolve 查询字符串最大 8000 字符
- Context remember 内容最大 128KB
- Context read 单内容最大 500,000 字符
- Skill bundle 读取最多 100 个文件，总内容不超过 500,000 字符
- Context Session 的 used_context_refs 最多 200 个

## API 分组

### Agent 协议接口

| 文档 | 说明 |
|------|------|
| [Agent 注册](./02-agent-register.md) | 注册 Agent 运行时身份（不签发令牌） |
| [Session 上报](./03-session-ingest.md) | 上报 Agent 会话轨迹数据 |
| [Context Workspace](./04-context-workspace.md) | 上下文解析、读取、Memory 读写 |
| [Replay 分支执行](./05-replay-branch.md) | teamEvolver 回调 Agent 执行 True Replay |
| [Skill 同步](./06-skill-sync.md) | Skill 拉取与推送同步 |

### 控制面接口

| 文档 | 说明 |
|------|------|
| [健康与状态](./07-health-status.md) | 健康检查、服务状态、手动触发进化 |
| [Session 查询](./08-sessions-api.md) | 查询队列中 Session 和已处理会话 |
| [Skill 管理](./09-skills-admin.md) | 团队/个人 Skill CRUD、发布申请、回滚与版本管理 |
| [验证与 Candidate](./10-validation.md) | Candidate 查询、Replay 评估、发布决策与删除 |
| [团队记忆聚合](./11-team-memory-aggregation.md) | 跨 User 聚合、任务恢复、聚合 Skill 与输出目录设置 |

### 控制台内部接口

以下 `/api/*` 端点服务于内置控制台，必须使用控制台 Session Cookie。它们随控制台同步演进，不属于 Agent Protocol V1 的稳定兼容面：

| 前缀 | 用途 | 主要文档 |
|------|------|----------|
| `/api/auth/*`、`/api/users/*`、`/api/team-settings` | 登录、首次管理员初始化、用户与身份映射 | [Web 控制台](../guides/03-console.md) |
| `/api/openviking/workspace/*` | Workspace 浏览、L0/L1、条件批量写、CLI | [存储空间与目录布局](../concepts/09-storage-layout.md) |
| `/api/replay-lab/*`、`/api/openviking/memory/*` | Skill / Memory 实验与 True Replay | [Web 控制台](../guides/03-console.md) |
| `/api/mining/*` | 知识源、挖掘任务、产物与 LIFT | [Skill Miner 指南](../guides/07-skill-miner.md) |
| `/api/langfuse-config`、`/api/langfuse-tracing-config`、`/langfuse/*` | 租户数据源、全局链路观测、拉取、映射和状态 | [可观测性指南](../guides/04-observability.md) |
| `/api/docs/*` | 内置文档目录、页面读取和搜索 | [文档维护指南](./99-docs-maintenance.md) |
| `/api/sharing-config`、`/api/model-settings`、`/api/model-settings/test`、`/api/skill-evolution/settings` | 共享/本地回退存储配置、进化模型配置（含连通性测试）与进化设置 | [Web 控制台](../guides/03-console.md) |
| `/api/skill-evolution/session-analysis/audit`、`/api/mined-skills`、`/api/mined-skills/{name}/submit` | Session 过滤审计查询、挖掘产物查询与提交 | [Skill Miner 指南](../guides/07-skill-miner.md) |
| `/api/openviking-accounts`、`/api/openviking-accounts/{account}/users`、`/api/openviking-accounts/{account}/import-users` | OpenViking 账号列表、账号用户与用户导入 | [Web 控制台](../guides/03-console.md) |
| `/sessions`、`/conversations`、`/conversations/export`、`/conversations/status`、`/conversations/{session_id}`、`/conversations/{session_id}/process`、`/history` | 控制台 Session/会话浏览、导出、处理状态与历史查询 | [Session 查询](./08-sessions-api.md) |
| `/v1/models`、`/v1/chat/completions` | OpenAI 兼容模型代理（模型列表与对话补全） | — |
| `/internal/agentshub/openviking-config`、`/internal/reload-skills` | AgentsHub 内部配置下发、Skill 运行时重载 | — |

### 文档维护

| 文档 | 说明 |
|------|------|
| [文档维护指南](./99-docs-maintenance.md) | 文档编写规范和维护流程 |
