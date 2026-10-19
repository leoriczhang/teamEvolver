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

## 验证

```bash
python -m pytest team_replay/tests
python -m pytest tests/replay/test_engine.py tests/replay/test_jobs_pg.py
```
