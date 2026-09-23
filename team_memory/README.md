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
  maintenance/                # DreamCycle compile、快照、维护 Skill
    runtime.py                # 主程序生命周期接入
    integrations/             # DreamCycle 运行集成
    legacy/                   # 历史 ReAct 实现，非当前生产写入链路
  memory_changes.py           # Memory Change 与 Replay 结果账本
  memory_routes.py            # Memory Replay HTTP 路由
  debug_routes.py             # Memory 检索调试路由
  frontend/                   # 页面、Memory Lab、实验面板、类型及前端测试
  tests/                      # 后端测试与离线预览
```

## 集成约定

- `team_memory.*` 是团队 Memory 唯一的 Python 模块接口；不再提供 `teamEvolver.team_memory`、
  `teamEvolver.aggregation`、`teamEvolver.dreamcycle` 或原路由/集成模块的导入别名。
- True Replay 执行实现统一位于同级 `team_replay/`。Memory Change 账本与
  Memory Replay 路由分别由本目录的 `memory_changes.py` 和
  `memory_routes.py` 所有，并通过最小账本 Interface 调用公共执行模块；
  Memory 路由复用主程序的会话检查，不维护第二套身份系统。
- `TeamEvolverConfig` 继承本目录的 `TeamMemoryConfig`；`ConfigStore` 调用本目录的默认值与转换函数。YAML 继续使用 `aggregation`、`dreamcycle` 键，现有配置文件与环境变量不迁移。
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
