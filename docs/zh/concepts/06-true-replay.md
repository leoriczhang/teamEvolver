# True Replay

True Replay 是 teamEvolver 的核心验证机制：在隔离的真实 Agent 运行时中**并行执行** Baseline 和 Candidate 两个分支，基于真实工具循环的执行结果进行完成性判定和效率比较，而非模拟测试、文本评审或 A/B 打分。

True Replay 的根本原则：**Candidate 必须在真实运行时中证明自己不比 Baseline 差**。

## Baseline 与 Candidate 分支

每个 Replay Case 同时启动两个独立分支，唯一变量是是否加载待验证的 Skill Candidate：

| 分支 | 加载内容 | 作用 |
|------|---------|------|
| **Baseline** | 当前已发布的团队 Skill（或不加载 Candidate） | 对照组，建立当前版本的真实执行基线 |
| **Candidate** | 待验证的 Skill Candidate 版本 | 实验组，测试变更后的执行效果 |

两个分支共享完全相同的：
- 用户指令（query/instruction）
- 冻结的 Context 投影（通过 snapshot hash 保证一致性）
- Session Materials（用户上传的输入文件）
- Checklist 完成条件
- 执行时限（timeout_seconds）和最大交互轮次（max_interactions）
- 模型配置（通过 Replay Model Broker 注入）

分支之间互不干扰，在各自的隔离环境中独立运行完整的工具循环。

## 隔离运行时执行

True Replay 不在主服务进程中执行 Agent，而是为每个分支创建一次性的隔离运行时环境。

### 本地 Hermes 沙箱

对于本地 Hermes 运行时，每个分支获得独立的临时 HOME 目录：

- `HOME` 和 `HERMES_HOME` 重定向到临时目录（`/tmp/teamevolver-replay-*/{branch}/`），真实 `~/.hermes` 永不被触碰
- Candidate 分支在其私有 `~/.hermes/skills/{name}/` 下安装 Skill Bundle
- workspace 目录是唯一允许写入的宿主机路径
- 配置文件（`config.yaml`）写入沙箱内的 `.hermes/`，镜像真实模型配置但 API Key 替换为短期凭证
- 环境变量冻结：`TERMINAL_ENV=local`、`HERMES_YOLO_MODE=1`（自动批准，无需 TTY），`HERMES_INTERACTIVE` 和 `HERMES_GATEWAY_SESSION` 被清除

### systemd 隔离

本地沙箱通过 `systemd-run` 启动，启用以下系统级隔离：

| 隔离维度 | systemd 指令 | 说明 |
|---------|-------------|------|
| 网络命名空间 | `PrivateNetwork=yes` | 分支进程无独立网络栈，只能通过 Unix Socket 连接 Model Broker |
| 文件系统 | `ProtectSystem=strict`、`ProtectHome=yes` | 系统目录只读，真实 HOME 不可见 |
| 可写路径 | `ReadWritePaths={sandbox_home}` | 仅沙箱目录可写 |
| 只读绑定 | `BindReadOnlyPaths=...` | 仅绑定必要的只读路径（Python 解释器、teamEvolver 源码、Hermes 源码、引用的文件） |
| 权限 | `NoNewPrivileges=yes`、`RestrictSUIDSGID=yes`、`LockPersonality=yes` | 禁止提权和 SUID |
| 进程 | `ProtectProc=invisible`、`ProcSubset=pid` | 进程视图隔离 |
| 地址族 | `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | 限制 Socket 类型 |
| 超时 | `RuntimeMaxSec={timeout}s` | 强制超时终止 |

> **注意**：Candidate Skill 可能写入 `~/.hermes` 并通过 shell 命令修改配置，其 `~` 会展开为 `$HOME`，因此每个分支必须使用独立的临时 HOME 目录。Referenced source paths（只读）必须在本机存在，否则该 Case 被标记为不可运行。

### 远程 Agent Runtime

对于注册了 `replay.branch.v1` 能力的外部 Agent（如 Pi Agent），True Replay 通过 HTTP 适配器发送 Replay 请求到 Agent 的 replay endpoint，由 Agent Runtime 负责隔离执行。外部 Runtime 必须实现协议规定的隔离要求。

## 冻结 Context 与 Snapshot Hash

True Replay 使用 Context Snapshot 确保两个分支看到完全一致的上下文视图：

1. **Snapshot 加载**：从源 Session 的 `context_usage.context_snapshot_id` 加载冻结的 Context 投影
2. **Treatment 替换**：对于 Skill Replay，移除被验证 Skill 的现有版本，注入 Baseline/Candidate 各自的 Skill 内容；对于 Memory Replay，移除被变更的 Memory 条目，注入 before/after 内容
3. **Hash 校验**：每个分支的 context_snapshot 计算 `shared_context_hash` 和 `context_input_hash`，确保分支间共享上下文一致，仅 treatment 部分不同
4. **运行时注入**：冻结的 Snapshot 通过 Replay 请求传递给 Agent Runtime，Agent 必须使用收到的 Snapshot 而非实时拉取上下文

Protocol V1 要求 Agent Runtime 返回 `context_input_hash`，与服务端计算的 hash 不匹配时结果 fail-closed。

## 渐进披露协议（Progressive Disclosure）

为了避免一次性给 Agent 过多要求导致任务偏移，True Replay 采用渐进披露协议：

1. **初始可见性**：第一轮只给 Agent 用户的原始 query（`initial_visibility: query_only`），不暴露 Checklist 项
2. **批量披露**：每轮交互后，用 Checklist Judge 评估完成情况，将未满足的 Checklist 项按批次（`batch_size=4`）披露给 Agent
3. **追加提示**：披露时生成后续提示（如"第 N 轮 Checklist 检查仍有未满足项。保留已完成内容，只补齐以下要求：…"）
4. **终止条件**：所有 Checklist 项满足（`all_satisfied=true`）时停止；无更多未披露项时也停止
5. **披露内容**：每项包含 `[id]` 和具体要求文本

渐进披露确保 Agent 先自主完成任务，再针对未满足的具体要求进行修正，避免初始 prompt 过载。

## 效率比较

Checklist 门禁通过后，对 Baseline 和 Candidate 进行效率比较。比较严格按以下优先级进行（字典序比较）：

| 优先级 | 指标 | 说明 |
|--------|------|------|
| 1（主指标） | `interaction_turns` | 交互轮次数——轮次更少意味着更高效 |
| 2（次指标） | `tool_call_count` | 工具调用数——更少的工具调用意味着更少的试错 |
| 3（次指标） | `total_tokens` | 总 Token 消耗——更少的 Token 意味着更简洁的推理 |

决策规则：
- **轮次减少**（improved）：Candidate 轮次 < Baseline → **接受**
- **轮次增加**（regressed）：Candidate 轮次 > Baseline → **拒绝**
- **轮次相同**：看工具调用数
  - 工具调用减少且无其他次指标退化 → **接受**
  - 工具调用增加 → **拒绝**
- **所有指标持平**（inconclusive）：不接受也不拒绝，标记为不确定

效率比较的策略 ID 为 `true_replay_turn_priority_v2`。

> **提示**：这是一个保守策略——Candidate 必须在主指标上明确优于 Baseline，或在主指标持平时在次指标上有净改进。指标持平不算通过。

## Checklist 门禁

效率比较之前，Checklist 完成性是硬性门禁：

- **Candidate 未通过 Checklist**（`candidate_checklist_incomplete`）：直接拒绝，不看效率
- **Candidate 通过但 Baseline 未通过**（`candidate_only_completed_checklist`）：直接接受
- **双方都通过**：进入效率比较
- **双方都未通过**：拒绝

Checklist 的详细说明见 [Checklist 门禁](./07-checklist)。

## 团队 Memory True Replay

DreamCycle 产出的团队 Memory Change（合并、去重、清理后的 Memory 条目变更）同样需要经过 True Replay 验证，验证逻辑与 Skill True Replay 一致，但 Treatment 是 Memory 内容而非 Skill Bundle。

### 与 Skill True Replay 的区别

| 维度 | Skill True Replay | Memory True Replay |
|------|-------------------|-------------------|
| Treatment 变量 | 加载 Baseline Skill vs Candidate Skill Bundle | 注入变更前（before）vs 变更后（after）的 Memory 内容 |
| Baseline 来源 | 当前已发布的团队 Skill 版本 | Memory Change 的 `before_content`（变更前快照） |
| Candidate 来源 | 待发布的 Skill Candidate | Memory Change 的 `after_content`（DreamCycle 产出的合并/去重后内容） |
| Snapshot 替换 | 替换 `skill_bundles[]` 中目标 Skill 的条目 | 替换 `memory_entries[]` 中被变更 Memory 的条目 |
| 触发时机 | Skill Evolution 管线 Validate 阶段 | DreamCycle 完成合并后手动/自动触发 |
| 入口 API | `POST /api/validation/candidates/{id}/replay` | `POST /api/dreamcycle/memory-replay` |

### 执行流程

1. **加载 Memory Change**：从 `MemoryChangeLedger` 读取指定 `change_id` 的 before/after 内容和关联的源 Session
2. **选择源 Session**：优先使用指定的 `source_session_id`，否则从与该 Memory 相关的历史 Session 中选取一个有 replay 能力的 Session
3. **构建共享 Snapshot**：加载源 Session 的冻结 Context 投影，**排除**被变更的 Memory 条目（避免双写），计算 `shared_context_hash`
4. **注入 Treatment**：
   - Baseline 分支：注入 before_content，计算 `before_treatment_hash`
   - Candidate 分支：注入 after_content，计算 `after_treatment_hash`
   - 如果 before/after hash 相同则拒绝执行（无实际变更）
5. **并行执行**：通过 `ThreadPoolExecutor(max_workers=2)` 同时启动两个分支，复用 Skill True Replay 的 `spawn_native_agent_branch` 运行器、Hermes 沙箱、systemd 隔离和 Model Broker
6. **渐进披露 + Checklist 门禁**：使用与 Skill Replay 完全相同的渐进披露协议和 Checklist 完成性门禁
7. **效率比较**：通过 `compare_efficiency` 按轮次 → 工具调用数 → Token 消耗进行字典序比较，产出 `accepted/rejected/inconclusive` 决策
8. **持久化结果**：Replay 结果（包含 baseline/candidate 轨迹、Checklist 报告、效率指标、最终决策）写回 Memory Change 记录

### 安全与一致性保障

- **Hash 校验**：每个分支返回 `context_input_hash`，与服务端计算的 `shared_context_hash + treatment_hash` 不匹配时 fail-closed
- **before/after 非空校验**：若 before 与 after 内容完全相同（hash 相等），拒绝执行无意义的 Replay
- **Checklist 最小化**：Memory Replay 要求至少 1 个 Checklist 项，最多 50 项，query 长度上限 32000 字符
- **运行时选择**：从源 Session 的 `runtime_type` 解析可用的 Replay 端点，本地 Hermes 走沙箱执行，远程 Agent（如 Pi Agent）走 HTTP Replay 适配器

代码入口：[dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py) 中的 `MemoryTrueReplayRunner`。

## 外部工具重放（Fail-Closed 策略）

真实 Session 中可能调用了外部工具（网络请求、数据库写入、第三方 API 等）。True Replay 对外部工具采用 **fail-closed** 策略：

- **可确定性注入**：当外部工具调用可以通过规范化工具名、标准化参数签名、同签名调用序列和结果 SHA-256 进行匹配注入时，回放该工具的录制结果
- **不可重放**：当外部工具的副作用无法确定性注入当前 Runtime 时，返回 `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` 错误，该 Case 标记为不可运行，**不**回退到真实外部调用
- **workspace-local 工具**：文件读写、终端命令等 workspace 内工具在沙箱内真实执行

> **注意**：仅按工具名匹配不构成 Protocol V1 合规的重放。必须同时匹配参数签名和调用序列。Pi Agent 当前声明 `external_tool_replay=fail-closed`：workspace 本地工具在分支沙箱内执行，网络能力或外部工具使 Case 不可运行而非回退到实时副作用。

## Replay Model Broker

隔离沙箱内的 Agent 需要调用 LLM，但不能直接持有模型 API Key。Replay Model Broker 提供短期凭证代理：

### 架构

```
┌─────────────────────────────────────────────────┐
│  Sandbox Worker (PrivateNetwork=yes)            │
│                                                 │
│  ┌───────────────┐    ┌──────────────────────┐  │
│  │ Hermes Agent  │───►│ ReplayModelSidecar   │  │
│  │ (config.yaml  │    │ (127.0.0.1:43128,    │  │
│  │  → sidecar)   │    │  loopback only)      │  │
│  └───────────────┘    └──────────┬───────────┘  │
│                                  │ Unix Socket   │
└──────────────────────────────────┼───────────────┘
                                   │ (Unix Stream,
                                   │  0o600 perms,
                                   │  Bearer token)
                                   ▼
                        ┌──────────────────────┐
                        │ ReplayModelBroker    │
                        │ (parent process,     │
                        │  Unix domain server) │
                        └──────────┬───────────┘
                                   │ httpx.stream
                                   │ (with real API key)
                                   ▼
                        ┌──────────────────────┐
                        │ Upstream LLM API     │
                        │ (base_url + api_key) │
                        └──────────────────────┘
```

### 安全属性

- **Key 隔离**：真实 API Key 仅存在于父进程（teamEvolver 主进程），沙箱内的 Hermes 配置文件中使用一次性 Bearer token（`secrets.token_urlsafe(32)`）
- **Unix Socket 通信**：Sidecar 通过 Unix Domain Socket 连接 Broker，Socket 文件权限 0o600，网络命名空间隔离阻止直接外网访问
- **短期生命周期**：Broker 和 Sidecar 随 Replay 分支启动，分支结束后立即关闭并删除 Socket 文件
- **Token 认证**：Sidecar 到 Broker 的请求必须携带正确的 Bearer token，Broker 验证后转发到上游 LLM，替换为真实 API Key
- **流式透传**：Broker 以 streaming 模式透传上游响应，不缓存完整响应，Sidecar 在 loopback 接口（127.0.0.1）上接收

Broker 的 worker_base_url 返回 `http://127.0.0.1:{port}/upstream`，Sidecar 监听该端口并通过 UDS 转发到父进程 Broker。

## 路径接地（Path Grounding）

如果指令中引用了文件路径（绝对路径或仓库相对路径），True Replay 在执行前验证路径存在性：

1. 从指令中提取看起来像文件路径的 token
2. 对绝对路径直接检查是否存在；对相对路径在搜索根目录下查找
3. 上传文件（Session Materials）映射到 `uploaded://{path}` 虚拟路径
4. 所有引用路径都存在 → Case 标记为 `runnable`；有缺失路径 → Case 标记为不可运行

引用路径用于配置 systemd 的 `BindReadOnlyPaths`，确保沙箱内可以读取这些文件。

## 分支结果收集

每个分支执行完成后输出包含：
- `ok`：是否成功
- `final_response`：最终回复文本
- `messages`：完整消息序列
- `interaction_turns`：交互轮次
- `tool_call_count`：工具调用数
- `total_tokens`/`input_tokens`/`output_tokens`：Token 消耗
- `interactions`：每轮交互详情（prompt、response、tool_call_count、checklist_report）
- `checklist_report`：最终 Checklist 评估结果
- `workspace_artifacts`：workspace 中的产物文件列表
- `context_input_hash`：实际使用的上下文 hash
- `error`/`error_code`：失败信息

结果被聚合并传入 `progressive_replay_decision` 做最终决策。

## 代码入口

| 模块 | 路径 |
|------|------|
| True Replay 核心 | [true_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/true_replay.py) |
| 渐进披露与 Checklist 决策 | [progressive_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/progressive_replay.py) |
| Replay Model Broker | [integrations/replay_model_broker.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/replay_model_broker.py) |
| Replay HTTP 适配器 | [integrations/replay_adapters.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/replay_adapters.py) |
| 效率指标比较 | [replay_metrics.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/replay_metrics.py) |
| Memory True Replay | [dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py) |
| Agent 注册与运行时解析 | [integrations/agent_registry.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/agent_registry.py) |
| Protocol V1 Replay Branch 规范 | [Protocol V1 规范](../agent-integrations/02-protocol-v1) |

## 相关文档

- [进化闭环](./02-evolution-loop)：True Replay 在进化闭环中的 Validate 阶段
- [Checklist 门禁](./07-checklist)：Checklist 作为完成性门禁的详细规则
- [Session 体系](./05-sessions)：Session 如何提供 Replay Case 和 Materials
- [Skill 体系](./03-skills)：Skill Candidate 与 Baseline 的关系
- [Memory 体系](./04-memory)：Memory Change 的 True Replay 验证
