# 自定义 Agent 接入指南

本文档提供从零开始将自定义 Agent 接入 teamEvolver 的分步指南。包含最小接入和完整接入两种模式的代码示例。

## 接入步骤总览

### 最小接入（会话回流）

1. 获取控制面密钥 `EVOLVE_INGEST_API_KEY`
2. 通过 `/internal/agents/register` 注册 Agent，声明 `session.ingest.v1` 能力
3. 安全存储返回的 `agent_access_token`
4. 配置主体映射（管理界面或注册 payload 中的 `subject_mappings`）
5. 实现会话数据上报（`POST /ingest_session`）

### 完整接入（Context + Replay + Skill Sync）

在最小接入基础上继续实现：

6. 实现 Context Workspace 调用（resolve/read/skills）
7. 实现 Context Session 生命周期管理（start/append/commit）
8. 暴露 Replay HTTP 端点供 teamEvolver 回调
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
        "endpoint": "https://my-agent.example.com/api/teamevolver/replay",
        "max_interactions": 10,
        "supports_materials": true,
        "supports_artifacts": false,
        "supports_full_trace": true,
        "idempotent": true,
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

## 步骤 3：存储访问令牌

注册成功响应示例：

```json
{
  "agent_id": "my-agent:prod",
  "runtime_type": "my-agent",
  "status": "active",
  "credentials": {
    "agent_access_token": "tev1_<random-token>"
  },
  "capabilities": ["session.ingest.v1"],
  "created_at": "2024-01-01T00:00:00Z"
}
```

`agent_access_token` 仅在首次注册或显式轮换时返回一次。必须安全存储（如密钥管理服务、环境变量、加密配置文件）。teamEvolver 服务端仅存储其 SHA-256 哈希，无法找回丢失的令牌。令牌丢失时需要重新注册或通过管理 API 轮换。

后续所有 Agent API 调用均使用此令牌认证：

```
Authorization: Bearer tev1_<random-token>
```

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

```python
import requests
import json

TEAMEVOLVER_URL = "http://<teamevolver-host>:52010"
AGENT_TOKEN = "tev1_<token>"

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

代码入口：`teamEvolver/proxy/routes.py:2971` (`ingest_session`)

## 步骤 6（可选）：实现 Context Workspace

完整接入需要调用 Context Workspace API，在用户发起请求时先获取相关的 Memory 和 Skill 上下文。

### Python 代码示例：解析上下文

```python
def resolve_context(external_subject: str, query: str, context_session_id: str = ""):
    payload = {
        "external_subject": external_subject,
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
        json={"context_ref": context_ref, "level": level},
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
            "context_session_id": context_session_id,
            "used_context_refs": used_refs
        },
        timeout=10
    )
    resp.raise_for_status()
    return resp.json()
```

## 步骤 8（可选）：暴露 Replay 端点

teamEvolver 在验证候选 Skill 时，会向 Agent 注册的 `replay_url` 发送 HTTP 请求，要求在隔离环境中执行 baseline 和 candidate 两个分支。

Replay 端点需要：

1. 接收 POST 请求，包含 `request_id`、`branch`（baseline/candidate）、`case.query`（任务指令）、`frozen_context`（冻结上下文）、`limits`（超时和轮次限制）。
2. 在隔离沙箱中执行任务，不得访问生产数据或产生外部副作用。
3. 返回包含 `interaction_turns`、`tool_call_count`、`total_tokens` 指标的结果，以及 `context_input_hash`。
4. 对于无法确定性重放的外部工具调用，返回 `REPLAY_EXTERNAL_TOOL_UNSUPPORTED`。

### Replay 请求处理框架示例

```python
from flask import Flask, request, jsonify

app = Flask(__name__)

@app.post("/api/teamevolver/replay")
def handle_replay():
    body = request.json
    request_id = body["request_id"]
    branch = body["branch"]
    case = body["case"]
    limits = body["limits"]
    frozen_context = body.get("context_snapshot", {})

    if branch not in ("baseline", "candidate"):
        return jsonify({"error": "invalid branch"}), 400

    try:
        result = run_replay_branch(
            branch=branch,
            instruction=case["query"],
            frozen_context=frozen_context,
            timeout_seconds=limits["timeout_seconds"],
            max_interactions=limits["max_interactions"]
        )
        return jsonify({
            "schema_version": "teamevolver.replay-branch-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "branch": branch,
            "status": "succeeded",
            "metrics": {
                "interaction_turns": result["turns"],
                "tool_call_count": result["tool_calls"],
                "total_tokens": result["tokens"]
            },
            "output": {"final_response": result["response"]},
            "trace": {"messages": result["messages"], "interactions": []},
            "context_input_hash": result["context_hash"],
            "elapsed_seconds": result["elapsed"]
        })
    except ReplayExternalToolError:
        return jsonify({
            "schema_version": "teamevolver.replay-branch-result.v1",
            "protocol_version": "1.0",
            "request_id": request_id,
            "branch": branch,
            "status": "unsupported",
            "error": {
                "code": "REPLAY_EXTERNAL_TOOL_UNSUPPORTED",
                "message": "external tool call cannot be deterministically replayed",
                "retryable": False
            },
            "metrics": {},
            "elapsed_seconds": 0
        })
```

Replay 适配器实现：`teamEvolver/integrations/replay_adapters.py`

## 步骤 9（可选）：实现 Skill Sync

Skill Sync 有两种模式：

### 拉取模式（简单）

定期调用 `GET /internal/agents/context/skills` 获取最新团队技能清单，与本地缓存对比后按需下载完整 Skill Bundle。

```python
def sync_skills(external_subject: str):
    resp = requests.get(
        f"{TEAMEVOLVER_URL}/internal/agents/context/skills",
        headers={"Authorization": f"Bearer {AGENT_TOKEN}"},
        params={"external_subject": external_subject, "scope": "team"},
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
| 令牌管理 | 存储一个字符串 | 存储一个字符串 |
| Session 上报 | ~40 行/会话 | ~40 行/会话（加上 context_usage） |
| Context Workspace | 不需要 | ~100 行（resolve + read + session 生命周期） |
| Replay 端点 | 不需要 | ~200+ 行（沙箱隔离 + 确定性重放） |
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
