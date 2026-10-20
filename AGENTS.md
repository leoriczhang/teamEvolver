# AGENTS.md

面向 coding agent 的工程指引。项目定位与架构见 [README.md](./README.md) 与 [架构总览](./docs/zh/concepts/01-architecture.md)，术语见 [CONTEXT.md](./CONTEXT.md)（修改文档或代码时使用其中的统一术语，遵守各词条的 Avoid 约定）。

## 项目速览

teamEvolver 是 Agent 团队能力进化控制面：一个模块化单体，同一镜像以 `api`（默认端口 52010）与 `worker`（健康端口 52011）两种角色运行，React 控制台由 `api` 提供，Python 3.11。PostgreSQL 是可变业务状态的唯一权威源，OpenViking 保存知识制品与 Compile Task。核心链路：Session 接入 → 强制分析 → Skill Evolution → True Replay 验证 → Candidate Review 与 Publish → Agent pull；另有团队 Memory 聚合与维护、成功经验、知识库挖掘。

## 常用命令

```bash
# 环境（按 requirements.lock 带 hash 安装，再 --no-deps -e .）
bash scripts/install_teamEvolver.sh
source .venv/bin/activate
npm --prefix web-ui ci

# 一键验证（Python 编译 + pytest + 前端测试与生产构建）
bash scripts/verify_local.sh

# 测试
python -m pytest
python -m pytest tests/test_xxx.py::test_case

# Lint（ruff：line-length 120，select E/F/I）
ruff check teamEvolver session_ingestion team_skills team_replay team_memory team_miner tests

# 数据库 Schema（应用进程不在启动时建表）
teamEvolver schema apply
teamEvolver schema status

# 服务
teamEvolver start --role all          # 仅 TEAMEVOLVER_ENV=local
teamEvolver start --role api
teamEvolver start --role worker
teamEvolver status
teamEvolver config show
teamEvolver stop

# 前端构建（改动 web-ui/ 时）
npm --prefix web-ui run build

# 文档检查（改动 docs/ 或路由、CLI、环境变量后）
node docs/scripts/check-docs-refs.mjs --strict
python -m pytest tests/contracts/test_docs.py
```

## 代码结构

```text
teamEvolver/            平台层
├── app.py              唯一 FastAPI 工厂与业务组合根
├── bootstrap.py        只读启动配置（环境变量 + 可选 YAML）
├── config_runtime.py   PG 全局配置与租户模块配置（revision CAS）
├── database/           连接池、事务、RLS、schema/ 版本 SQL
├── runtime/            角色入口、生命周期、worker_tasks、Worker 循环、租约
├── storage/            ObjectStore（PostgreSQL storage_objects）
├── openviking/         唯一的 OpenViking HTTP/Compile 客户端
├── admin/ tenants/     控制台身份、RBAC、租户
├── context/            Agent Context 网关
└── cli/                命令行
session_ingestion/      Session 上报、数据源拉取、强制分析阶段
team_skills/            Skill 库、Candidate、Evolution、成功经验、导入导出、实验工作台
team_replay/            True Replay、Test Dataset、Replay Adapter
team_memory/            团队 Memory 聚合、DreamCycle 维护、Memory Replay
team_miner/             挖掘模块组合入口
├── knowledge_miner/    知识库挖掘，资料 → Wiki
└── skill_miner/        Skill 挖掘，资料 → Skill 包
web-ui/                 React + TypeScript 控制台，构建到 teamEvolver/web/dist/
tests/                  按领域组织的单元、集成、架构契约和验收测试
docs/                   中英双语文档
```

业务模块提供 `api/`（`create_router(services, **kwargs)`）、Handler、配置和 Repository，由 `teamEvolver/app.py` 装配。Schema 随实际领域契约组织，不创建空文件占位。

Python 测试只从 `tests/` 收集，按领域归档，命名使用职责而非实施阶段。公共 fixture、fake 和数据工厂放在 `tests/support/`，禁止从 `test_*.py` 相互导入。具体约定见 [测试指南](./tests/README.md)；历史实施证据不作为长期测试的运行依赖。

## 工程硬约束

分层与依赖：

- Router 只做认证、校验和短事务受理；LLM、Replay、Compile 轮询、批量文件处理和外部投递一律写入 `worker_tasks` 返回 `202`，由 Worker Handler 执行。不引入独立 Service 层。
- `teamEvolver/database`、`runtime`、`storage`、`openviking`、`llm.py`、`observability` 不导入业务模块；业务模块不导入 `teamEvolver.app` 或其他模块的 Handler。
- FastAPI 只出现在 `teamEvolver/app.py`、`teamEvolver/runtime/http_runtime.py` 和各模块 `api/`。
- Repository 只访问本领域表。手写生产代码单文件不超过 1000 行。
- 这些规则由 `tests/contracts/test_dependencies.py`、`tests/contracts/test_package_contracts.py`、`tests/contracts/test_application_contracts.py` 检查。

存储与配置：

- 可变状态只写 PostgreSQL；本机目录只放临时文件、日志、PID 和可重建缓存。PG 或 OpenViking 故障时不回退本机目录。
- Schema 变更只追加新的版本 SQL 到 `teamEvolver/database/schema/`，已发布的版本文件不改。
- 租户模块配置缺失时返回 503，不回退 YAML；配置写入使用 `expected_revision`，长任务固定受理时的 revision。
- 凭证只以 Secret Ref 保存，接口与日志不出现明文。

并发：

- 模型调用经 `teamEvolver/llm.py` 的专用线程池与全局并发槽（`TEAMEVOLVER_LLM_CONCURRENCY`，默认 8）；队列有界，背压用 429 或有界队列。
- 路由中的同步 I/O 用 `asyncio.to_thread`，不阻塞事件循环。
- 跨副本互斥用 `resource_leases` 或数据库行锁，不依赖进程内锁。

Skill 与 Replay：

- 团队 Skill 变更统一经 `team_skills/library/repository.py:SkillLibraryRepository`，在一个事务内写版本、文件与幂等记录；分发只走 `GET /sync/skills` pull，不创建投递 outbox。
- `team_replay/` 不导入 `team_memory` 或其他模块的 Handler，只使用平台层与其他模块的公开 Query（如 `session_ingestion/queries.py`、`team_skills/library/queries.py`）；纯执行引擎通过 `ReplayHost` 获取宿主能力。

进程：

- 进程操作只用精确 PID，禁止 `pgrep -f teamEvolver` 之类宽匹配；`--daemon` 的 PID 文件由 `teamEvolver/runtime_state.py:pid_file_path` 决定。
- 代码改动后重启服务才生效。

## 提交前验证清单

1. 上面的 `ruff check` 无错误。
2. `python -m pytest` 通过。
3. 改动了 `web-ui/`：`npm --prefix web-ui test` 与 `npm --prefix web-ui run build` 通过。
4. 改动了 `docs/`、路由、CLI 或环境变量：`node docs/scripts/check-docs-refs.mjs --strict` 与 `python -m pytest tests/contracts/test_docs.py` 通过，并遵循 [文档维护指南](./docs/zh/api/99-docs-maintenance.md)。
