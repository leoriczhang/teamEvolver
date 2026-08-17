# Pi Agent 接入指南

本文档描述 Pi Agent 与 teamEvolver 的集成方式。Pi 是一个基于子进程的命令行 Coding Agent，通过 `pi` CLI 执行任务，支持 bash 命令执行、文件读写、内置工具和 MCP 工具。Pi 是 teamEvolver 的首批完整接入运行时之一，支持 Protocol V1 的全部核心能力：Session 上报、Context Workspace、True Replay 分支执行和 Skill Sync。

## Pi Agent 概述

Pi Agent 通过子进程方式运行 Agent 循环（`pi_rpc_worker.py` 中使用 `posix_spawn + setsid` 启动，避免 fork/vfork 不稳定问题），核心特征：

- **运行模式**：子进程 CLI Agent，通过 RPC 与宿主通信
- **工具集**：`bash`（终端命令）、`file`（文件读写）、`builtin`（内置工具）、`mcp`（MCP 工具）
- **沙箱模型**：systemd-user 级别隔离，使用 `PrivateNetwork=yes` 网络命名空间 + Unix Socket Model Broker
- **进程启动**：`posix_spawn + setsid` 确保稳定启动，避免段错误
- **外部工具重放策略**：fail-closed（不可确定性重放的外部调用直接拒绝，不回退实时调用）

集成参考代码位于（宿主侧）：

```
/home/zhangpengkun/AgentsHub/backend/app/integrations/team_evolver.py
/home/zhangpengkun/AgentsHub/backend/app/integrations/team_evolver_replay.py
/home/zhangpengkun/AgentsHub/backend/app/core/pi_agent.py
```

## 声明的能力

Pi Agent 在注册时声明以下 V1 能力：

| 能力 ID | 说明 |
|---------|------|
| `session.ingest.v1` | 通过异步队列上报完整 Session 轨迹（async-job delivery） |
| `context.workspace.v1` | 使用 Context Workspace 获取个人/团队 Memory 和 Skill 上下文 |
| `replay.branch.v1` | 支持 True Replay HTTP 分支执行，外部工具 fail-closed |
| `skill.sync.v1` | 接收 Skill 发布/回滚 HTTP webhook 推送 |
| `memory.personal.read.v1` | 读取个人 Memory |
| `memory.personal.write.v1` | 写入个人 Memory |
| `memory.team.read.v1` | 读取团队 Memory |
| `skill.personal.read.v1` | 读取个人 Skill |
| `skill.team.read.v1` | 读取团队 Skill |
| `skill.team.evolve.v1` | 参与团队 Skill 进化 |
| `skill.bundle.v1` | 支持 `bundle_v1` 格式的 Skill Bundle 安装 |

## 注册流程

### 1. 获取控制面密钥

Pi Agent 部署时配置环境变量 `EVOLVE_INGEST_API_KEY`，与 teamEvolver 服务的控制面注册密钥一致。

### 2. 发送注册请求

```
POST /internal/agents/register
Authorization: Bearer <EVOLVE_INGEST_API_KEY>
Content-Type: application/json
```

注册 payload 示例：

```json
{
  "schema_version": "teamevolver.agent-registration.v1",
  "protocol_version": "1.0",
  "agent_id": "pi:<tenant-id>",
  "runtime_type": "pi",
  "runtime_class": "pi",
  "runtime_version": "<pi-version>",
  "display_name": "Pi Agent",
  "capabilities": {
    "session.ingest.v1": {"delivery": "async-job"},
    "context.workspace.v1": {
      "scopes": [
        "personal_memory", "team_memory",
        "personal_skills", "team_skills"
      ],
      "operations": [
        "resolve", "read", "skills",
        "remember", "forget", "session"
      ]
    },
    "replay.branch.v1": {
      "transport": "http",
      "endpoint": "https://<pi-host>/api/internal/team-evolver/replay",
      "max_interactions": 20,
      "supports_materials": true,
      "supports_artifacts": true,
      "supports_full_trace": true,
      "idempotent": false,
      "runtime": "pi",
      "sandbox": "systemd-user",
      "network_policy": "private-network+unix-broker",
      "external_tool_replay": "fail-closed",
      "tools": ["bash", "file", "builtin", "mcp"]
    },
    "skill.sync.v1": {
      "transport": "http",
      "endpoint": "https://<pi-host>/api/internal/team-evolver/sync"
    },
    "memory.personal.read.v1": {},
    "memory.personal.write.v1": {},
    "memory.team.read.v1": {},
    "skill.personal.read.v1": {},
    "skill.team.read.v1": {},
    "skill.team.evolve.v1": {},
    "skill.bundle.v1": {"formats": ["bundle_v1"]}
  },
  "endpoints": {
    "health_url": "https://<pi-host>/api/health",
    "replay_url": "https://<pi-host>/api/internal/team-evolver/replay",
    "skill_sync_url": "https://<pi-host>/api/internal/team-evolver/sync"
  },
  "auth": {"replay_profile": "pi"},
  "metadata": {
    "tenant_id": "<tenant-id>",
    "platform": "linux",
    "tools": ["bash", "file", "builtin", "mcp"]
  },
  "subject_mappings_authoritative": true,
  "subject_mappings": []
}
```

### 3. 存储访问令牌

注册成功后，teamEvolver 返回 `credentials.agent_access_token`。Pi Agent 需要持久化存储此令牌，后续所有 Agent API 调用均使用此令牌认证。令牌格式为 `tev1_<random>`，teamEvolver 端仅存储 SHA-256 哈希。

相关代码：`teamEvolver/integrations/agent_registry.py:221` (`issue_agent_access_token`)

### 4. 配置 Replay API Key

teamEvolver 调用 Pi Replay 端点时，通过环境变量查找认证密钥：

```bash
TEAMEVOLVER_AGENT_PI_REPLAY_API_KEY=<key>
```

Skill Sync API Key 通过以下环境变量配置：

```bash
TEAMEVOLVER_AGENT_PI_SKILL_SYNC_API_KEY=<key>
```

## Session 上报

Pi Agent 在每个会话结束后通过异步任务队列上报完整 Session 轨迹。上报内容包括：

- 完整消息序列（用户消息、Agent 回复、工具调用和结果）
- 每轮交互详情（prompt、response、tool_calls、token 消耗）
- Agent Event 日志
- Runtime Context（主体标识、源材料、沙箱快照）
- Session 元数据（模型配置、工具列表、Skill 使用情况）

Session 上报使用 `POST /api/agent/sessions`，使用注册时获取的 `agent_access_token` 认证，通过 durable outbox 保证至少一次投递，失败时退避重试。

## Runtime Context 构建

Pi 在上报 Session 时，在 `runtime_context` 中提供以下字段：

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `external_subject` | string | 是 | 用户唯一标识，格式为 `<tenant>:<user-id>`，用于主体映射 |
| `tenant_id` | string | 推荐 | 租户 ID，用于多租户隔离 |
| `user_id` | string | 推荐 | 用户在租户内的 ID |
| `source_materials` | array | 可选 | 本次会话使用的源材料列表（仓库、文件等） |
| `sandbox_snapshot` | object | 可选 | 沙箱快照信息（镜像、workspace hash），用于 True Replay 隔离 |

示例：

```json
{
  "runtime_context": {
    "external_subject": "tenant-a:user-42",
    "tenant_id": "tenant-a",
    "user_id": "user-42",
    "source_materials": [
      {
        "type": "repository",
        "uri": "https://github.com/example/repo",
        "ref": "main",
        "sha": "abc123"
      }
    ],
    "sandbox_snapshot": {
      "image": "pi/sandbox:v2",
      "workspace_hash": "def456"
    }
  }
}
```

主体映射需要在 teamEvolver 管理后台预先配置，将 `external_subject` 映射到 teamEvolver 用户 ID。注册时也可通过 `subject_mappings` 字段批量同步映射关系（`subject_mappings_authoritative: true` 表示由 Pi 侧权威提供映射）。

## Context Workspace 集成

Pi Agent 在每轮交互开始前，通过 Context Workspace API 拉取上下文：

1. **调用** `GET /api/agent/context/resolve?external_subject=<id>` 获取当前用户的上下文投影
2. **注入**：将团队/个人 Memory 条目和已发布的团队 Skill Bundle 注入 Agent 系统提示或工具上下文
3. **回写**：会话结束后，通过 `POST /api/agent/context/session` 提交本轮 Session 的 Context 使用快照，用于后续进化分析
4. **Memory 写入**：对有价值的用户偏好或知识，通过 `POST /api/agent/context/remember` 写入个人 Memory

相关代码：`teamEvolver/proxy/agent_context.py`

## Pi True Replay 实现

Pi Agent 实现了 Protocol V1 级别的 True Replay，核心特征是 **external_tool_replay=fail-closed**。

### 沙箱隔离

Pi 的 Replay Worker 通过 systemd-run 启动，启用以下隔离：

| 隔离维度 | systemd 指令 | 说明 |
|---------|-------------|------|
| 网络命名空间 | `PrivateNetwork=yes` | Worker 无独立网络栈，仅能通过 Unix Socket 连接 Model Broker |
| 文件系统 | `ProtectSystem=strict`、`ProtectHome=yes` | 系统目录只读，真实 HOME 不可见 |
| 可写路径 | `ReadWritePaths={sandbox_home}` | 仅沙箱临时目录可写 |
| 进程启动 | `posix_spawn + setsid` | 避免 fork/vfork 段错误，确保进程组独立 |
| 凭证隔离 | 受保护 Unix Socket | 模型 API Key 通过 Model Broker 短期凭证代理，Worker 不直接持有 |

### 外部工具重放策略

1. **Workspace 本地工具**（文件读写、bash 命令、代码搜索等）在分支沙箱内真实执行，不做拦截。
2. **网络可达工具**（HTTP 请求、数据库访问、外部 API）：
   - 如果原始会话中该工具有录制的返回结果，Pi 按调用序列确定性重放结果；
   - 如果遇到未录制的外部工具调用，Pi **不**回退到实时调用，直接返回 `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` 错误，该 case 标记为不可重放（fail-closed）。
3. **确定性上下文**：Replay 接收 teamEvolver 下发的 `frozen_context`（冻结的上下文投影），不执行新的上下文解析。
4. **模型凭证代理**：Replay Worker 内的模型请求通过 ReplayModelSidecar → Unix Socket → ReplayModelBroker 转发，Broker 持有真实 API Key 并以 streaming 模式透传响应。

### Replay 结果格式

Pi Replay 端点返回的结果包含完整 trace 信息：

```json
{
  "schema_version": "teamevolver.replay-branch-result.v1",
  "protocol_version": "1.0",
  "request_id": "replay_<hash>",
  "branch": "candidate",
  "status": "succeeded",
  "metrics": {
    "interaction_turns": 3,
    "tool_call_count": 8,
    "total_tokens": 6200,
    "api_calls": 3,
    "input_tokens": 5400,
    "output_tokens": 800
  },
  "output": {
    "final_response": "..."
  },
  "trace": {
    "messages": [],
    "events": [],
    "interactions": []
  },
  "context_input_hash": "<sha256-of-frozen-context>",
  "runtime_checklist_report": {},
  "elapsed_seconds": 52.3
}
```

### Skill Bundle 安装

Replay 的 Candidate 分支中，待验证的 Skill Bundle 被安装到隔离沙箱的 Skill 目录下（`~/.pi/skills/<name>/`），不影响已发布版本。Baseline 分支加载当前已发布的 Skill 版本。两个分支的 Skill Bundle 完全独立。

## Skill Sync

teamEvolver 在 Skill 发布或回滚时，通过 HTTP webhook 向 Pi 的 `skill_sync_url` 推送变更通知。Pi 侧收到通知后：

1. 拉取最新的 Skill Bundle
2. 更新本地 Skill 缓存
3. 热加载到运行中的 Agent（无需重启）

推送通过 durable outbox 保证可靠投递，支持 per-tenant 过滤、退避重试和死信队列。

## 相关代码路径

| 文件 | 说明 |
|------|------|
| `teamEvolver/integrations/agent_protocol.py` | Protocol V1 线协议常量与校验 |
| `teamEvolver/integrations/agent_registry.py` | Agent 注册与令牌管理 |
| `teamEvolver/integrations/replay_adapters.py` | Replay HTTP 适配器 |
| `teamEvolver/integrations/skill_sync_adapters.py` | Skill Sync webhook 推送 |
| `teamEvolver/proxy/agent_context.py` | Context Workspace API 实现 |
| `teamEvolver/true_replay.py` | True Replay 核心执行引擎 |
| `teamEvolver/progressive_replay.py` | 渐进披露与 Checklist 决策 |
| `teamEvolver/dreamcycle/memory_replay.py` | Memory True Replay 执行器 |
| `teamEvolver/integrations/replay_model_broker.py` | Replay Model Broker（Unix Socket 凭证代理） |
| `AgentsHub/backend/app/integrations/team_evolver.py` | Pi 侧集成参考实现（注册、Session 上报、Sync 接收） |
| `AgentsHub/backend/app/integrations/team_evolver_replay.py` | Pi 侧 Replay 分支执行与隔离 |
| `AgentsHub/backend/app/core/pi_agent.py` | Pi Agent Runtime 核心（子进程管理、RPC、工具循环） |
| `AgentsHub/backend/app/core/pi_rpc_worker.py` | Pi RPC Worker（posix_spawn + setsid 启动） |
