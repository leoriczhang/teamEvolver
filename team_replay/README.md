# Team Replay

`team_replay` 是统一的 True Replay 执行模块，与 `teamEvolver/`、
`team_memory/`、`team_miner/` 同级。它消费 Test Dataset 和不可变的
Baseline/Candidate treatment，负责真实 Agent Runtime 执行、Checklist
完成门禁、效率比较和结果聚合。

## Interface

```python
from team_replay.contracts import skill_replay_spec
from team_replay.gateway import DisabledReplayGateway, EmbeddedReplayGateway
```

- `ReplaySpec`：版本化的运行输入，包含 Account、被验收资产、Test Dataset、
  Baseline/Candidate、Runtime、策略和资源限制。
- `ReplayGateway.evaluate(...)`：唯一执行入口。
- `DisabledReplayGateway`：未启用 Replay 时返回 `not_run/inconclusive`，
  不把“未运行”误判为验收失败。
- `EmbeddedReplayGateway`：当前进程内执行；后续独立 Worker 可以实现同一
  Interface，而不改变上游项目。

当前唯一策略为 `checklist-first-efficiency-v1`：先检查 Candidate 是否完成
Checklist，再在完成门禁通过后比较交互轮次、工具调用和 Token。Skill 与
Memory 暂时共用该策略。

## Ownership

```text
team_replay/
  contracts.py       # ReplaySpec、Treatment 和结果 schema
  gateway.py         # 可选执行 Interface 与 Adapter
  protocol.py        # Agent Replay wire contract
  engine.py          # Skill/Agent 双分支执行与沙箱
  memory.py          # Memory before/after 双分支编排
  policy.py          # Checklist-first 决策
  metrics.py         # 效率指标
  aggregation.py     # 多 Case/窗口聚合
  adapters.py        # HTTP/映射/本地 Runtime Adapter
  deap.py            # DEAP Runtime Adapter
  model_broker.py    # 隔离执行的模型代理
  turn_server.py     # Agent 侧逐轮执行端点
```

下列业务逻辑不属于 Replay：

- Test Dataset 的挖掘与编译归 `team_miner`。
- Memory Change 账本、前后快照和 HTTP 路由归 `team_memory`。
- Skill Candidate、静态检查、Candidate Review 与 Publish 归
  `teamEvolver`。

## Dependency Rule

`team_replay` 不得导入 `teamEvolver`、`team_memory` 或 `team_miner`。
宿主配置、Session、Context Snapshot、Skill materialization 和
Agent Registry 通过 `ReplayHost` Adapter 注入。上游可以依赖
`team_replay` 的轻量 contract，但 Replay 不反向发现或读取上游目录。

`teamEvolver.replay.*` 与 `team_memory.replay.*` 在兼容期内仅由虚拟兼容模块提供，不保留重复目录。

## Validation

```bash
python -m pytest team_replay/tests
python -m pytest tests/test_true_replay_efficiency.py
python -m pytest team_memory/tests/test_memory_true_replay.py
```

## Session 数据集控制台

左侧「数据集」保存从运行总览选取的 Session 集合。它与 Skill Candidate
同源生成的 Test Dataset 分开存储；不会改变 Skill Lab 和进化验收的数据集。

- `datasets/collections.py` 保存 Session 快照、可编辑 Query/Checklist 和批次记录，
  存储由宿主注入，落在当前租户的本地状态库（本地文件或 PostgreSQL）。
- `datasets/batch.py` 复用 `ReplayAdapterFactory`、`run_branch` 和 Checklist 裁判。
  每条 Session 执行单个独立分支，不注入 Skill Candidate；默认 Checklist 为
  原始首轮请求，可在运行前细化。它不产生 Baseline/Candidate 的效率比较或发布结论。
- 每个数据集最多 500 条、64 MiB。全服务最多两个批次，每批最多两个执行线程。
  任务通过 HTTP 202 提交；停止操作等待当前条目结束，不再领取后续条目。
  重启后未结束批次标记为中断，保留已完成结果。
- ZIP 包含 `dataset.json`、`cases.jsonl` 和 `sessions/<item_id>.json`。
  批次另外保存提交时的完整输入，之后修改或移除数据集条目不影响历史追溯。

宿主 REST API 位于 `teamEvolver/proxy/dataset_routes.py`：
`/api/datasets`（列表、创建），`/{dataset_id}`（详情、编辑、删除），
`/{dataset_id}/items`（添加），`/items/{item_id}`（快照、编辑、移除），
`/{dataset_id}/export`（ZIP），`/{dataset_id}/runs`（批次列表、提交），
`/runs/{run_id}`（进度），`/runs/{run_id}/cancel`（停止），
`/runs/{run_id}/items/{item_id}`（历史输入），
`/runs/{run_id}/results/{item_id}`（结果及交互轨迹）。
