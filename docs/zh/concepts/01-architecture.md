# 架构总览

teamEvolver 是一个模块化单体：一份 release、一个镜像，按 `api` 与 `worker` 两种角色运行。API 只做认证、授权、校验、短事务受理和查询；所有长任务以 `202 Accepted + worker_tasks + Worker Handler` 的方式执行。PostgreSQL 是可变业务状态的唯一权威源，OpenViking 是外部知识制品和 Compile Task 的权威源。

## 系统架构图

```text
                 Agent Runtime（v2：租户 Key + user_id）      浏览器控制台（Session Cookie）
                               │                                       │
                               ▼                                       ▼
        ┌──────────────── 负载均衡（无会话粘滞）─────────────────────────────┐
        ▼                                                                  ▼
┌──────────────────────┐                                  ┌──────────────────────┐
│ teamEvolver api ×N   │  create_app()：认证 → 租户上下文 → │ teamEvolver api ×N   │
│ :52010               │  模块 Router → 短事务 → 202       │                      │
└─────────┬────────────┘                                  └─────────┬────────────┘
          │ 写领域记录 + payload + worker_tasks（同一事务）           │
          ▼                                                         ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ PostgreSQL（RLS，按 tenant_id 隔离）                                         │
│ teamevolver_config · tenants · tenant_configs · secret_refs · adapters      │
│ ingestions · sessions · skills · skill_versions · skill_candidates          │
│ datasets · memory_runs · context_* · worker_tasks · resource_leases         │
│ tenant_usage · artifacts · storage_objects · audit_log · schema_versions    │
└─────────────────────────────────────────────────────────────────────────────┘
          ▲ FOR UPDATE SKIP LOCKED 领取，fence 校验写回                       ▲
┌─────────┴────────────┐                                  ┌─────────┴────────────┐
│ teamEvolver worker ×N│ Handler：session.* skill.*        │ teamEvolver worker ×N│
│ 健康端口 :52011       │ replay.* memory.* miner.*        │                      │
└─────────┬────────────┘                                  └─────────┬────────────┘
          │ Compile / Content / Session / Search（Root Key + Account + User）
          ▼
┌─────────────────────────────────────────────────────────────────────────────┐
│ OpenViking：Skill · Memory · Wiki · Compile Task                             │
└─────────────────────────────────────────────────────────────────────────────┘
```

## 模块边界

```text
teamEvolver/            平台层
├── app.py              唯一 FastAPI 工厂与业务组合根（create_app、build_role_services）
├── bootstrap.py        只读启动配置（环境变量 + 可选 YAML）
├── config_runtime.py   PG 中的全局配置与租户模块配置（revision CAS）
├── database/           连接池、事务、RLS、schema_runner 与版本 SQL
├── runtime/            角色入口、生命周期、worker_tasks 仓库、Worker 循环、租约
├── storage/            ObjectStore（PostgreSQL storage_objects；本地实现仅供开发）
├── openviking/         唯一的 OpenViking HTTP/Compile 客户端
├── security/           Secret 加密、AgentPrincipal
├── admin/ tenants/     控制台身份、RBAC、用户、租户
├── operations/         /api/jobs 统一任务接口
├── observability/      健康、就绪、日志状态、Langfuse 出站追踪
├── platform_docs/      控制台入口、平台资产、OpenViking Workspace
├── context/            Agent Context 网关
└── cli/                teamEvolver 命令行
session_ingestion/      Session 推送、数据源拉取、强制分析阶段
team_skills/            Skill 库、Candidate、Evolution、Experience、Transfer、Workbench
team_replay/            True Replay、Test Dataset、ReplayAdapterFactory
team_memory/            团队 Memory 聚合、DreamCycle 维护、Memory Replay
team_miner/             知识库挖掘（OpenViking Compile 跟踪与索引）
web-ui/                 React 控制台源码，构建到 teamEvolver/web/dist/
```

业务模块统一提供 `api/`（`create_router(services, **kwargs)`）、`handlers`、`config.py`、`schemas.py`、`repository.py`。例外：`team_replay/contracts.py` 是跨模块 Replay 契约；`team_miner` 不建 `repository.py`。

## 依赖规则

1. `teamEvolver/database`、`runtime`、`storage`、`openviking`、`llm.py`、`observability` 不导入业务模块。
2. `teamEvolver/app.py` 是唯一组合根，按固定顺序装配 11 个 `api` 包，并拒绝重复的 `(method, path)`。
3. 业务模块不导入 `teamEvolver.app` 或其他模块的 Handler；跨模块只通过公开 Contract、只读 Query、持久化任务或组合根注入的 `services.extras`。
4. Repository 只访问本领域表。
5. FastAPI 只出现在 `teamEvolver/app.py`、`teamEvolver/runtime/http_runtime.py` 和各模块 `api/`。
6. 手写生产代码单文件不超过 1000 行。

这些规则由 `tests/contracts/test_dependencies.py`、`tests/contracts/test_package_contracts.py` 和 `tests/contracts/test_application_contracts.py` 静态检查。

## 请求链路（API 角色）

1. `teamEvolver/runtime/http_runtime.py` 生成 `request_id`/`trace_id`，调用注入的 authorizer。
2. `teamEvolver/admin/authn.py:build_console_authorizer` 解析身份：服务 Root Key、租户 Key（`tevt_`）或控制台 Session Cookie，并绑定 `TenantContext`。
3. 模块 Router 校验权限、参数、幂等键和资源上限。
4. 在一个租户事务内写领域记录、不可变 payload 和 `worker_tasks`，返回 `202`、`job_id` 和 `status_url`（`/api/jobs/{job_id}`）。
5. Router 不直接执行 LLM、Replay、Compile 轮询、批量文件处理或外部投递。

## Worker 执行模型

`teamEvolver/runtime/worker.py:run_worker` 在所有 active 租户之间轮询领取任务：

- 使用 `FOR UPDATE SKIP LOCKED` 领取；领取时递增 `fence`，写结果必须匹配 tenant、owner、attempt 和 fence，迟到的旧 attempt 写入被拒绝。
- 默认租约 60 秒，心跳续约；续约失败后不得发起新的外部副作用。
- 失败重试默认最多 3 次，退避 5/30/120 秒加 jitter。
- 上游结果未知时进入 `reconciling`，按幂等键对账，不盲目重提交。
- `resource_leases` 控制租户并发和 Memory 目标树、经验投递等排他资源；`tenant_usage` 按逻辑任务计数。
- PG 短故障时 Worker 循环按 1、2、4…最多 30 秒退避继续运行，`/readyz` 返回 503；恢复后自动继续。

状态机：

```text
queued -> running -> succeeded
                  -> retry_wait -> queued
                  -> waiting_external -> reconciling -> succeeded/failed
                  -> failed
                  -> cancelled
```

固定 Handler kind 闭集见 `teamEvolver/runtime/tasks.py:TASK_KINDS`：

| 模块 | kind |
| --- | --- |
| session_ingestion | `session.pull`、`session.normalize`、`session.analyze`、`session.archive` |
| team_skills | `skill.evolve`、`skill.validate`、`skill.transfer`、`experience.sync` |
| team_replay | `replay.run`、`dataset.run` |
| team_memory | `memory.aggregate`、`memory.compile`、`memory.replay` |
| team_miner | `miner.track_compile`、`miner.rebuild_index` |

## 生命周期

`teamEvolver/runtime/lifecycle.py:RuntimeLifecycle` 固定关闭顺序：撤销 readiness → 停止领取 → 等待在途任务（默认 30 秒）→ 关闭外部客户端和连接池。负载均衡在 `/readyz` 返回 503 后不再分发新请求。

## 端口约定

| 角色 | 默认端口 | 提供 |
| --- | --- | --- |
| `api` | 52010（`TEAMEVOLVER_API_PORT` 或 `--port`） | 全部 HTTP API、控制台、`/healthz`、`/readyz`、`/status` |
| `worker` | 52011（`--port`） | `/healthz`、`/readyz` |
| `all` | 52010 | 本地开发：API + Worker 同进程 |

完整端点见 [端点索引](../api/00-endpoints.md)。

## 代码入口

| 组件 | 代码路径 |
|------|---------|
| 角色入口 | [entrypoint.py](../../../teamEvolver/runtime/entrypoint.py) |
| 组合根 | [app.py](../../../teamEvolver/app.py) |
| CLI | [cli/](../../../teamEvolver/cli/) |
| Worker 循环 | [worker.py](../../../teamEvolver/runtime/worker.py) |
| 任务仓库 | [repository.py](../../../teamEvolver/runtime/repository.py) |
| Schema 版本 | [schema/](../../../teamEvolver/database/schema/) |
| 配置 | [config_runtime.py](../../../teamEvolver/config_runtime.py)、[bootstrap.py](../../../teamEvolver/bootstrap.py) |
| 前端源码 | [web-ui/src/](../../../web-ui/src/) |
