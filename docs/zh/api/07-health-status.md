# 健康与状态 API

## 1. API 实现介绍

健康与状态接口用于监控 teamEvolver 服务的运行状态、检查组件连通性，以及手动触发进化周期。这些接口不需要认证（设计为内网部署，应由网络边界控制访问）。

代码实现：`teamEvolver/proxy/routes.py`（`/health`、`/healthz`、`/status`、`/trigger`、`/storage/status`）

## 2. 接口和参数说明

---

### GET /health

健康检查端点，返回服务存活状态。

**认证：** 无

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | 固定为 `"ok"` |

---

### GET /healthz

Kubernetes 风格存活探针，返回简单的 ok 状态。

**认证：** 无

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `ok` | boolean | 固定为 `true` |

---

### GET /status

服务状态仪表盘，返回队列深度、注册技能数等运行时指标。

**认证：** 无（控制台也使用此接口）

**Query 参数：**

| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `refresh` | boolean | 否 | 是否强制刷新缓存，默认 false |

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `running` | boolean | 进化循环是否运行中（注：当前实现固定返回 false，服务可用性通过其他字段判断） |
| `pending_sessions` | integer | 队列中等待处理的 Session 数 |
| `registered_skills` | integer | 已注册的团队 Skill 数量 |
| `skills` | object | 技能详情映射，key 为技能名 |
| `skills.<name>.skill_id` | string | 技能 ID |
| `skills.<name>.version` | integer | 当前版本号 |

**缓存：** 响应缓存 5 秒，`refresh=true` 时强制刷新。

---

### POST /trigger

手动触发一次进化周期（evolve cycle）。后台仍会按配置间隔（默认 600 秒）自动扫描队列。

**认证：** 无（应由防火墙限制访问）

**方法：** POST

**响应：** 由嵌入式进化应用返回，通常为 202 Accepted 表示已触发。

**注意：** `/trigger` 路径经过嵌入式进化中间件处理，由 `teamEvolver/evolve/runtime/orchestrator.py` 中的进化引擎响应。如果进化引擎未就绪或未配置，可能返回错误。

相关代码：`teamEvolver/proxy/routes.py:824` (`_is_embedded_evolve_path`)、`teamEvolver/proxy/server.py:332` (`_dispatch_embedded_evolve_request`)

---

### POST /trigger-dreamcycle

触发 DreamCycle 内存维护任务。

**认证：** 无（内部接口）

**响应：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `started` 或 `not_configured` |

未配置 DreamCycle 时返回 503 状态码。

---

### GET /storage/status

检查共享存储（OpenViking）的配置和连通性。

**认证：** 无

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `backend` | string | 存储后端类型（`viking` 或 `none`） |
| `deployment` | string | 部署模式（`cloud` 或 `local`） |
| `endpoint` | string | OpenViking 端点地址 |
| `namespace` | string | 命名空间 |
| `api_key_present` | boolean | API Key 是否已配置 |
| `reachable` | boolean | 存储是否可达 |
| `reason` | string | 不可达原因（reachable=false 时） |

代码：`teamEvolver/proxy/routes.py:1520` (`_storage_status`)

---

### GET /langfuse/status

检查 Langfuse 可观测性集成状态。

**认证：** 无

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `enabled` | boolean | 是否启用 Langfuse |
| `tracing` | object | Tracing 状态 |
| `host` | string | Langfuse 主机地址 |
| `public_key_present` | boolean | Public Key 是否配置 |
| `secret_key_present` | boolean | Secret Key 是否配置 |
| `reachable` | boolean | 是否可达 |
| `total_sessions` | integer | 总 Session 数 |
| `reason` | string | 不可达原因 |

## 3. 使用示例

### 健康检查

```bash
curl -fsS "http://localhost:52010/health"
```

响应：

```json
{"status": "ok"}
```

### 查看服务状态

```bash
curl -fsS "http://localhost:52010/status"
```

响应示例：

```json
{
  "running": false,
  "pending_sessions": 3,
  "registered_skills": 5,
  "skills": {
    "database-debugging": {"skill_id": "database-debugging", "version": 3},
    "code-review": {"skill_id": "code-review", "version": 1}
  }
}
```

### 手动触发进化

```bash
curl -fsS -X POST "http://localhost:52010/trigger"
```

### 检查存储状态

```bash
curl -fsS "http://localhost:52010/storage/status"
```

响应示例（正常）：

```json
{
  "backend": "viking",
  "deployment": "local",
  "endpoint": "http://localhost:52011",
  "namespace": "resources",
  "api_key_present": false,
  "reachable": true
}
```

### 检查存活探针

```bash
curl -fsS "http://localhost:52010/healthz"
```

响应：

```json
{"ok": true}
```

## 4. 响应契约与错误处理

### 错误码

| HTTP 状态码 | 场景 |
|------------|------|
| 503 | DreamCycle 未配置（`/trigger-dreamcycle`） |
| 503 | 上游模型 base URL 未配置（模型代理端点，不影响健康检查） |
| 503 | Session 存储未配置（`/ingest_session` 时，不影响健康检查） |

### 部署建议

1. **负载均衡健康检查**：使用 `/healthz` 作为 L7 健康检查路径，`/health` 作为就绪探针。
2. **网络隔离**：`/trigger` 和 `/trigger-dreamcycle` 无认证，生产环境应通过防火墙限制为内网访问。
3. **监控指标**：定期抓取 `/status` 的 `pending_sessions` 值，持续增长表示进化处理能力不足或队列堵塞。
4. **存储告警**：`/storage/status` 返回 `reachable: false` 时触发告警，表示共享存储不可用，Skill 同步和 Memory 功能将降级。
