# Pi Agent / AgentsHub 接入

Pi Agent 与 AgentsHub 按 [协议 v2](./06-protocol-v2.md) 接入。Session 的 `runtime.type` 使用实际运行时标签（如 `pi`、`agentshub`），只用于分析与 Replay 选择，不参与鉴权。

## 数据面

| 用途 | 接口 | 说明 |
| --- | --- | --- |
| Session 上报 | `POST /ingest_session` | 任务结束后上报；`runtime_context.user_id` 为 AgentsHub 用户 |
| Context | `/internal/agents/context/*` | 模型调用前 `resolve`，按需 `read`；`remember` 写入个人 Memory |
| Skill 拉取 | `GET /sync/skills` | 用 `ETag` 增量更新，把 bundle 安装到 Pi 的 Skill 目录 |

请求格式与错误码见 [Session 上报](../api/03-session-ingest.md)、[Agent Context](../api/04-context-workspace.md)、[Skill 拉取](../api/06-skill-sync.md)。

## Runtime Context

```json
{
  "runtime": {"type": "pi", "version": "1.4.0"},
  "runtime_context": {"user_id": "alice"},
  "turns": [{
    "turn_num": 1,
    "prompt_text": "...",
    "response_text": "...",
    "tool_calls": [],
    "context_usage": {"context_snapshot_id": "ctxsnap_...", "skill_refs": [{"context_ref": "ctx_...", "operation": "injected"}]}
  }]
}
```

`context_usage` 中的引用必须来自同一 `user_id` 的 `resolve` / `skills` 响应，15 分钟内有效。

## True Replay

Pi 的 True Replay 由部署方编写的 Replay Adapter 实现，见 [Replay Adapter](../api/05-replay-branch.md#replay-adapter)。实现要求：

- 每个分支创建独立的 Pi 子进程、会话与工作区，网络、模型、工具与初始材料一致，唯一差别是安装的 Skill bundle（`context.treatment.skill`）。
- 每个分支的 Skill bundle 安装到该分支自己的工作区，不写入共享目录。
- 无法确定性重放的外部副作用工具直接失败（抛出 `ReplayUnsupported`），不回退到真实调用。
- `send()` 返回本轮完整的 assistant / tool 消息与真实观测到的 `tool_call_count`、`total_tokens`。

Adapter 可以使用 `TurnBasedReplayAdapter` 调用 AgentsHub 提供的逐轮 HTTP 端点；端点认证 Key 通过 `TEAMEVOLVER_AGENT_<PROFILE>_REPLAY_API_KEY` 提供给 TeamEvolver Worker。
