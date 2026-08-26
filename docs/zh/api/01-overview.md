# API 概览

本文档描述 teamEvolver 服务的 HTTP API 接口。

## 基础信息

| 项目 | 值 |
|------|-----|
| 默认 Base URL | `http://<host>:52010` |
| Content-Type | `application/json`（除文件上传外） |
| 字符编码 | UTF-8 |

teamEvolver 统一使用单端口 `52010` 承载所有 HTTP 接口，包括健康检查、Agent 协议 API、控制台 API 和 Web 控制台静态资源。

## 认证方式

teamEvolver API 使用三种认证机制：

| 认证方式 | 适用场景 | Header 格式 |
|----------|---------|-------------|
| 控制面密钥 | Agent 注册 (`/internal/agents/register`) | `Authorization: Bearer <EVOLVE_INGEST_API_KEY>` |
| Agent 访问令牌 | Agent 协议 API（Session 上报、Context Workspace） | `Authorization: Bearer <agent_access_token>` |
| 控制台 Session Cookie | 控制台管理 API（`/api/*`） | Cookie: `teamEvolver_console_session=<token>` |
| 无认证 | 健康检查、状态查询 | 无需认证 |

### 控制面密钥

环境变量 `EVOLVE_INGEST_API_KEY` 配置的密钥，用于注册新 Agent。权限最高，必须妥善保管。未配置此变量时，V1 注册端点返回 503 错误。

代码入口：`teamEvolver/proxy/routes.py:768` (`_check_v1_control_plane_key`)

### Agent 访问令牌

注册成功后由 teamEvolver 签发的令牌（格式 `tev1_<random>`），权限仅限于该 Agent 注册时声明的 capabilities 对应的 scope。令牌与 integration_id 绑定，服务端仅存储 SHA-256 哈希。

令牌 scope 映射（`teamEvolver/integrations/agent_registry.py:201` `_access_scopes`）：

| Capability | 授予的 Scope |
|-----------|-------------|
| `session.ingest.v1` | `session.ingest` |
| `context.workspace.v1` | `context.describe`, `context.resolve`, `context.read`, `context.skills`, `context.session` |
| `memory.personal.write.v1` | `context.remember`, `context.forget` |

代码入口：`teamEvolver/integrations/agent_registry.py:259` (`verify_agent_access_token`)

### 控制台 Session

通过 `/api/auth/login` 登录后获得 HttpOnly Cookie，有效期 24 小时。`/api/*` 路径下的管理接口需要此认证，管理员用户额外执行权限检查。

代码入口：`teamEvolver/proxy/routes.py:1699` (`require_console_auth` 中间件)

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
| [Agent 注册](./02-agent-register.md) | 注册 Agent 运行时，获取访问令牌 |
| [Session 上报](./03-session-ingest.md) | 上报 Agent 会话轨迹数据 |
| [Context Workspace](./04-context-workspace.md) | 上下文解析、读取、Memory 读写 |
| [Replay 分支执行](./05-replay-branch.md) | teamEvolver 回调 Agent 执行 True Replay |
| [Skill 同步](./06-skill-sync.md) | Skill 拉取与推送同步 |

### 控制面接口

| 文档 | 说明 |
|------|------|
| [健康与状态](./07-health-status.md) | 健康检查、服务状态、手动触发进化 |
| [Session 查询](./08-sessions-api.md) | 查询队列中 Session 和已处理会话 |
| [Skill 管理](./09-skills-admin.md) | Skill CRUD、发布、回滚、版本管理 |
| [验证与 Candidate](./10-validation.md) | 候选 Skill 审核、批准、驳回、Replay 结果 |
| [团队记忆聚合](./11-team-memory-aggregation.md) | 跨 User 记忆聚合为团队共享记忆、任务进度、聚合 Skill 编辑 |

### 文档维护

| 文档 | 说明 |
|------|------|
| [文档维护指南](./99-docs-maintenance.md) | 文档编写规范和维护流程 |
