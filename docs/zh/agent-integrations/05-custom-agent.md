# 自定义 Agent 接入

本页用 Python 示例说明如何把任意 Agent 运行时接入 TeamEvolver。协议约定见 [协议 v2](./06-protocol-v2.md)。

## 接入步骤

### 最小接入

1. 从管理员处获取服务地址和租户 Key。
2. 任务结束后调用 `POST /ingest_session` 上报 Session。

### 完整接入

3. 模型调用前用 Agent Context 检索上下文，并在 Session 中记录 `context_usage`。
4. 模型调用前用 `GET /sync/skills` 拉取团队 Skill。
5. 需要 True Replay 时，部署方编写 Replay Adapter，或提供逐轮 HTTP 端点。

## 步骤 1：配置凭证

```bash
export AGENT_TEAMEVOLVER_URL="https://teamevolver.example.com"
export TEAMEVOLVER_TENANT_TOKEN="tevt_..."
```

租户 Key 代表整个租户，持有者可以代表租户内任意 `user_id` 调用数据面接口。只在 Agent 服务端保存，不下发到浏览器或终端用户设备。

## 步骤 2：上报 Session

```python
import os
import httpx

BASE = os.environ["AGENT_TEAMEVOLVER_URL"]
HEADERS = {"Authorization": f"Bearer {os.environ['TEAMEVOLVER_TENANT_TOKEN']}"}


def report_session(session_id: str, user_id: str, turns: list[dict]) -> dict:
    envelope = {
        "schema_version": "teamevolver.agent-session.v2",
        "protocol_version": "2.0",
        "session_id": session_id,
        "runtime": {"type": "my-agent", "version": "1.0.0"},
        "runtime_context": {"user_id": user_id},
        "turns": turns,
    }
    response = httpx.post(f"{BASE}/ingest_session", json=envelope, headers=HEADERS, timeout=30)
    response.raise_for_status()
    return response.json()  # 202: {"accepted": true, "job_id": ..., "status_url": ..., "dedupe": false}


report_session("task-001", "alice", [
    {"turn_num": 1, "prompt_text": "分析本月成本", "response_text": "成本报告已完成。",
     "tool_calls": [], "metrics": {"input_tokens": 520, "output_tokens": 890}},
])
```

上报是异步的：返回 202 表示已持久化受理。重复上报同一 `session_id` 返回第一次的受理结果。网络失败时可以安全重试。

## 步骤 3：检索上下文

```python
def resolve_context(user_id: str, query: str) -> dict:
    response = httpx.post(f"{BASE}/internal/agents/context/resolve", headers=HEADERS, timeout=30, json={
        "user_id": user_id,
        "query": query,
        "scopes": ["team_skills", "personal_skills", "team_memory", "personal_memory"],
        "max_items": 8,
        "max_chars": 12000,
    })
    response.raise_for_status()
    return response.json()


result = resolve_context("alice", "月度成本报告")
prompt_context = "\n\n".join(f"[{i['scope']}] {i['title']}\n{i['l1'] or i['l0']}" for i in result["items"])
```

需要正文时用引用读取：

```python
def read_context(user_id: str, context_ref: str, level: str = "l2") -> dict:
    response = httpx.post(f"{BASE}/internal/agents/context/read", headers=HEADERS, timeout=30,
                          json={"user_id": user_id, "context_ref": context_ref, "level": level})
    response.raise_for_status()
    return response.json()
```

把实际注入或读取的引用写入该轮的 `context_usage`：

```python
turn["context_usage"] = {
    "context_snapshot_id": result["snapshot_id"],
    "skill_refs": [{"context_ref": i["context_ref"], "operation": "injected"}
                   for i in result["items"] if i["kind"] == "skill"],
    "memory_refs": [{"context_ref": i["context_ref"], "operation": "injected"}
                    for i in result["items"] if i["kind"] == "memory"],
}
```

引用 15 分钟后过期，请在任务结束后及时上报 Session。

## 步骤 4：Context Session（可选）

需要把对话事件写入 OpenViking Session 时：

```python
def context_session(user_id: str, external_id: str, events: list[tuple[str, str]], used_refs: list[str]):
    start = httpx.post(f"{BASE}/internal/agents/context/sessions/start", headers=HEADERS, timeout=30,
                       json={"user_id": user_id, "external_session_id": external_id}).json()
    sid = start["context_session_id"]
    for sequence, (role, content) in enumerate(events, start=1):
        httpx.post(f"{BASE}/internal/agents/context/sessions/append", headers=HEADERS, timeout=30, json={
            "user_id": user_id, "context_session_id": sid, "event_id": f"{external_id}:{sequence}",
            "sequence": sequence, "role": role, "content": content,
        }).raise_for_status()
    httpx.post(f"{BASE}/internal/agents/context/sessions/commit", headers=HEADERS, timeout=30, json={
        "user_id": user_id, "context_session_id": sid, "used_context_refs": used_refs,
    }).raise_for_status()
```

`sequence` 必须从 1 连续递增；相同 `event_id` 与相同内容重放是幂等的。

## 步骤 5：拉取团队 Skill

```python
import base64
import pathlib

_etag = ""


def pull_skills(user_id: str, target: pathlib.Path) -> None:
    global _etag
    headers = {**HEADERS, **({"If-None-Match": _etag} if _etag else {})}
    response = httpx.get(f"{BASE}/sync/skills", params={"user_id": user_id}, headers=headers, timeout=30)
    if response.status_code == 304:
        return
    response.raise_for_status()
    _etag = response.headers["ETag"]
    for skill in response.json()["skills"]:
        root = target / skill["name"]
        for item in skill["files"]:
            path = root / item["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(base64.b64decode(item["content_b64"]))
```

团队 Skill 只通过拉取交付，服务端不推送。

## 步骤 6：True Replay（可选）

TeamEvolver 不直接调用 Agent 的私有接口。部署方编写一个 Replay Adapter（Python 文件），由服务 Root Key 写入 PostgreSQL、租户管理员绑定，接口为 `open(context) -> session`、`session.send(message)`、`session.close()`，见 [Replay Adapter](../api/05-replay-branch.md#replay-adapter)。

Agent 已经能通过 HTTP 逐轮执行时，Adapter 可以委托给仓库提供的 `TurnBasedReplayAdapter`。Agent 侧可参考示例服务：

```bash
python scripts/replay_turn_server.py --port 8010 --api-key "$REPLAY_KEY"
```

示例服务的端点为 `POST /turn/<runtime_type>`，每次调用执行一轮，`request_id` 在同一分支内保持不变。成功时必须返回本轮完整的 `messages` 与 `metrics.tool_call_count`、`metrics.total_tokens`；遇到无法确定性重放的外部副作用时返回 `unsupported`，不回退为真实调用。

## 验证

```bash
curl "$AGENT_TEAMEVOLVER_URL/healthz"
curl -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  "$AGENT_TEAMEVOLVER_URL/internal/agents/context/describe?user_id=alice"
curl -i -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  "$AGENT_TEAMEVOLVER_URL/sync/skills?user_id=alice"
```

上报 Session 后，在控制台 Session 页面确认记录出现，或由管理员用 `/api/sessions/{session_id}/stages` 查看处理阶段。
