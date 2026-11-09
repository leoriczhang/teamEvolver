# Team Replay

`team_replay` 是 True Replay 与 Test Dataset 模块。它对每个 Replay Case 打开隔离的 Baseline 与 Candidate 会话，通过租户绑定的 Replay Adapter 驱动客户真实 Agent，用 Checklist 判定完成门禁，再比较效率。概念见 [True Replay](../docs/zh/concepts/06-true-replay.md)，接口见 [Replay 与实验工作台 API](../docs/zh/api/05-replay-branch.md)。

## 文件

```text
team_replay/
  api/                  # Adapter 控制面、数据集、Replay 运行的 HTTP 路由
  commands.py           # 短事务受理与固定执行配置
  handlers.py           # Worker Handler：replay.run、dataset.run，检查点与结果
  repository.py         # Replay 运行、Case 结果与检查点
  queries.py            # Replay 运行的公开只读查询
  contracts.py          # 跨模块 Replay 契约：ReplaySpec、treatment、结果
  gateway.py            # Embedded / Disabled / Host Replay Gateway
  integration.py        # 由固定 Adapter 与模型配置构造 ReplayHost
  host.py               # ReplayHost 协议
  runner.py             # 纯执行入口：不可变输入、注入的 host
  engine.py             # 租户绑定的双分支编排
  execution.py          # 单分支执行：run_branch
  execution_plan.py     # 冻结 Prompt/Context 组件、目标、位置、绑定与非目标环境
  request_capture.py    # 最终模型传输边界的完整请求与实际派发证据
  judging.py            # Checklist 裁判与用户模拟器
  policy.py             # Checklist 规范化、渐进披露与判定
  metrics.py            # 效率比较
  aggregation.py        # 多 Case 聚合
  memory.py             # Memory 内容级 Replay
  hooks.py              # Adapter 运行接口：ReplayContext、AgentObservation、ReplayUnsupported
  protocol.py           # 逐轮协议 teamevolver.replay-turn-*.v1
  adapters/             # 内置传输、Adapter 源码加载与校验、Adapter 版本与绑定
  adapters/examples/    # 单文件上传模板；AgentsHub 在每分支内复用隔离 Session
  turn_server.py        # Agent 侧逐轮执行示例服务
  datasets/             # 数据集 Schema、PG 仓库、Session 选择与快照、合成、材料
  config.py / schemas.py
  tests/
```

## 约定

- 所有 Replay 运行先持久化完整输入，再由 Worker 执行；重领后从已完成 Case 续跑。
- Adapter 源码只能由服务 Root Key 写入；每次运行固定受理时的 Adapter revision 与 `replay` 模块配置。
- 指标只记录运行时真实观测到的值，缺失记为不可用，不补零；裁判不可用时 fail closed。
- 本模块不导入 `team_memory` 或其他模块的 Handler；读取 Session 与 Skill 只通过 `session_ingestion/queries.py` 与 `team_skills/library/queries.py` 等公开 Query。
- Skill Candidate、静态检查、Candidate Review 与 Publish 属于 `team_skills`；Memory Change 账本属于 `team_memory`。

## Prompt 与 Context 执行计划

原有 Skill-only Adapter 无需新增字段。独立 Prompt/Context 候选通过两分支的 `execution_plan` 传入；Schema 为 `teamevolver.execution-plan.v1`，包含 `binding`、`environment`、完整 `components` 和 `target_component_ids`。组件固定 `component_id`、`kind`、`slot`、`order`、`binding`、`revision`、`content` 与正文 SHA-256 `content_hash`。目前支持 `agent_prompt`、`context_fragment`，slot 为 `system`、`developer`、`context`（物化为 developer）。所有共享组件均保留，不能按 Case 的 Skill 子集裁掉。

两分支只允许目标正文、哈希和 revision 变化；其他组件、注入位置、绑定、原材料与运行环境必须不变。联合 Skill 修改还要求计划的 `binding.allow_skill_change=true`，且 Adapter 声明 `skill_treatment=true`。Context 原项替换通过组件的 `binding.context_ref` 唯一匹配冻结快照中的 `ref_id` 或 `uri`，先移除原项再注入，其他项完整保留。

Adapter 元数据必须声明 `capabilities.execution_plan="teamevolver.execution-plan.v1"`、`component_kinds` 与 `request_observation="local_dispatch"`，能力随 Loader、BoundFactory 和 GuardedFactory 传递。Adapter 必须在最终模型传输边界调用分支的 `request_observer.dispatch`，发送所捕获的不可变 JSON bytes，并返回 `teamevolver.execution-receipt.v1` receipt。Replay 对照分支本地派发记录核对完整请求、目标正文、位置、顺序、非目标输入和无凭证的目的地址；仅回传 hash、伪造 body、重复注入或后续覆盖均不能通过。返回的 `execution_verification` 区分 `verified`、`unsupported` 与旧链路的 `not_required`。

`adapters/examples/observed_http.py` 是可运行的模型 HTTP 传输参考，需要冻结 `environment.request` 中的 model/tools/options；它不代表已适配任何业务 Agent。当前 AgentsHub 示例没有已验证的独立 Prompt/Context 覆盖 API，遇到计划会明确返回 unsupported。远端单独自报 receipt 不能替代本地最终模型派发证据。这依赖固定版本、受信的 Adapter 代码及其传输实现，不提供对任意上传 Python 或远端服务的密码学证明。A/A 可验证执行一致性；指标不变时继续按原策略判 inconclusive，不能据此声称效果提升。

## 验证

```bash
python -m pytest tests/replay
```
