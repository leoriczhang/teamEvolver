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

# 测试（testpaths = tests/）
python -m pytest
python -m pytest tests/test_xxx.py::test_case   # 单测

# Lint（ruff：line-length 120，select E/F/I）
ruff check teamEvolver tests

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
├── evolve/           # Evidence、Skill Evolution、Dataset、发布
├── validation/       # Candidate 队列与 True Replay Worker
├── dreamcycle/       # 团队 Memory 进化与 Memory Replay
├── aggregation/      # 跨 User 团队记忆聚合
├── integrations/     # Agent Protocol V1、Hermes、Langfuse、Replay Adapter
├── proxy/            # FastAPI 路由、控制台与 Workspace 接口
├── skills/           # Bundle、版本、SkillMutationService（所有 Skill 变更唯一入口）
├── storage/          # OpenViking / 本地存储适配
├── config_store/     # YAML 配置默认值与持久化
├── skillminer/       # 文档技能挖掘
└── web/              # 控制台静态产物
web-ui/               # React + TypeScript 控制台源码
tests/                # 单元、集成、协议和回放测试
docs/                 # 中英双语文档源
```

## 工程硬约束

存储：

- 高频写路径（session queue、session_index、filter audit、evidence、registry、manifest 等）必须走本地存储，不得直写 OpenViking。
- session_index 采用单写者模式（合并队列 + 周期 flush），禁止读-改-写竞态；批量写必须用 `default_mode="upsert"`。
- 存储操作需带重试（5xx/连接错误，2-5s 退避）；重试逻辑必须放在锁外，锁内禁止 sleep。
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

- Skill 发布统一走 `SkillMutationService`（outbox + tombstone），不得绕过直接写存储。

## 提交前验证清单

1. `python -m compileall teamEvolver tests` 无错误。
2. `python -m pytest` 通过。
3. 改动了 `web-ui/` → `npm --prefix web-ui run build` 通过。
4. 改动了 `docs/` → `node docs/scripts/check-docs-refs.mjs` 通过，并遵循 [文档维护指南](./docs/zh/api/99-docs-maintenance.md)。

## Agent skills

### Issue tracker

Issues and specs live as GitHub issues on https://github.com/leoriczhang/teamEvolver. See `docs/agents/issue-tracker.md`.

### Triage labels

Default five canonical labels (`needs-triage`, `needs-info`, `ready-for-agent`, `ready-for-human`, `wontfix`). See `docs/agents/triage-labels.md`.

### Domain docs

Single-context layout: root `CONTEXT.md` + `docs/adr/`. See `docs/agents/domain.md`.
