# Agent 注册 API

## 1. API 实现介绍

Agent 注册接口用于将新的 Agent 运行时注册到 teamEvolver，并获取限定作用域的访问令牌。V1 注册使用控制面密钥认证，注册时声明 Agent 支持的能力（capabilities）、回调端点和元数据。

首次注册成功时，响应中包含 `credentials.agent_access_token`，该令牌仅返回一次，服务端仅存储其 SHA-256 哈希。重复注册已存在的 agent_id 不会重新签发令牌，除非请求中指定 `rotate_access_token: true`。

代码实现：`teamEvolver/integrations/agent_registry.py:107` (`register_agent`)
路由入口：`teamEvolver/proxy/routes.py:3366` (`register_agent_runtime`)

## 2. 接口和参数说明

### 请求

```
POST /internal/agents/register
Authorization: Bearer <EVOLVE_INGEST_API_KEY>
Content-Type: application/json
```

### 请求参数

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | 必须为 `teamevolver.agent-registration.v1` |
| `protocol_version` | string | 是 | 协议版本，必须匹配 `^1\.` 模式（如 `1.0`） |
| `agent_id` | string | 是 | Agent 唯一标识，格式 `runtime:tenant`，最长 160 字符 |
| `runtime_type` | string | 是 | 运行时类型标识（如 `hermes`、`agentshub`、`my-agent`） |
| `runtime_version` | string | 否 | Agent 运行时版本号 |
| `display_name` | string | 否 | 显示名称，默认为 agent_id |
| `capabilities` | object/array | 是 | 声明支持的能力，推荐使用 object 格式携带详细配置 |
| `endpoints` | object | 否 | Agent 回调端点配置 |
| `endpoints.health_url` | string(uri) | 否 | 健康检查 URL |
| `endpoints.replay_url` | string(uri) | 否 | Replay 回调 URL（声明 replay.branch.v1 时必填） |
| `endpoints.skill_sync_url` | string(uri) | 否 | Skill Sync webhook URL |
| `auth` | object | 否 | 认证配置（不含密钥） |
| `metadata` | object | 否 | 自定义元数据（如 tenant_id） |
| `subject_mappings_authoritative` | boolean | 否 | 是否权威同步主体映射，默认 false |
| `subject_mappings` | array | 否 | 主体映射列表，权威模式下替换已有映射 |
| `subject_mappings[].external_subject` | string | 否 | Agent 侧用户标识 |
| `subject_mappings[].team_evolver_user_id` | string | 否 | teamEvolver 用户 ID |
| `rotate_access_token` | boolean | 否 | 是否轮换访问令牌，默认 false |

### Capability 详情

| Capability | 详情字段 | 说明 |
|-----------|---------|------|
| `session.ingest.v1` | 无 | 支持 Session 上报 |
| `context.workspace.v1` | `scopes` | 可访问的 Context 范围数组，可选值：`personal_memory`、`team_memory`、`personal_skills`、`team_skills` |
| `replay.branch.v1` | `transport`、`endpoint`、`max_interactions`、`supports_materials`、`supports_artifacts`、`supports_full_trace`、`idempotent`、`auth_profile` | True Replay 回调配置 |
| `skill.sync.v1` | `transport`、`endpoint`、`auth_profile` | Skill Sync 推送配置 |

### 响应

| 字段 | 类型 | 说明 |
|------|------|------|
| `agent_id` | string | Agent 唯一标识 |
| `runtime_type` | string | 运行时类型 |
| `runtime_version` | string | 运行时版本 |
| `display_name` | string | 显示名称 |
| `capabilities` | array[string] | 规范化后的能力列表 |
| `capability_ids` | array[string] | 规范化后的 capability ID 列表（含别名映射） |
| `endpoints` | object | 已验证的端点配置 |
| `status` | string | 状态（`active`） |
| `created_at` | string(ISO8601) | 创建时间 |
| `updated_at` | string(ISO8601) | 更新时间 |
| `credentials` | object | 凭证信息（仅首次注册或轮换时返回） |
| `credentials.agent_access_token` | string | Agent 访问令牌，格式 `tev1_<random>` |
| `subject_sync` | object | 主体同步结果 |
| `subject_sync.missing_user_ids` | array[string] | 映射中未找到的用户 ID |
| `access_token_configured` | boolean | 是否已配置访问令牌（公开记录中） |

## 3. 使用示例

### 最小注册示例

```bash
curl -X POST "http://localhost:52010/internal/agents/register" \
  -H "Authorization: Bearer my-control-plane-key" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "runtime_version": "1.0.0",
    "capabilities": {
      "session.ingest.v1": {}
    }
  }'
```

### 完整注册示例

```bash
curl -X POST "http://localhost:52010/internal/agents/register" \
  -H "Authorization: Bearer my-control-plane-key" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "runtime_version": "1.0.0",
    "display_name": "My Custom Agent",
    "capabilities": {
      "session.ingest.v1": {},
      "context.workspace.v1": {
        "scopes": ["personal_memory", "team_memory", "team_skills"]
      },
      "replay.branch.v1": {
        "transport": "http",
        "endpoint": "https://agent.example.com/replay",
        "max_interactions": 10,
        "supports_materials": true,
        "supports_full_trace": true,
        "auth_profile": "my_agent"
      },
      "skill.sync.v1": {}
    },
    "endpoints": {
      "health_url": "https://agent.example.com/health",
      "replay_url": "https://agent.example.com/replay",
      "skill_sync_url": "https://agent.example.com/skill-sync"
    },
    "metadata": {
      "tenant_id": "tenant-a"
    }
  }'
```

## 4. 响应契约与错误处理

### 成功响应示例

```json
{
  "schema_version": "teamevolver.agent-registration.v1",
  "protocol_version": "1.0",
  "runtime_version": "1.0.0",
  "agent_id": "my-agent:prod",
  "runtime_type": "my-agent",
  "display_name": "My Custom Agent",
  "capabilities": ["session.ingest.v1"],
  "capability_ids": ["session.ingest.v1"],
  "capability_details": {},
  "endpoints": {},
  "status": "active",
  "created_at": "2024-01-15T10:30:00Z",
  "updated_at": "2024-01-15T10:30:00Z",
  "credentials": {
    "agent_access_token": "tev1_abcdef1234567890..."
  }
}
```

### 错误码

| HTTP 状态码 | 错误信息 | 原因 |
|------------|---------|------|
| 401 | `invalid Agent control-plane key` | 控制面密钥错误 |
| 503 | `EVOLVE_INGEST_API_KEY is required for Agent Protocol V1 registration` | 服务端未配置控制面密钥 |
| 400 | `agent_id is required` | 缺少 agent_id |
| 400 | `V1 registration cannot carry storage credentials` | V1 注册中包含了 storage 字段（OpenViking 凭证），V1 不允许 |
| 400 | `Agent endpoint must be an HTTP(S) URL` | endpoints 中的 URL 格式无效 |
| 400 | `Agent endpoint cannot contain credentials` | URL 中包含用户名密码 |
| 400 | `Agent endpoint targets a forbidden metadata host` | URL 指向云元数据服务 |
| 400 | `Agent endpoint targets a forbidden IP address` | URL 指向链路本地/组播/未指定地址 |
| 400 | `unsupported registration schema` | schema_version 不正确 |
| 400 | `PROTOCOL_VERSION_UNSUPPORTED: <version>` | protocol_version 主版本号不是 1 |

### 注意事项

1. V1 注册 payload 中**不得**包含 `storage` 字段（OpenViking endpoint/key），V1 模式下所有存储凭证保留在 teamEvolver 服务端。
2. Endpoint URL 必须是 http/https，不得包含凭据（user:pass@host），不得指向元数据服务（169.254.169.254 等）或私有/链路本地 IP 地址。
3. 注册 payload 中的密钥字段（key、token、secret、password、credential）会被自动清除，不会持久化到注册表。
4. 同一 agent_id 重复注册会更新记录但不会自动轮换令牌，需显式设置 `rotate_access_token: true`。
