# Agent 注册退役落地方案

> 状态：阶段 0–4、阶段 5 迁移/恢复工具和新控制台已实现并验证；阶段 6 独立清理源码、补丁及数据清理/恢复工具已实现。现网切换、观察期和数据迁移尚未执行。
>
> 目标：彻底退役 `/internal/agents/register`、`agents.json` 和 Agent subject 映射。Agent 数据面只使用租户机凭证 `tevt_` 与请求中的 `user_id`，不识别、不登记、也不校验 Agent 来源。

## 1. 最终信任模型

实现入口：[v2 接入](./docs/zh/agent-integrations/06-protocol-v2.md)、
[迁移与恢复](./docs/zh/guides/11-agent-deregistration.md)、
[迁移脚本](./scripts/migrate_deregister.py)。
兼容版和独立清理版均已通过后端测试、前端生产构建和文档引用检查；最终数量、启动证据和发布限制见 [验收报告](./agent-deregistration-acceptance.md)。
这里的“实现完成”不代表已满足生产观察期，也不代表已执行阶段 5/6 发布。

### 1.1 唯一身份

Agent 数据面的唯一身份是：

```text
(tenant_id, user_id)
```

- `tenant_id` 只能由服务端根据 `tevt_` 解析，客户端声明的租户 ID 不参与鉴权。
- `user_id` 由持有租户 Key 的调用方直接声明，是可信的 acting subject，不是第二个密钥。
- `account_id` 从当前租户的有效配置中解析，不接受请求传入。
- 租户 Key 对该租户数据面拥有完整权限，可以代表该租户 Account 下的任意 `user_id`。
- 服务端只校验 `user_id` 非空、长度和路径安全，不再查询 Agent 注册、`agent_subjects` 或 `agent_identities`。
- 相同 `user_id` 在不同租户下是两个不同主体，存储、Context ref、Session 和 OpenViking Account 必须隔离。

Agent 数据面不再关心：

- Agent 类型、实例、安装来源或运行位置
- `integration_id`
- `external_subject`
- Agent capabilities
- Agent endpoint 注册
- Agent 启用/停用状态

### 1.2 数据访问原则

`tevt_` 先确定租户及其 OpenViking Account，`user_id` 再确定该 Account 内的个人命名空间。Agent 请求不得通过全局 `users.json` 选择 OpenViking Account、API Key 或其他租户配置。

因此，Agent 数据面不复用当前 `_agent_context_user_by_id()`。应新增一个小接口集中解析身份：

```python
@dataclass(frozen=True)
class AgentPrincipal:
    tenant_id: str
    account_id: str
    user_id: str


def resolve_agent_principal(request, config, user_id: str) -> AgentPrincipal:
    """由已认证的 tevt_ 请求和 user_id 构造租户内主体。"""
```

该模块隐藏以下实现：

- 校验请求确实由有效 `tevt_` 认证
- 从 request context 读取 `tenant_id`
- 从租户有效配置读取 `sharing_viking_account`
- 规范化并校验 `user_id`
- 拒绝空值、路径穿越字符和超长 ID

Context、Session push 和后续 Agent 数据面只依赖 `AgentPrincipal`，不各自重复身份逻辑。

## 2. 当前依赖与目标拆分

当前注册表承担了四类无关职责：

1. Context Workspace 的 Agent ownership
2. V1 Session push 的 Agent 校验和 subject 映射
3. Skill Sync 服务端 push 的 endpoint 发现
4. True Replay 的 endpoint 和 capability 配置

退役后分别由独立模块承担：

| 当前职责 | 最终实现 |
|---|---|
| Agent 身份 | 删除；数据面身份固定为 `(tenant_id, user_id)` |
| Context ownership | `AgentPrincipal` + tenant-scoped `ContextStateStore` |
| Session subject 映射 | 请求直带 `runtime_context.user_id` |
| Skill 分发 | Agent 使用 `tevt_` 主动拉取 `/sync/skills` |
| Replay 执行 | 系统负责裁判和渐进交互；客户 `ReplayAdapterFactory` 只运行下游 Agent |
| Validation runtime 列表 | `validation.runtimes` 配置 |

数据源 pull 链路继续保持独立，不引入 Agent 注册依赖。

## 3. 决策汇总

| # | 决策点 | 选择 |
|---|---|---|
| D1 | 鉴权标准 | 仅 `tevt_ + user_id` |
| D2 | Agent 来源 | 不登记、不识别、不校验 |
| D3 | Context ownership | `(tenant_id, user_id)`；ref/session 同时校验两者 |
| D4 | User 数据来源 | Agent 数据面不查询全局 `users.json` |
| D5 | OpenViking Account | 由 `tevt_` 对应租户的有效配置确定 |
| D6 | Skill Sync | 删除服务端 push，保留认证后的 Agent pull |
| D7 | True Replay | 系统负责 A/B、裁判、拟人化披露和指标；客户 Adapter 只负责 Agent 执行 |
| D8 | 注册表 | 完全删除 `agents.json` 和注册路由 |
| D9 | 协议迁移 | 先双读兼容，再切严格模式，最后清理数据 |

## 4. 最终架构

```text
Agent
  |
  | Authorization: Bearer tevt_xxx
  | user_id: alice
  v
Tenant middleware
  |
  | derives tenant_id + effective account_id
  v
AgentPrincipal(tenant_id, account_id, user_id)
  |
  +--> Context Workspace
  |      ownership = tenant_id + user_id
  |
  +--> Session push
  |      subject = user_id
  |
  +--> /sync/skills
  |      tenant-scoped pull
  |
  +--> True Replay
         system judge + user simulator
         tenant-bound ReplayAdapterFactory -> downstream Agent
```

`agents.json` 不在任何请求路径、后台任务或 Replay 路径中。

## 5. 分阶段落地

### 阶段 0：契约冻结与兼容开关

**目的**：只增加能力，不修改存量数据，不影响旧客户端。

#### 0.1 临时兼容模式

新增临时配置：

```yaml
agent_protocol:
  identity_mode: dual  # dual | tenant_user

skills:
  delivery_mode: push  # push | pull

replay:
  adapter: ""
  adapters_dir: ""
```

- `dual`：优先接受 `user_id`；旧请求仍可通过 `integration_id + external_subject` 解析。
- `tenant_user`：只接受 `tevt_ + user_id`。
- `skills.delivery_mode` 仅用于灰度和回滚，注册表删除后固定为 `pull` 并移除开关。

配置必须同步接入：

- `teamEvolver/config_store/defaults.py`
- `teamEvolver/config.py`
- `teamEvolver/config_store/bridge.py`
- 配置序列化、环境变量覆盖和测试

`agent_protocol.identity_mode` 和 `skills.delivery_mode` 是迁移期的部署级开关，
不允许租户覆盖。租户只通过扁平字段 `replay_adapter` 选择 Replay adapter；
`replay_adapters_dir` 同样是部署级配置。

#### 0.2 可观测性

增加以下计数：

- `agent_identity_requests_total{mode="tenant_user|legacy"}`
- `agent_identity_rejected_total{reason}`
- `skill_pull_requests_total{tenant_id,status}`
- `replay_adapter_resolve_total{tenant_id,result}`

旧协议响应增加 Deprecation/Sunset 信息。只有连续一个完整发布周期无 legacy 请求，才允许进入阶段 5。

#### 0.3 本阶段禁止事项

- 不清空 `agent_subjects` / `agent_identities`
- 不改写 Context 状态字段
- 不删除 `agents.json`
- 不把旧 outbox 标记为 `synced`
- 不删除旧 Schema

### 阶段 1：统一 AgentPrincipal 与 Context Workspace

#### 1.1 新增 AgentPrincipal 模块

建议新建：

`teamEvolver/integrations/agent_principal.py`

接口只暴露：

```python
resolve_agent_principal(request, config, user_id) -> AgentPrincipal
```

实现要求：

- 只接受已由 middleware 验证的 `tevt_`
- `tenant_id` 来自 `current_tenant_id()`
- `account_id` 来自当前租户的有效 `sharing_viking_account`
- `user_id` 使用统一规范化规则，建议最大 160 字符
- 不读取 `agents.json`
- 不读取 `users.json`
- 不接受客户端传入 account/tenant 覆盖

#### 1.2 `ContextStateStore` 使用主体绑定

文件：`teamEvolver/integrations/context_workspace.py`

将外部接口中的 `agent_id` 替换为 `principal`，记录至少保存：

```json
{
  "tenant_id": "tenant-a",
  "user_id": "alice"
}
```

需要调整：

- `issue_ref`
- `resolve_ref`
- `save_snapshot`
- `record_snapshot_read`
- `load_snapshot`
- `start_session`
- `mark_openviking_created`
- `get_session`
- `event_status`
- `record_event`
- `resolve_session_usage_refs`
- `mark_usage_submitted`
- `mark_committed`
- `audit`
- `verify_context_usage`

所有读取、修改、撤销和提交都必须同时验证 `tenant_id` 与 `user_id`，不能只依赖 ref/session ID。

Context Session ID 改为：

```python
stable_hash({
    "tenant_id": principal.tenant_id,
    "user_id": principal.user_id,
    "external_session_id": external_session_id,
})
```

这样同租户不同用户使用相同 `external_session_id` 时不会冲突。现有 Context Session ID 保持不变，通过兼容读取处理，不做在线重命名。

PG 中 `agent_context_state.json` 已按租户 RLS 隔离；记录中的 `tenant_id` 是防御性校验和审计字段，不替代底层租户隔离。

#### 1.3 Context Workspace 九个端点

文件：`teamEvolver/proxy/agent_context.py`

所有端点都要求 `user_id`，包括只携带 ref/session ID 的操作：

| 端点 | 新请求身份字段 |
|---|---|
| describe | Query `user_id` |
| resolve | Body `user_id` |
| read | Body `user_id` |
| skills | Query `user_id` |
| remember | Body `user_id` |
| forget | Body `user_id` |
| sessions.start | Body `user_id` |
| sessions.append | Body `user_id` |
| sessions.commit | Body `user_id` |

统一处理流程：

1. `principal = resolve_agent_principal(request, effective_config, user_id)`
2. 根据 `principal.account_id + principal.user_id` 构造个人空间
3. 根据 `principal.account_id` 构造团队空间
4. Context store 调用只传 `principal`

删除：

- `_agent_context_integration`
- `_agent_context_user`
- `_agent_context_user_by_id`
- `resolve_active_agent` import
- `resolve_agent_subject_user_id` import

兼容模式下：

- 有 `user_id` 时走新路径，忽略旧身份字段。
- 没有 `user_id` 且模式为 `dual` 时，使用旧注册表和 subject 映射得到 user_id，再构造 `AgentPrincipal`。
- 模式为 `tenant_user` 时，缺少 `user_id` 返回 `400 USER_ID_REQUIRED`。

#### 1.4 协议版本

新增 Context v2 Schema，保留 v1 只用于迁移期解析：

- `agent-context-request-v2.schema.json`
- `agent-context-result-v2.schema.json`
- `agent-context-snapshot-v2.schema.json`

不要原地修改 v1 Schema 的含义。v2 响应 subject 只包含：

```json
{
  "tenant_id": "tenant-a",
  "user_id": "alice"
}
```

### 阶段 2：Session push 与 Agent 客户端

#### 2.1 Session 协议

文件：

- `session_ingestion/push/protocol.py`
- `docs/schemas/agent-session-v2.schema.json`

v2 要求：

```json
{
  "runtime": {
    "type": "hermes"
  },
  "runtime_context": {
    "user_id": "alice"
  }
}
```

- `runtime.type` 仅用于分析、展示和 Replay adapter 选择，不参与鉴权。
- 删除 `runtime.integration_id`。
- 删除 `runtime_context.external_subject`。
- `runtime_context.user_id` 必填。

v1 在 `dual` 模式继续兼容，切换为 `tenant_user` 后拒绝。

#### 2.2 Session 路由

文件：`session_ingestion/push/routes.py`

新路径：

1. 验证 `tevt_`
2. 从 `runtime_context.user_id` 构造 `AgentPrincipal`
3. 写入 `runtime_context.team_evolver_user_id`
4. `meta.user_id` 使用规范化后的 `principal.user_id`
5. `verify_context_usage(principal=principal, turns=...)`
6. 进入 ingest

删除：

- `resolve_active_agent`
- `resolve_agent_subject_user_id`
- `agent_record`
- Agent 启用/停用判断

服务端不要求 `user_id` 预先存在于全局 `users.json`。租户 Key 对其 Account 内的 user ID 声明负责。

#### 2.3 Hermes 客户端

必须同时更新：

- `session_ingestion/push/hermes/push_session.py`
- `session_ingestion/push/hermes/install.py`
- `teamEvolver/integrations/hermes_context_provider/__init__.py`
- Context delivery spool 生成的 payload

Hermes 配置只保留：

```json
{
  "base_url": "...",
  "tenant_token": "tevt_...",
  "user_id": "alice"
}
```

`HermesDeliverySpool.integration_id` 如果仅用于本地幂等和队列分区，应重命名为 `producer_id`，不要把内部队列标识误当成 Agent 身份。

### 阶段 3：Skill Sync 切换为认证 Pull

#### 3.1 开放租户机凭证路径

文件：`teamEvolver/proxy/routes.py`

将精确路径 `/sync/skills` 加入 `tevt_` 可访问的数据面路径。请求必须携带
Query `user_id`，并通过 `resolve_agent_principal()` 形成与其他 Agent 数据面
一致的身份；Skill bundle 仍按租户共享，不按用户过滤。该端点必须：

- 拒绝匿名请求
- 拒绝失效租户 Key
- 拒绝缺少或非法的 `user_id`
- 使用 token 解析出的当前租户
- 忽略客户端提供的租户覆盖

`_sync_bundle_payload()` 已使用 `current_tenant_id()`，继续保留该租户隔离。

#### 3.2 Pull 模式不创建伪 delivery

文件：

- `team_skills/library/mutations.py`
- `team_skills/candidates/worker.py`
- `teamEvolver/launcher.py`
- `teamEvolver/proxy/routes.py`

当 `skills.delivery_mode=pull`：

- 发布成功状态为 `published` 或 `available`
- 不创建新的 `skill_sync_outbox` delivery
- 不调用 no-op deliverer
- 不把“无人接收”记录为 `synced`
- 候选发布结果不再生成误导性的 `agent_sync.synced`

切换时，旧 outbox 事件统一标记：

```json
{
  "status": "cancelled",
  "reason": "delivery_mode_changed_to_pull"
}
```

在兼容窗口内可以保留 push worker。进入阶段 5 后删除：

- `_run_skill_sync_outbox`
- `skill_sync_adapters.py`
- retry/discard 管理路由
- 对应前端状态和操作

#### 3.3 Agent pull 增强

文件：`teamEvolver/integrations/hermes_skill_sync/sync_skills.py`

- 使用 `tevt_` 请求 `/sync/skills?user_id=<user_id>`
- 增加响应 ETag，客户端发送 `If-None-Match`
- 本地缓存 `{name: {version, sha256, tree_sha256}}`
- 未变化的 Skill 不执行删除和重写
- `DEFAULT_MIN_INTERVAL_SECONDS` 从 60 秒降至 15 秒

该 hook 是 `pre_llm_call`，因此交付语义是“下一次模型调用前可见”，不是“发布后 15 秒内主动推送”。文档和验收必须使用这一语义。

### 阶段 4：数据集驱动的 True Replay

#### 4.1 职责划分

teamEvolver 负责完整的评测编排：

- 从 Test Dataset 读取初始 `query`、Checklist 和固定材料
- 为 Baseline/Candidate 创建彼此隔离的分支
- 保证两个分支收到相同的初始 query、材料、上下文和运行限制
- 每轮调用下游 Agent
- 用独立裁判 Agent 检查 Checklist
- 未完成时选择下一批待披露要求
- 把要求改写成拟人化的用户反馈
- 达成 Checklist 后计算并比较客观指标
- 保存每轮对话、裁判证据、披露记录和指标

客户 Python adapter 只负责真实运行下游 Agent：

- 创建一个分支会话
- 应用该分支的 treatment
- 接收一条用户消息并调用客户 Agent
- 返回 Agent 回复、轨迹、产物和原始指标
- 清理分支会话或工作区

客户 adapter 不负责也不能看到：

- Checklist
- Checklist 满足状态
- 下一批隐藏要求
- 裁判 Prompt 或裁判结论
- Baseline/Candidate 比较结果
- 发布决策

这保证下游 Agent 看到的是自然用户交互，而不是评测协议。

#### 4.2 标准 Replay 循环

每个 Replay Case 对 Baseline 和 Candidate 分别执行：

```text
1. 打开隔离分支，并应用唯一的 A/B treatment 差异
2. user_message = dataset.query
3. 把 user_message 发送给下游 Agent
4. 收集 AgentObservation
5. 裁判 Agent 根据完整交互证据检查 Checklist
6. 若全部满足：结束分支并计算客观指标
7. 若未全部满足：
   a. 选择下一批尚未满足的要求
   b. 生成拟人化用户反馈
   c. 将反馈作为下一轮 user_message
8. 达到 max_interactions 仍未满足：分支标记 checklist_incomplete
```

首轮只向下游 Agent 发送 Test Dataset 的 `query`。Checklist 不进入初始
Prompt，也不进入 adapter context。

Baseline 与 Candidate 的唯一预期差异是 treatment：

- Baseline 使用当前已发布 Skill 或不加载 Candidate
- Candidate 加载待验证的 Skill Candidate

模型、Agent 版本、工具、材料、上下文快照、超时和初始 query 必须保持一致。
如果客户运行时无法保证隔离或固定这些条件，应返回 `unsupported`，不能静默执行
不可比较的 A/B。

#### 4.3 最小客户 Adapter 接口

新建：`team_replay/hooks.py`

```python
from dataclasses import dataclass
from typing import Any, Literal, Mapping, Protocol


@dataclass(frozen=True)
class ReplayTreatment:
    branch: Literal["baseline", "candidate"]
    skill: Mapping[str, Any] | None


@dataclass(frozen=True)
class ReplayContext:
    request_id: str
    runtime_type: str
    treatment: ReplayTreatment
    materials: tuple[Mapping[str, Any], ...]
    context_snapshot: Mapping[str, Any]
    timeout_seconds: int


@dataclass(frozen=True)
class AgentObservation:
    response: str
    messages: tuple[Mapping[str, Any], ...] = ()
    artifacts: tuple[Mapping[str, Any] | str, ...] = ()
    metrics: Mapping[str, int | float] | None = None


class ReplaySession(Protocol):
    def send(self, user_message: str) -> AgentObservation:
        ...

    def close(self) -> None:
        ...


class ReplayAdapterFactory(Protocol):
    def open(self, context: ReplayContext) -> ReplaySession:
        ...
```

接口约束：

- `query` 和后续反馈都只通过 `ReplaySession.send(user_message)` 传入。
- adapter 自己维护客户 Agent 的会话连续性；系统不要求客户处理内部 history 格式。
- A/B 变量只通过 `ReplayContext.treatment` 传入。
- `ReplayContext` 不含 Checklist、期望答案或裁判状态。
- `AgentObservation.response` 必填。
- `messages`、`artifacts` 用作裁判证据。
- `metrics` 只接受运行时真实观测值，不允许估算或缺失时补零。
- 所有 Session 显式实现 `close()`；无资源实现使用 no-op。
- 每个分支创建独立 `ReplaySession`，不能共享对话状态或可变工作区。

现有 `TurnBasedReplayAdapter`、`MappedHttpAdapter` 和 `DeapReplayAdapter`
分别包装为该接口的内置 Adapter。`HttpReplayAdapter` 和
`LegacyAgentsHubHttpAdapter` 属于 branch-only 旧协议，迁移期如需保留，
使用经过测试的 `LegacyBranchSession` 包装；最终不作为标准接口的一部分。

#### 4.4 客户 Python 文件

每个客户 adapter 文件只暴露一个工厂：

```python
REPLAY_ADAPTER = {
    "label": "Customer Replay",
    "enabled": True,
}


def build_replay_adapter(config):
    return CustomerReplayFactory(...)
```

客户在 `ReplayAdapterFactory.open(context)` 中完成 endpoint、认证、
Agent 会话、工作区和 treatment 注入，在 `ReplaySession.send()` 中完成真实
Agent 调用。teamEvolver 不要求客户注册 endpoint、capability 或 Agent 类型。

客户 Python 文件可以读取租户的 Replay 配置和部署环境变量，但系统只把执行所需的
`ReplayContext` 交给它。统一使用 `build_replay_adapter`，不再同时存在
`build_replay_hook`。

#### 4.5 裁判 Agent

裁判 Agent 每轮读取：

- 完整 Checklist
- 截至当前轮的用户消息和 Agent 回复
- Agent 返回的 messages/tool trace
- 当前分支产物

裁判只输出结构化事实：

```json
{
  "items": [
    {
      "id": "R01",
      "satisfied": true,
      "evidence": "具体可核验的回复、工具轨迹或产物证据"
    }
  ],
  "all_satisfied": false,
  "positive_observations": [
    "已经完成且值得向用户确认的具体内容"
  ]
}
```

规则：

- Checklist 是完成门禁，不是评分项。
- 没有具体证据时不得判定满足。
- 裁判失败时 fail closed，不计算效率胜负。
- 裁判不生成 Token、工具调用数等运行指标。
- 裁判不直接决定 Candidate 是否发布。

#### 4.6 拟人化渐进披露

将现有 `next_disclosure_prompt()` 拆成两个步骤：

1. `select_disclosure_items()`：确定性选择下一批未满足 Checklist 条目。
2. `render_user_feedback()`：由用户模拟器把结构化结果改写成自然用户消息。

用户模拟器输入：

- 本轮 Agent 回复
- 裁判确认的 `positive_observations`
- 本轮允许披露的未满足要求
- 已披露要求和轮次

用户模拟器输出：

```json
{
  "message": "前面的数据整理得很清楚。不过我还需要你补充每项成本的计算依据，并把最终结论写进交付文件。"
}
```

反馈要求：

- 先具体肯定已完成且有证据支持的内容，再指出当前需要补充的内容。
- 没有可肯定内容时不虚构表扬，使用自然的澄清或追问。
- 使用真实用户语气，不出现 Checklist、评分、评测、Baseline、Candidate、轮次编号等内部词。
- 不直接复制 Checklist ID。
- 只披露当前选中的要求，不泄露后续隐藏要求。
- 不替 Agent 给出实现答案，只表达用户目标、缺失结果或期望修正。
- 同一缺口再次反馈时应结合最新回复重新措辞，避免机械重复。
- 反馈作为普通 `user` 消息写入交互历史，供下一轮 Agent 和最终审计使用。

裁判保持 `temperature=0`；用户模拟器可使用低但非零温度，以提高自然度。
两者必须使用不同 Prompt 和独立输出 Schema，不能让文案生成影响 Checklist 判定。

#### 4.7 客观指标与决策

只有 Checklist 门禁通过后，才比较效率指标：

| 指标 | 来源 |
|---|---|
| `interaction_turns` | teamEvolver 计数 |
| `elapsed_seconds` | teamEvolver 单调时钟 |
| `tool_call_count` | 优先由 messages/tool trace 计算 |
| `total_tokens` | 客户 Agent 运行时返回 |
| `input_tokens` / `output_tokens` | 客户 Agent 运行时返回 |
| `api_calls` | 客户 Adapter 返回 |

缺失指标记录为 `unavailable`，不补零，也不参与该维度比较。

决策顺序：

1. Candidate 未通过 Checklist：reject。
2. Candidate 通过、Baseline 未通过：按完成门禁 accept。
3. 两者都通过：只比较客观指标。
4. 两者都未通过或裁判不可用：inconclusive，不比较效率。

不引入主观总分。每个结论必须能回溯到 Checklist 证据或原始指标。

#### 4.8 加载、权限与绑定

新建：`team_replay/adapter_runtime.py`

实现：

- `directory(config)`
- `validate_content(content, filename)`
- `available(config)`
- `binding(config, tenant)`
- `describe(config, tenant)`
- `load_factory(config, tenant, revision)`
- `read_content`
- `save_content`

配置字段：

- `TeamEvolverConfig.replay_adapter`
- `TeamEvolverConfig.replay_adapters_dir`
- 非 default 租户从 `config_overrides.replay_adapter` 读取绑定
- 非 default 租户不得继承 default 租户绑定

提供以下控制面路由：

- `GET /api/replay-adapter`
- `PUT /api/replay-adapter`
- `GET /api/replay-adapter/code`
- `PUT /api/replay-adapter/code`
- `POST /api/replay-adapter/code/test`

Python adapter 会在服务进程内执行任意代码，因此：

- 绑定预装 adapter 可由管理员操作。
- 源码读写和测试只允许 Root Key/部署 Owner。
- 测试接口使用显式测试 context 和 query，不接触真实发布数据。
- 如果未来开放给租户管理员，必须改为隔离进程，不能只依赖 AST 校验。

#### 4.9 ReplayHost 与 engine

`ReplayHost` 删除：

- `resolve_runtime_agent`
- `resolve_replay_capability`

新增：

```python
resolve_replay_factory(
    runtime_type: str,
    source_session: Mapping[str, Any],
) -> ReplayAdapterFactory | None
```

`team_replay/engine.py` 保留并集中实现：

- A/B 分支编排
- 初始 query 发送
- Checklist 裁判
- 渐进披露选择
- 拟人化用户反馈
- 轮次和超时控制
- 客观指标聚合与比较

删除：

- Agent 注册查询
- capability 字典解析
- endpoint 解析
- transport 选择
- `integration_id` fallback

`AGENTSHUB_REPLAY_URL` 等旧环境变量只能由默认 Replay adapter 兼容，engine
不再读取。下游 Agent 不需要实现 teamEvolver Replay 协议，只需要由客户
Python adapter 转换 `send(user_message)` 和 `AgentObservation`。

同时更新：

- `team_replay/policy.py`
- `team_replay/metrics.py`
- `team_replay/protocol.py`
- `team_replay/runtime_matrix.py`
- `teamEvolver/replay_adapter.py`
- `scripts/import_skillopt.py`
- Replay 和 team-memory 测试

Validation runtime 来源改为：

```yaml
validation:
  runtimes:
    - hermes
    - deap
    - agentshub
```

`team_skills/evolution/runtime/orchestrator.py` 和
`team_skills/library/runtime_policy.py` 不再调用 `list_agents()`。

### 阶段 5：严格切换与数据迁移

**前置条件**：

- legacy 请求计数在一个完整发布周期内为 0
- 所有 Agent 已改为发送 `user_id`
- 所有租户的 `/sync/skills` token 隔离测试通过
- 使用 True Replay 的租户均已绑定可用 adapter

#### 5.1 先切严格模式

```yaml
agent_protocol:
  identity_mode: tenant_user

skills:
  delivery_mode: pull
```

观察错误率、Session ingest、Context、Skill pull 和 Replay 至少一个完整业务周期。此时仍保留旧数据用于快速回滚，但新请求不再读取。

#### 5.2 迁移脚本

新建：`scripts/migrate_deregister.py`

必须支持：

```text
--dry-run
--apply
--tenant <id>
--all-tenants
--restore <backup-manifest>
```

要求：

- 可重复执行
- 写入 migration version
- 输出迁移前后记录数和 SHA-256
- 任一租户失败时不继续删除旧数据
- 备份与原数据位于相同 tenant/object-store scope

PG 迁移：

1. 枚举租户。
2. 对每个租户显式调用 `read_kv(..., tenant_id=tenant_id)`。
3. 为该租户 Context refs/sessions/snapshots 写入实际 `tenant_id`。
4. 复用记录中已有的 `user_id`。
5. 保留旧 `agent_id` 为 `agent_id_legacy`，直到最终清理。
6. 不从 Agent metadata 或 audit 猜测租户。
7. 不允许无法识别时回填 `default`。

文件模式只有 `default` 租户，按相同规则处理。

现有 Context Session ID 不重命名；新 Session 使用阶段 1 的新 ID 算法。

备份：

- 每个租户的 `agents.json` → `agents.pre-deregister.json`
- 全局 `users.json` → `users.pre-deregister.json`
- 每个租户的 `agent_context_state.json` → 对应备份 key
- 生成包含 tenant、key、记录数、checksum 的 manifest

备份完成后才从 `users.json` 删除 `agent_subjects` 和 `agent_identities`。不在每条用户记录中复制 `_legacy` 字段，避免 2 万用户注册表体积翻倍。

### 阶段 6：删除注册表与兼容代码

删除：

- `teamEvolver/integrations/agent_registry.py`
- `teamEvolver/integrations/skill_sync_adapters.py`
- `POST /internal/agents/register`
- `GET/POST /api/agent-integrations`
- `POST /internal/agentshub/openviking-config`
- Agent 注册前端和 setup checklist
- subject mapping 函数与 UI
- legacy identity mode
- push delivery mode及其后台任务和管理接口

执行全仓扫描：

```bash
rg -n "agent_registry|register_agent|resolve_active_agent|list_agents"
rg -n "integration_id|external_subject|agent_subjects|agent_identities"
```

扫描结果需要逐项分类：

- Agent 身份语义：删除
- legacy migration/备份说明：可保留并明确标记
- 内部 producer/idempotency 标识：重命名，不能机械删除

已知必须补齐的文件包括：

- `teamEvolver/integrations/hermes_context_provider/__init__.py`
- `team_replay/runtime_matrix.py`
- `teamEvolver/integrations/hermes_delivery.py`
- `CONTEXT.md`（删除或重定义已退役的 Agent Integration 术语）
- `AGENTS.md`（更新代码结构中的 Agent 注册说明）
- `README.md` / `README.en.md`
- `CUSTOMER_DEPLOYMENT.md`
- `docs/agent-integration-protocol-v1.md`
- `docs/schemas/agent-context-result-v1.schema.json`
- `docs/schemas/agent-context-snapshot-v1.schema.json`
- 所有中英文 Agent、Context、Session、Skill Sync 和 Replay 文档

文档链接统一改为仓库相对路径，不能包含开发机绝对路径。

## 6. 回滚策略

### 阶段 0-4

- `agent_protocol.identity_mode` 切回 `dual`
- `skills.delivery_mode` 切回 `push`
- Replay 切回旧 host 实现
- 不需要恢复数据，因为尚未删除旧字段

### 阶段 5

- 先切回 `dual`
- 使用 backup manifest 恢复 users、agents 和 Context 状态
- 恢复后校验 checksum 和记录数

### 阶段 6

阶段 6 是最终不可兼容清理，只能通过版本回滚加数据 restore 完成。必须在阶段 5 稳定观察后单独发布。

“git revert”不能替代数据回滚。

## 7. 测试计划

### 7.1 身份与隔离

- 有效 `tevt_ + user_id` 可访问 Agent 数据面
- 缺少或无效 `tevt_` 返回 401
- 缺少或非法 `user_id` 返回 `USER_ID_REQUIRED` / `USER_ID_INVALID`
- 不需要注册 Agent
- 不需要用户的 Agent subject 映射
- 相同 `user_id` 在租户 A、B 下访问不同 Account 和状态
- 客户端提供 `X-Tenant-Id` 不能覆盖 token 对应租户
- Context ref/session 同时校验 tenant 与 user

### 7.2 Context

- 九个端点全部覆盖
- 同租户两个用户使用同一 `external_session_id` 不冲突
- 用户 A 不能用自己的 `user_id` 读取或提交用户 B 的 ref/session
- v1/v2 双读和严格模式分别覆盖
- 存量 Session ID 在迁移后仍可完成

### 7.3 Session push

- v2 仅要求 `runtime.type + runtime_context.user_id`
- 未注册 runtime 可正常 ingest
- `meta.user_id` 和 `team_evolver_user_id` 使用规范化 user ID
- context usage 不能跨 tenant/user

### 7.4 Skill pull

- `tevt_ + user_id` 可访问 `/sync/skills`
- 匿名、失效 token 被拒绝
- 租户 A/B bundle 隔离
- ETag 未变化返回 304
- 本地未变化 Skill 不重写
- pull 模式不创建新 delivery outbox
- 存量 outbox 被标记 `cancelled`，不能标记 `synced`

### 7.5 Replay

- Baseline/Candidate 收到完全相同的初始 query、材料和上下文
- 两个分支只允许 treatment 不同，并使用独立 Session/工作区
- 第一轮 `send()` 只收到 Test Dataset 的 query
- Checklist、裁判结论和隐藏要求不会进入 `ReplayContext` 或客户 Adapter
- factory 构造 `ReplaySession`
- `send(user_message)` 返回 `AgentObservation`
- TurnBased、MappedHttp、DEAP 内置 Adapter 契约测试
- stateless Session 的 no-op `close`
- legacy branch Session 转换测试
- adapter 解析不依赖 `integration_id`
- engine 不解析 endpoint/capability
- 裁判逐项给出可核验证据，没有证据时 fail closed
- 全部满足后立即停止，不继续披露
- 用户反馈先肯定有证据的已完成内容，再自然表达当前缺口
- 无已完成内容时不虚构表扬
- 用户反馈不出现 Checklist ID、评分、Baseline、Candidate 等内部词
- 每轮只披露选中的未满足要求，不泄露后续要求
- 缺失运行指标标记 `unavailable`，不得补零
- 非 Root 用户不能编辑或测试 Python adapter 源码

### 7.6 迁移

- dry-run 不写数据
- 多租户逐租户迁移
- 重复 apply 结果一致
- 中途失败不删除源数据
- restore 后 checksum 与迁移前一致
- 不产生错误的 `default` tenant 回填

### 7.7 验证命令

```bash
python -m compileall teamEvolver team_skills team_memory team_miner team_replay session_ingestion tests
python -m pytest
npm --prefix web-ui run build
node docs/scripts/check-docs-refs.mjs
rg -n "agent_registry|register_agent|resolve_active_agent|list_agents"
```

## 8. 验收标准

1. Agent 数据面唯一身份为 `(tenant_id, user_id)`。
2. `tenant_id` 和 Account ID 只能由 `tevt_` 对应租户决定。
3. Agent 数据面不读取 `users.json`、`agents.json` 或 subject 映射。
4. Context 九个端点全部要求并校验 `user_id`。
5. Session push 无需注册，未注册 runtime 可正常 ingest。
6. `/sync/skills` 可由 `tevt_ + user_id` 访问且通过跨租户隔离测试。
7. pull 模式不产生伪 `synced` delivery。
8. True Replay 只通过租户绑定的 `ReplayAdapterFactory` 创建隔离 `ReplaySession`。
9. 客户 Python Adapter 的运行接口只有 `open(context)`、`send(user_message)` 和 `close()`。
10. 下游 Agent 首轮只收到数据集 query，任何一轮都看不到 Checklist 和裁判状态。
11. 裁判只负责完成门禁；未完成时由独立用户模拟器生成拟人化渐进反馈。
12. Checklist 通过后才比较客观指标，缺失指标不补零。
13. Replay engine 不读取 Agent endpoint、capability 或 `integration_id`。
14. `/internal/agents/register` 和 Agent 注册前端均不存在。
15. `agent_registry.py`、`agents.json` 和所有运行时引用均已删除。
16. 迁移有 dry-run、checksum、逐租户报告和可执行 restore。
17. 全部后端测试、前端构建和文档检查通过。

## 9. 阶段依赖

```text
阶段 0：契约、兼容开关、指标
   |
   +--> 阶段 1：AgentPrincipal + Context v2
   |       |
   |       +--> 阶段 2：Session v2 + Hermes 客户端
   |
   +--> 阶段 3：Skill pull
   |
   +--> 阶段 4：Replay adapter
           |
           v
阶段 5：严格模式 + 逐租户迁移
   |
   v
阶段 6：删除注册表、兼容代码和旧 UI
```

阶段 1-4 均为可回滚的加法改造。阶段 5 在旧流量归零后执行。阶段 6 必须独立发布，不能与协议切换或数据迁移混在同一次发布中。
