# Team Memory

Memory 专属代码集中在本目录，与 `teamEvolver/` 同级；两者仍属于同一个 Python 分发包、同一个 FastAPI 进程。

## 维护入口

```text
team_memory/
  service.py                  # 两阶段编排、任务状态与目标锁
  compile_client.py           # OpenViking compile/tasks/cp HTTP
  config.py                   # 配置字段、默认值、YAML 转换
  routes.py                   # 聚合和双 Skill 配置接口
  aggregation/                # 用户枚举、staging、聚合 Skill、增量状态
  maintenance/                # OV compile 快照、维护 Skill 与调度入口
    runtime.py                # compile-only 主程序生命周期接入
    workflow.py               # ov cp + ov compile 维护阶段
    maintenance_skill.py      # OpenViking 使用的维护 Skill
  memory_changes.py           # Memory Change 与 Replay 结果账本
  memory_routes.py            # Memory Replay HTTP 路由
  debug_routes.py             # Memory 检索调试路由
  frontend/                   # 页面、Memory Lab、实验面板、类型及前端测试
  tests/                      # 后端测试与离线预览
```

## 集成约定

- `team_memory.*` 是团队 Memory 唯一的 Python 模块接口；不再提供
  `teamEvolver.team_memory`、`teamEvolver.aggregation`、`teamEvolver.dreamcycle`
  或旧 Job/ReAct 集成模块。
- True Replay 执行实现统一位于同级 `team_replay/`。Memory Change 账本与
  Memory Replay 路由分别由本目录的 `memory_changes.py` 和
  `memory_routes.py` 所有，并通过最小账本 Interface 调用公共执行模块；
  Memory 路由复用主程序的会话检查，不维护第二套身份系统。
- `TeamEvolverConfig` 继承本目录的 `TeamMemoryConfig`；`ConfigStore` 调用本目录的默认值与转换函数。YAML 仅接受 `memory.maintenance` 与 `memory.aggregation`，维护入口始终唯一。R6 的 `compile.executor: internal` 经 Port 使用隔离 KEE Worker；旧远端任务仍按原执行器查询。
- 前端由 `web-ui/` 的 Vite、TypeScript、Tailwind 编译，使用 `@memory/*` 引用本目录页面，公共 UI 与请求客户端仍通过 `@/*` 引用。没有第二套 React 依赖或独立部署。
- 打包配置同时包含 `teamEvolver*`、`team_memory*` 与 `team_replay*`；Docker 和离线发布脚本必须复制三者。测试与前端源码不属于 Python 运行时包。

## 验证

```bash
python -m pytest team_memory/tests
python -m pytest
npm --prefix web-ui test
npm --prefix web-ui run build
node docs/scripts/check-docs-refs.mjs
python -m team_memory.tests.serve_team_memory_preview --port 52016
```

默认 pytest 同时收集根目录公共测试和本目录测试。预览仅使用模拟数据，不读取生产凭据，也不发起真实 OpenViking 请求。

两阶段流程和远端 URI 保持不变：聚合 compile 写入正式 Memory，`ov cp` 留私有快照，再由维护 compile 写回同一目录。路径迁移不增加原子发布、物理删除或自动回滚能力。

## KEE R6 Compile Port

`internal_compile.py` 复用既有目标锁和 AggregationState，在 metadata 内保留最多 32 个编译检查点。
旧执行器 ID 不转换；新内部执行记录 `kee-memory-` ID。模型输出先保存，再写入通用 OV 内容接口，等待索引时不重复模型或正文写入。
当前内部批次预算为 1 MiB 输入、128 个输出页，模型最多两次尝试；超预算明确阻塞，不静默丢页。
内部 Worker 不执行 Skill 中的脚本或工具。维护不新增删除或原子发布保证；若 Skill 依赖实际工具执行，保留旧执行器配置并先做适配验收。
恢复说明见 [R6 运维](../docs/zh/guides/20-kee-r6.md)。
