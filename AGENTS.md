# AGENTS.md

面向 coding agent 的工程指引。项目定位、架构与术语见 [README.md](./README.md) 与 [CONTEXT.md](./CONTEXT.md)（修改文档或代码时必须使用 CONTEXT.md 中的统一术语，遵守各词条的 Avoid 约定）。

## 项目速览

teamEvolver 是 Agent 团队能力进化控制面：FastAPI 服务（默认端口 52010）+ React 控制台，Python 3.10+。核心链路：Session/Evidence ingest → Skill Evolution → True Replay 验证 → 候选评审发布 → Agent 分发；另有 DreamCycle 团队 Memory 进化与 SkillMiner 文档挖掘。上下文存储为 OpenViking。

## 常用命令

```bash
# 环境
python3 -m venv .venv && source .venv/bin/activate
python -m pip install -e ".[all,dev]"
npm --prefix web-ui ci

# 一键验证（Python 编译 + pytest + 前端生产构建）
bash scripts/verify_local.sh

# 测试（自动收集公共、Memory、Miner 和 Replay 测试）
python -m pytest
python -m pytest tests/test_xxx.py::test_case   # 单测

# Lint（ruff：line-length 120，select E/F/I）
ruff check teamEvolver team_skills team_memory team_miner team_replay session_ingestion tests

# 服务
teamEvolver start --daemon
teamEvolver status / doctor / config show / stop

# 前端构建（仅改动 web-ui/ 时需要；仓库已含构建产物）
npm --prefix web-ui run build

# 文档引用检查（改动 docs/ 后必须跑）
node docs/scripts/check-docs-refs.mjs
```

## 代码结构

```text
teamEvolver/          # Python 包（FastAPI 单服务承载全部模块）
├── proxy/            # FastAPI 路由、控制台、Workspace 接口与各模块桥接
├── integrations/    # 租户主体、Context、Skill pull 与迁移期兼容入口
├── replay.py        # team_replay 虚拟兼容入口（另有 true_replay.py / progressive_replay.py 等别名）
├── tenants/          # 多租户上下文与配置继承
├── storage/          # OpenViking / 本地存储与 PG 适配
├── config_store/     # YAML 配置默认值与持久化
├── observability/    # Langfuse 追踪运行时
├── admin/ · cli/     # 管理工具与命令行入口
└── web/              # 控制台静态产物
session_ingestion/    # Session 接入：Agent Push、数据源 Pull 与租户 Adapter
team_skills/          # Skill 库（library/）、候选校验发布（candidates/）与 Skill Evolution 流水线（evolution/）
team_miner/           # Skill 挖掘、Benchmark、任务编排、前端、测试与主服务桥接
team_memory/          # Memory 专属后端、配置、路由、frontend/ 和 tests/
team_replay/          # True Replay Interface、执行引擎、策略与 Runtime Adapter
web-ui/               # React + TypeScript 控制台外壳与公共 UI
tests/                # 单元、集成、协议和回放测试
docs/                 # 中英双语文档源
```

## 工程硬约束

存储：

- 高频写路径（session queue、session_index、filter audit、evidence、registry、manifest 等）必须走本地存储，不得直写 OpenViking。
- OpenViking 不可用时的本地回退走 `LocalObjectStore`（线程安全、原子写、按实例隔离目录）。

并发与异步：

- 路由中的同步 viking I/O 必须包 `asyncio.to_thread`，不得阻塞事件循环。
- LLM 调用使用专用 `ThreadPoolExecutor` + 全局 `asyncio.Semaphore(≤8)`，并行度不得超标。
- 背压必须有界：429 响应、Semaphore、有界队列，禁止无界内存队列。

进程与服务：

- 进程操作只用精确 PID 或全路径唯一匹配，禁止 `pgrep -f teamEvolver` 之类宽匹配。
- 后台任务必须记录 PID 文件（`/tmp/te_*.pid`）；重启前确认端口释放（`lsof -t -iTCP:52010 -sTCP:LISTEN`）。
- 代码改动后必须重启服务，新配置和修复才生效。

其他：

- 修改 Memory 代码前阅读 `team_memory/README.md` 的维护入口与集成约定；`team_memory.*`
  是唯一模块接口，不得恢复 `teamEvolver.team_memory`、`teamEvolver.aggregation` 或
  `teamEvolver.dreamcycle` 等旧导入别名，也不要恢复旧 ReAct 写入调度。

- Skill 发布统一走 `SkillMutationService`；pull 模式保留版本与 tombstone，不创建 delivery outbox。

- True Replay 新实现统一放在根目录 `team_replay/`；该包通过 `ReplayHost` Adapter 获取宿主能力，保持对 `teamEvolver`、`team_memory` 和 `team_miner` 的单向依赖。旧 `teamEvolver.replay.*` 只保留虚拟兼容别名。

## 提交前验证清单

1. `python -m compileall teamEvolver team_skills team_memory team_miner team_replay session_ingestion tests` 无错误。
2. `python -m pytest` 通过。
3. 改动了 `web-ui/` → `npm --prefix web-ui run build` 通过。
4. 改动了 `docs/` → `node docs/scripts/check-docs-refs.mjs` 通过，并遵循 [文档维护指南](./docs/zh/api/99-docs-maintenance.md)。

## Agent 协议迁移

涉及 Agent 身份或迁移发布时，先读 [落地方案](./agent-deregistration-plan.md)。以方案中的租户 Key + 声明 user_id 为准；同名用户按租户隔离。阶段 5 的数据操作需要观察期证据；阶段 6 单独发布。
