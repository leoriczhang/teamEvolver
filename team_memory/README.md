# Team Memory

团队 Memory 模块：把 Account 内各用户的个人 Memory 聚合为共享团队 Memory，用 DreamCycle 维护，并把每次变更记录为可 Replay 的 Memory Change。概念与配置见 [Memory 体系](../docs/zh/concepts/04-memory.md)，接口见 [团队 Memory API](../docs/zh/api/11-team-memory-aggregation.md)。

## 文件

```text
team_memory/
  api/__init__.py           # 运行受理、设置、状态与 Memory Change 查询
  api/skills.py             # 聚合 Skill 与维护 Skill 的读取和发布
  api/replay.py             # Memory Replay 受理
  runs.py                   # 短事务受理：memory_runs + memory.aggregate 任务
  handlers.py               # Worker Handler：Account 租约、阶段执行、Memory Change
  pipeline.py               # 聚合、确定性 staging、tree-reduce Compile、维护阶段
  merge.py                  # 带检查点的 Compile 分组
  repository.py             # memory_runs、memory_stages、memory_checkpoints、memory_changes
  changes.py                # 读取 Memory Change 前后快照
  replay.py                 # 把 Memory Change 转换为单 Case ReplaySpec
  openviking.py             # Memory 使用的 OpenViking 身份与路径
  config.py / schemas.py    # memory 模块配置与任务输入
  aggregation/              # 用户枚举、staging 快照、聚合 Skill、增量状态
  maintenance/maintenance_skill.py  # 维护 Skill 默认内容
  tests/
```

## 约定

- Memory 运行由 `/api/aggregation/run` 或 `/api/memory/maintenance/run` 受理，作为 `memory.aggregate` 任务在 Worker 执行；Skill 发布为 `memory.compile`，Memory Replay 为 `memory.replay`。
- 同一 OpenViking Account 同时只运行一个 Memory 任务，由 `resource_leases` 的 `memory:account:<account>` 租约保证。
- 运行状态、阶段与检查点保存在 PostgreSQL；Worker 重领后从已完成阶段续跑。
- OpenViking 调用经 `teamEvolver/openviking/` 客户端；本模块不导入其他业务模块的 Handler。
- Memory Replay 只使用 `team_replay/contracts.py` 的公开契约。

## 验证

```bash
python -m pytest team_memory/tests
python -m pytest tests/memory/test_jobs_pg.py
```
