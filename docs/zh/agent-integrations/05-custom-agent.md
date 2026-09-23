# 自定义 Agent 接入

使用 [v2 协议](./06-protocol-v2.md)，在每次 Session/Context/Skill pull 中声明用户；
租户与 Account 由服务端配置确定。

以下章节保留 V1 时期的分步接入详细参考，供兼容窗口内使用。

本文档提供从零开始将自定义 Agent 接入 teamEvolver 的分步指南。包含最小接入和完整接入两种模式的代码示例。

## 接入步骤总览

### 最小接入（会话回流）

1. 获取控制面密钥 `EVOLVE_INGEST_API_KEY`
2. 通过 `/internal/agents/register` 注册 Agent，声明 `session.ingest.v1` 能力
3. 获取并安全存储租户机器凭证 `tevt_`（由运维提供）
4. 配置主体映射（管理界面或注册 payload 中的 `subject_mappings`）
5. 实现会话数据上报（`POST /ingest_session`）

### 完整接入（Context + Replay + Skill Sync）

在最小接入基础上继续实现：

6. 实现 Context Workspace 调用（resolve/read/skills）
7. 实现 Context Session 生命周期管理（start/append/commit）
8. 暴露 Replay Turn 端点供 teamEvolver 回调（或复用 `scripts/replay_turn_server.py`）
9. 实现 Skill Sync（拉取或接收推送 webhook）
10. 可选：实现个人 Memory 写入（remember/forget）

## 步骤 1：获取控制面密钥

联系 teamEvolver 运维获取控制面密钥 `EVOLVE_INGEST_API_KEY`。此密钥用于注册 Agent，权限等同于管理员，必须安全存储，不得暴露给客户端。

密钥通过环境变量在 teamEvolver 服务端配置：

```bash
export EVOLVE_INGEST_API_KEY="<your-secret-key>"
```

未配置此环境变量时，V1 注册端点返回 503 错误。

代码入口：`teamEvolver/proxy/routes.py:768` (`_check_v1_control_plane_key`)

## 步骤 2：注册 Agent

发送注册请求到 teamEvolver 服务。

### 最小接入注册示例

```bash
curl -X POST "http://<teamevolver-host>:52010/internal/agents/register" \
  -H "Authorization: Bearer <EVOLVE_INGEST_API_KEY>" \
  -H "Content-Type: application/json" \
  -d '{
    "schema_version": "teamevolver.agent-registration.v1",
    "protocol_version": "1.0",
    "agent_id": "my-agent:prod",
    "runtime_type": "my-agent",
    "runtime_version": "1.0.0",
    "display_name": "My Custom Agent",
    "capabilities": {
      "session.ingest.v1": {}
    }
  }'
```

### 完整接入注册示例

```bash
curl -X POST "http://<teamevolver-host>:52010/internal/agents/register" \
  -H "Authorization: Bearer <EVOLVE_INGEST_API_KEY>" \
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
        "orchestration": "server_driven",
        "endpoint": "https://my-agent.example.com/api/teamevolver/replay",
        "auth_profile": "my_agent"
      },
      "skill.sync.v1": {
        "transport": "http",
        "endpoint": "https://my-agent.example.com/api/teamevolver/skill-sync"
      }
    },
    "endpoints": {
      "health_url": "https://my-agent.example.com/health",
      "replay_url": "https://my-agent.example.com/api/teamevolver/replay",
      "skill_sync_url": "https://my-agent.example.com/api/teamevolver/skill-sync"
    },
    "subject_mappings_authoritative": true,
    "subject_mappings": [
      {
        "external_subject": "user-001",
        "team_evolver_user_id": "alice"
      },
      {
        "external_subject": "user-002",
        "team_evolver_user_id": "bob"
      }
    ]
  }'
```

代码实现：`teamEvolver/integrations/agent_registry.py:107` (`register_agent`)

## 步骤 3：获取并安全存储租户机器凭证

注册成功响应示例（注册**不再签发任何令牌**，响应中不包含任何凭证字段）：

```json
{
  "agent_id": "my-agent:prod",
  "runtime_type": "my-agent",
  "status": "active",
  "capabilities": ["session.ingest.v1"],
  "created_at": "2024-01-01T00:00:00Z"
}
```

Agent 数据面所需的**租户机器凭证**（格式 `tevt_<random>`）由运维提供：多租户部署下从控制台「租户管理」创建租户时获取，仅展示一次，轮换接口为 `POST /api/tenants/{tenant_id}/rotate-token`（旧凭证立即失效）；单租户部署（未启用 `storage_pg`）下由运维通过 `TEAMEVOLVER_TENANT_TOKEN` 环境变量配置（必须以 `tevt_` 开头，服务端将其解析为 `default` 租户）。

必须安全存储（如密钥管理服务、环境变量、加密配置文件）。teamEvolver 服务端仅存储其 SHA-256 哈希，无法找回丢失的凭证；凭证丢失或泄露时轮换：多租户在租户页轮换，单租户修改 `TEAMEVOLVER_TENANT_TOKEN` 并重启进程。

后续所有 Agent API 调用均使用该凭证认证：

```
Authorization: Bearer tevt_<random>
```

使用它调用 Context Workspace 时必须在每条请求中声明 `integration_id`（`describe`/`skills` 用 Query 参数，其余七条 POST 路由用 Body 字段），服务端校验该 Agent 属于当前租户且为 `active`，并以它作为 `context_ref`、Context Session 与审计记录的归属键（声明即归属）。该凭证不能调用管理接口（`/api/*`）和 Agent 注册接口（`/internal/agents/register`）。

## 步骤 4：映射主体

主体（Subject）是 Agent 侧的用户标识，需要映射到 teamEvolver 用户。有两种配置方式：

### 方式 A：注册时批量同步

在注册 payload 中设置 `subject_mappings_authoritative: true` 并提供 `subject_mappings` 数组。这会替换该集成的所有现有映射。

### 方式 B：管理界面配置

在 teamEvolver 控制台的 Agent 集成管理页面，手动添加 external_subject 到 teamEvolver 用户的映射关系。

映射格式为：

```
integration_id (agent_id) + external_subject -> team_evolver_user_id
```

未映射的主体调用 Context API 或 Session 上报时会收到 `403 SUBJECT_NOT_MAPPED` 错误。

代码实现：`teamEvolver/proxy/users_admin.py` (`resolve_agent_subject_user_id`, `sync_agent_subject_mappings`)

## 步骤 5：实现会话上报

最小接入的核心功能。每次 Agent 会话结束后，将完整轨迹上报到 teamEvolver。

### Python 代码示例（最小接入）

当前协议（v2 payload）：

```python
import os
import requests

base = os.environ["TEAMEVOLVER_URL"]
headers = {"Authorization": "Bearer " + os.environ["TEAMEVOLVER_TENANT_TOKEN"]}
user_id = "alice"
response = requests.post(base + "/ingest_session", headers=headers, timeout=60, json={
    "schema_version": "teamevolver.agent-session.v2",
    "protocol_version": "2.0",
    "session_id": "task-001",
    "runtime": {"type": "custom"},
    "runtime_context": {"user_id": user_id},
    "turns": [{"turn_num": 1, "prompt_text": "Analyze costs", "response_text": "Report ready."}],
})
response.raise_for_status()
print(response.json())
```

兼容窗口内的 V1 payload 参考：

```python
import os
import requests

TEAMEVOLVER_URL = "http://<teamevolver-host>:52010"
AGENT_TOKEN = "tevt_<token>"   # 租户机器凭证（Agent 数据面唯一的机器凭证）

def ingest_session(session_id: str, external_subject: str, turns: list, metrics: dict):
    payload = {
        "schema_version": "teamevolver.agent-session.v1",
        "protocol_version": "1.0",
        "session_id": session_id,
        "runtime": {
            "type": "my-agent",
            "integration_id": "my-agent:prod",
            "version": "1.0.0",
            "protocol_version": "1.0"
        },
        "runtime_context": {
            "external_subject": external_subject
        },
        "turns": turns,
        "metrics": metrics,
        "source_materials": []
    }

    resp = requests.post(
        f"{TEAMEVOLVER_URL}/ingest_session",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()

turns = [
    {
        "turn_num": 1,
        "prompt_text": "帮我写一个排序函数",
        "response_text": "好的，这是一个快速排序的实现...",
        "messages": [],
        "tool_calls": [],
        "tool_results": [],
        "injected_skills": [],
        "used_skills": [],
        "modified_skills": [],
        "metrics": {
            "input_tokens": 150,
            "output_tokens": 300
        }
    }
]

result = ingest_session(
    session_id="sess-abc123",
    external_subject="user-001",
    turns=turns,
    metrics={"interaction_turns": 1}
)
print(result)
```


检索 Context 使用 POST `/internal/agents/context/resolve`，JSON 为 `{"user_id":"alice","query":"..."}`。
read、forget、append、commit 即使已有 ref/session ID 也必须携带 user_id。
完整字段见 [Context API](../api/04-context-workspace.md)。

团队 Skill 使用 [认证 pull](../api/06-skill-sync.md)。需要真实评测时，部署 Python
[ReplayAdapterFactory](../api/05-replay-branch.md)，封装现有 Agent API，并保持每个分支独立连续会话。
客户 Agent 无需实现裁判和渐进披露协议。

以下章节保留 V1 时期的完整接入详细参考，供兼容窗口内使用。

完整接入需要调用 Context Workspace API，在用户发起请求时先获取相关的 Memory 和 Skill 上下文。每条请求都必须声明 `integration_id`（`describe`/`skills` 为 Query 参数，其余 POST 路由为 Body 字段）。

### Python 代码示例：解析上下文

```python
def resolve_context(external_subject: str, query: str, context_session_id: str = ""):
    payload = {
        "external_subject": external_subject,
        "integration_id": "my-agent:prod",
        "query": query,
        "scopes": ["personal_memory", "team_memory", "team_skills"],
        "max_items": 12,
        "max_chars": 16000
    }
    if context_session_id:
        payload["context_session_id"] = context_session_id

    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/resolve",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json=payload,
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()

result = resolve_context("user-001", "如何处理数据库连接错误？")
for item in result["items"]:
    print(f"[{item['scope']}] {item['title']}: {item['l0'][:100]}...")
```

### 读取完整内容

```python
def read_context(context_ref: str, level: str = "full"):
    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/read",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={"integration_id": "my-agent:prod", "context_ref": context_ref, "level": level},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()
```

Context Workspace API 实现：`teamEvolver/proxy/agent_context.py`

## 步骤 7（可选）：实现 Context Session

Context Session 用于将上下文库使用情况与 OpenViking Session 关联，支持精确的使用归因。

```python
def start_context_session(external_subject: str, external_session_id: str):
    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/sessions/start",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "external_subject": external_subject,
            "integration_id": "my-agent:prod",
            "external_session_id": external_session_id
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()["context_session_id"]

def commit_context_session(context_session_id: str, used_refs: list[str]):
    resp = requests.post(
        f"{TEAMEVOLVER_URL}/internal/agents/context/sessions/commit",
        headers={
            "Authorization": f"Bearer {AGENT_TOKEN}",
            "Content-Type": "application/json"
        },
        json={
            "integration_id": "my-agent:prod",
            "context_session_id": context_session_id,
            "used_context_refs": used_refs
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()
```

## 步骤 8（可选）：实现 Replay Turn 端点

teamEvolver 在验证候选 Skill 时，会调用 Agent 注册的 `replay_url` 执行 baseline 和 candidate 两个分支。推荐使用**服务端驱动的 Turn 协议**：Agent 注册 `orchestration: "server_driven"`，teamEvolver 每个交互轮次调用一次 turn 端点，多轮循环、Checklist 评审和指标聚合由服务端完成。

Turn 端点需要：

1. 接收 POST 请求（`teamevolver.replay-turn-request.v1`），包含 `request_id`（Session 句柄，相同 `request_id` 的后续轮次必须续接同一回放会话而不是重置）、`turn_num`、`branch`（baseline/candidate）、`prompt`（本轮指令）、`history`（此前各轮记录 `[{turn_num, prompt, response}]`）、`limits.turn_timeout_seconds`（本轮超时）；第 1 轮还会附带 `context_snapshot`、`skill`、`materials`、`tool_policy`。
2. 在隔离环境中执行本轮任务，不得访问生产数据或产生外部副作用。
3. 返回本轮结果（`teamevolver.replay-turn-result.v1`）：`final_response`、`messages`（本轮完整消息轨迹，服务端 Checklist Judge 唯一可见的证据），以及 fail-closed 的 `metrics`——`tool_call_count` 和 `total_tokens` 必须是非负整数，缺失或非法时该轮校验失败。
4. 对于无法确定性重放的外部工具调用，返回 `status: "unsupported"`（fail-closed，不得回退到实时调用）。

### 快速方式：复用现成的 Turn 服务

仓库自带 `scripts/replay_turn_server.py`，无需自己实现 HTTP 和协议层：

```bash
python scripts/replay_turn_server.py --port 8010 --api-key <secret>
```

- 路由：`POST /turn/<runtime_type>`（teamEvolver 每轮调用一次）和 `GET /health`（探活）
- 每个 `runtime_type` 对应 `AGENT_HANDLERS` 字典中的一个处理函数；脚本按 `request_id` 维护每会话历史并作为 `req["history"]` 传给处理函数，无状态 Agent 也能续接任务
- 处理函数抛出 `ReplayUnsupportedError` 时，脚本返回 `status: "unsupported"`
- teamEvolver 侧导出 `TEAMEVOLVER_AGENT_<AUTH_PROFILE>_REPLAY_API_KEY=<secret>`，与 `--api-key` 传入相同密钥

### 自行实现 Turn 端点示例

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/api/teamevolver/replay/turn")
def handle_replay_turn():
    body = request.json
    request_id = body["request_id"]
    turn_num = body["turn_num"]
    branch = body["branch"]
    prompt = body["prompt"]
    history = body.get("history", [])      # 此前各轮：[{turn_num, prompt, response}]
    turn_timeout = body["limits"]["turn_timeout_seconds"]
    # 第 1 轮额外携带：context_snapshot、skill、materials、tool_policy

    if branch not in ("baseline", "candidate"):
        return jsonify({"error": "invalid branch"}), 400

    try:
        result = run_replay_turn(
            request_id=request_id,         # 作为会话句柄续接执行
            turn_num=turn_num,
            prompt=prompt,
            history=history,
            turn_timeout_seconds=turn_timeout
        )
        return jsonify({
            "schema_version": "teamevolver.replay-turn-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "turn_num": turn_num,
            "branch": branch,
            "status": "succeeded",
            "final_response": result["response"],
            "messages": result["messages"],
            "metrics": {
                "tool_call_count": result["tool_calls"],
                "total_tokens": result["tokens"]
            }
        })
    except ReplayExternalToolError:
        return jsonify({
            "schema_version": "teamevolver.replay-turn-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "turn_num": turn_num,
            "branch": branch,
            "status": "unsupported",
            "error": {
                "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
                "message": "external tool call cannot be deterministically replayed",
                "retryable": False
            },
            "metrics": {}
        })
```

Replay 适配器实现：`team_replay/adapters.py:TurnBasedReplayAdapter`

## 步骤 9（可选）：实现 Skill Sync

Skill Sync 有两种模式：

### 拉取模式（简单）

定期调用 `GET /internal/agents/context/skills` 获取最新团队技能清单，与本地缓存对比后按需下载完整 Skill Bundle。

```python
def sync_skills(external_subject: str):
    resp = requests.get(
        f"{TEAMEVOLVER_URL}/internal/agents/context/skills",
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        params={"external_subject": external_subject, "scope": "team", "integration_id": "my-agent:prod"},
        timeout=30
    )
    resp.raise_for_status()
    return resp.json()
```

### 推送模式（实时）

暴露 webhook 端点接收 teamEvolver 的 Skill 变更通知，返回确认信息：

```python
@app.post("/api/teamevolver/skill-sync")
def handle_skill_sync():
    body = request.json
    event_id = body["event_id"]
    action = body["action"]
    skills = body["skills"]
    tenant_ids = body.get("tenant_ids", [])

    for skill in skills:
        if action == "publish":
            download_and_apply_skill(skill)
        elif action == "delete":
            remove_skill(skill["name"])

    return jsonify({
        "ok": True,
        "results": {
            tid: {
                "verification": {
                    "skills": [
                        {
                            "name": s["name"],
                            "matched": True,
                            "actual_version": s["version"],
                            "actual_sha256": s["sha256"]
                        }
                        for s in skills
                    ]
                }
            }
            for tid in tenant_ids
        }
    })
```

Skill Sync 实现：`teamEvolver/integrations/skill_sync_adapters.py`

## 最小接入 vs 完整接入代码示例总结

| 特性 | 最小接入代码量 | 完整接入代码量 |
|------|--------------|--------------|
| 注册 | ~20 行 | ~50 行（含所有 capability 声明） |
| 凭证管理 | 从运维获取一个 `tevt_` 字符串并存储 | 从运维获取一个 `tevt_` 字符串并存储 |
| Session 上报 | ~40 行/会话 | ~40 行/会话（加上 context_usage） |
| Context Workspace | 不需要 | ~100 行（resolve + read + session 生命周期） |
| Replay Turn 处理函数 | 不需要 | 一个函数（填入 `scripts/replay_turn_server.py` 的 `AGENT_HANDLERS`，HTTP 与协议层由脚本提供） |
| Skill Sync | 不需要 | ~50 行（拉取模式）或 ~80 行（推送 webhook） |
| Memory 写入 | 不需要 | ~20 行（remember/forget） |

建议先完成最小接入并验证数据正确回流后，再逐步实现 Context Workspace 和 Replay 功能。

## 测试验证

接入完成后，使用以下命令验证各环节：

```bash
# 健康检查
curl -fsS "http://<teamevolver-host>:52010/health"

# 验证 Agent 注册状态
curl "http://<teamevolver-host>:52010/api/agent-integrations"

# 触发一次进化周期
curl -X POST "http://<teamevolver-host>:52010/trigger"

# 查看队列中的 Session
curl "http://<teamevolver-host>:52010/sessions?limit=5"
```

相关测试用例可参考：`tests/test_agent_registry.py`、`tests/test_agent_protocol.py`、`tests/test_agent_context_workspace.py`、`tests/test_replay_adapters.py`。
