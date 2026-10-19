# 健康、状态与任务 API

代码入口：

- 健康与状态：`teamEvolver/observability/api/__init__.py:create_router`
- Worker 健康端口：`teamEvolver/runtime/http_runtime.py:build_health_app`
- 统一任务接口：`teamEvolver/operations/api/__init__.py:create_router`
- 任务状态机：`teamEvolver/runtime/repository.py:WorkerTaskRepository`

## 健康检查

| 角色 | 端口 | 路由 |
| --- | --- | --- |
| `api` | `TEAMEVOLVER_API_PORT`，默认 52010 | `/healthz`、`/readyz`、`/status` 及全部业务接口 |
| `worker` | 默认 52011（`teamEvolver start --role worker --port` 可改） | 仅 `/healthz`、`/readyz` |
| `all` | 同 `api` | 同 `api`，进程内同时运行 Worker |

三个路由均无需认证。

### GET /healthz

存活探针，进程存活即返回 200：

```json
{"ok": true, "role": "api"}
```

### GET /readyz

就绪探针，读取 `teamEvolver/runtime/lifecycle.py:RuntimeLifecycle.ready`。生命周期启动完成后返回 200；以下情况返回 503：启动尚未完成、正在优雅停机、Worker 循环报告 PostgreSQL 不可用、Worker 循环在停机之外意外退出。

```json
{"ready": false, "role": "worker"}
```

停机时先撤销就绪状态，负载均衡摘除副本后再等待在途任务结束。

### GET /status

```json
{"role": "api", "ready": true}
```

`/status` 也允许租户 Key 访问，供 Agent 运行时探测服务。

## 统一任务接口

所有 `202 Accepted` 受理返回的 `job_id` 都是 `worker_tasks.task_id`，可用以下接口操作。租户由请求身份确定，RLS 保证只能看到本租户任务，其他租户的任务返回 404。需要控制台登录或 Root Key。

### GET /api/jobs/{job_id}

```json
{
  "job_id": "3f0c...:session.normalize",
  "kind": "session.normalize",
  "status": "running",
  "attempt": 1,
  "cancel_requested": false,
  "upstream_task_id": "",
  "result_ref": "",
  "error_code": "",
  "created_at": "2026-10-14T08:00:00Z",
  "updated_at": "2026-10-14T08:00:03Z"
}
```

| 状态 | 含义 |
| --- | --- |
| `queued` | 等待 Worker 领取 |
| `running` | Worker 持有租约执行中 |
| `retry_wait` | 失败后按退避等待重试 |
| `waiting_external` | 已提交外部任务（如 OpenViking Compile），等待上游完成；`upstream_task_id` 为上游任务 ID |
| `reconciling` | 结果未知，正在与上游对账 |
| `succeeded` | 成功，`result_ref` 指向结果 |
| `failed` | 终态失败，`error_code` 为原因 |
| `cancelled` | 已取消 |

业务结果用各模块的 `.../jobs/{job_id}/result` 读取，例如 `/api/datasource/jobs/{job_id}/result`、`/api/skill-evolution/jobs/{job_id}/result`。

### POST /api/jobs/{job_id}/cancel

- `queued`、`retry_wait`：立即变为 `cancelled`。
- `running`、`waiting_external`、`reconciling`：置 `cancel_requested=true`，Worker 在安全点停止，不中断不可幂等的外部写入。
- 已是终态：返回当前任务视图。

```json
{"job_id": "...", "status": "running", "cancel_requested": true}
```

### POST /api/jobs/{job_id}/retry

只对 `failed` 任务生效，将其重新置为 `queued`，`attempt` 在下次领取时递增。其他状态返回 `409 only a failed job can be retried (status=<status>)`。

## Worker kind

| kind | 模块 | 来源 |
| --- | --- | --- |
| `session.pull`、`session.normalize`、`session.analyze`、`session.archive` | Session 接入 | 上报、数据源拉取、定时拉取 |
| `skill.evolve` | Skill 进化 | 进化触发、Prompt 测试、模型 / 链路观测测试、OpenViking 目录初始化 |
| `skill.validate` | Skill 验证 | Candidate 评估与验证 |
| `skill.transfer` | Skill 导入导出 | Skill 导入导出 |
| `experience.sync` | 成功经验 | 经验同步、全量导入、Pattern 编译 |
| `memory.aggregate`、`memory.compile`、`memory.replay` | 团队 Memory | Memory 运行、维护、Memory Skill 发布、Memory Replay |
| `miner.track_compile`、`miner.rebuild_index` | 知识库挖掘 | 挖掘任务、上传、编辑、删除 |
| `replay.run`、`dataset.run` | Replay | Replay 运行、Adapter 测试、数据集运行与合成 |

Worker 每轮刷新租户时应用追踪配置，并调度数据源拉取、Skill 进化、Memory 维护与成功经验同步。任务调度由租约和去重键保护，多副本 Worker 可同时运行。

## 服务日志状态

`GET /api/logging/status` 见 [服务日志状态](./15-logging-status.md)。
