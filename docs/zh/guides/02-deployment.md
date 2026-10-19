# 生产部署指南

生产环境用同一个镜像分别运行 `api` 与 `worker` 两个角色，连接外部 PostgreSQL 与 OpenViking。两种角色都可以水平扩容，负载均衡不需要会话粘滞。安装与镜像构建见 [安装部署](../getting-started/03-installation.md)。

## 前置条件

| 组件 | 要求 |
| --- | --- |
| PostgreSQL | 14+；预先创建数据库与 Schema，应用角色为 Schema owner，`NOSUPERUSER NOBYPASSRLS` |
| OpenViking | 可访问的服务地址与 Root Key；知识库挖掘需要原生 Compile Runtime |
| 模型 | OpenAI 兼容端点与 Key |
| 镜像 | 由 `cicd_scripts/build.sh` 产出 release 后 `docker build` |

## 角色

| 角色 | 职责 | 端口 | 探针 |
| --- | --- | --- | --- |
| `api` | 认证、校验、短事务受理、查询、控制台静态资源 | 52010 | `/healthz`、`/readyz` |
| `worker` | 领取 `worker_tasks`、执行长任务、定时调度（追踪配置、数据源拉取、经验同步） | 52011（仅健康） | `/healthz`、`/readyz` |
| `all` | API 与 Worker 同进程，仅用于 `TEAMEVOLVER_ENV=local` | 52010 | 同 `api` |

Worker 之间通过 PostgreSQL 租约与 fence 协调：任务被领取后持有租约，崩溃或超时后由其他 Worker 接管，迟到的结果因 fence 不匹配被拒绝。

## 环境文件

`deploy/.env`（不入库，权限 0600）：

```bash
TEAMEVOLVER_PG_DSN=postgresql://te_app:<password>@db:5432/teamevolver
TEAMEVOLVER_PG_SCHEMA=teamevolver
TEAMEVOLVER_ROOT_API_KEY=<random 32+ bytes>
TEAMEVOLVER_SECRET_MASTER_KEY=<random 32+ bytes>
TEAMEVOLVER_OV_ENDPOINT=http://openviking:1933
TEAMEVOLVER_OV_API_KEY=<OpenViking Root Key>
TEAMEVOLVER_LLM_BASE_URL=https://api.openai.com/v1
TEAMEVOLVER_LLM_API_KEY=<model key>
TEAMEVOLVER_LLM_MODEL=gpt-4o-mini
TEAMEVOLVER_TENANT_TOKEN=tevt_<random>
TEAMEVOLVER_ENV=production
```

生成随机值：`python3 -c 'import secrets;print(secrets.token_urlsafe(32))'`。`TEAMEVOLVER_SECRET_MASTER_KEY` 一经使用不能更换，否则已保存的 Secret Ref 无法解密。完整字段见 [配置参考](./01-configuration.md)。

可选的只读 YAML 放在 `deploy/config/config.yaml`，容器入口自动设置 `TEAMEVOLVER_CONFIG_FILE`。

## 初始化数据库

在任一实例（或安装了 release 的运维机）执行：

```bash
teamEvolver schema apply --dry-run
teamEvolver schema apply
teamEvolver schema status
```

`schema apply` 幂等，按版本执行 `teamEvolver/database/schema/` 下的脚本并记录到 `schema_versions`，同时为每个租户写入模块默认配置。应用进程不会在启动时建表。
首次安装使用空数据库与新的 Schema 基线。

## Docker Compose

仓库根目录的 `compose.yaml` 从同一镜像启动 `teamevolver-api` 与 `teamevolver-worker`，读取 `deploy/.env`：

```bash
TARGET_ARCH=x86_64 sh cicd_scripts/build.sh
docker compose build
docker compose up -d
docker compose ps
```

两个服务的健康检查都访问各自端口的 `/readyz`。

## 容器入口

镜像入口 `container_start.sh`：

1. 存在 `/app/deploy/.env` 时加载。
2. `TEAMEVOLVER_CONFIG_DIR`（默认 `/app/deploy/config`）下存在 `config.yaml` 且未设置 `TEAMEVOLVER_CONFIG_FILE` 时使用它。
3. `TEAMEVOLVER_API_HOST` 默认 `0.0.0.0`。
4. `TEAMEVOLVER_ROLE` 必须为 `api` 或 `worker`，否则以 64 退出。
5. `APP_PORT` 或 `TEAMEVOLVER_PORT` 存在时作为 `--port` 传入。

Kubernetes 中把 API 与 Worker 部署为两个 Deployment，使用同一镜像和 Secret，仅 `TEAMEVOLVER_ROLE` 与端口不同。`readinessProbe` 与 `livenessProbe` 分别指向 `/readyz` 与 `/healthz`。

## 单机进程

```bash
teamEvolver start --role api --daemon --log-file /var/log/teamevolver/api.log
teamEvolver status
teamEvolver stop
```

`--daemon` 在后台启动一个进程并写入 PID 文件，同一台机器只能有一个 daemon。需要同时运行 API 与 Worker 时，用 systemd 等进程管理器分别前台启动两个进程：

```ini
[Service]
EnvironmentFile=/etc/teamevolver/env
ExecStart=/opt/teamEvolver-venv/bin/teamEvolver start --role worker
Restart=on-failure
KillSignal=SIGTERM
TimeoutStopSec=60
```

## 反向代理

API 前可以放 Nginx 或云负载均衡：

- 转发全部路径到 API 实例的 52010 端口，不需要会话粘滞。
- `POST /ingest_session` 的请求体可达 32 MiB，Replay 与 Skill 写入可达 96 MiB，相应调整 `client_max_body_size`。
- 保留 `X-Request-ID` 与 `X-Trace-ID` 请求头。
- 控制台 Cookie 名称包含 host、port 与 root_path 的哈希，经过代理时保持 `Host` 头一致。
- Worker 的 52011 端口只需对探针开放。

## 停机与升级

实例收到 SIGTERM 后：

1. 撤销 readiness，`/readyz` 返回 503。
2. Worker 停止领取新任务。
3. 等待在途任务完成，最多 30 秒；未完成的任务租约到期后由其他 Worker 接管。
4. 关闭数据库连接池。

升级步骤：

1. 构建新 release，在预发环境离线安装并执行 `teamEvolver schema apply --dry-run`。
2. 执行 `teamEvolver schema apply`。Schema 只向前演进。
3. 逐个替换 API 与 Worker 实例。

应用回滚只能回到兼容当前 Schema 的 release。不执行破坏性数据库回滚。

## 备份

PostgreSQL 是 TeamEvolver 可变状态的唯一权威源，按数据库常规方式备份（`pg_dump` 或物理备份 + WAL 归档）。OpenViking 中的知识制品按 OpenViking 的备份方式处理。本机目录（`skills_runtime_cache_dir`、日志）都可以重建，不需要备份。

备份中包含 `secret_refs` 密文；恢复时必须使用相同的 `TEAMEVOLVER_SECRET_MASTER_KEY`。

## 资源建议

| 角色 | CPU | 内存 | 说明 |
| --- | --- | --- | --- |
| API | 1–2 核 | 1–2 GiB | 受理与查询为短事务 |
| Worker | 2–4 核 | 2–4 GiB | 模型调用并发由 `TEAMEVOLVER_LLM_CONCURRENCY` 控制；ZIP / 文档转换占用内存 |

PostgreSQL 连接数按 `(storage_pg_pool_max + storage_pg_control_pool_max) × 实例数` 预留。

## 安全

- `TEAMEVOLVER_ROOT_API_KEY`、`TEAMEVOLVER_SECRET_MASTER_KEY`、`TEAMEVOLVER_OV_API_KEY`、模型 Key 只通过环境变量或 Secret 注入，不写入 YAML 与镜像。
- 租户 Key 只交给 Agent 服务端。轮换见 [控制台身份与租户权限](../api/20-console-rbac.md#租户与成员)。
- 数据库应用角色不能是 SUPERUSER 或 BYPASSRLS，租户隔离依赖 RLS。
- 对外只暴露 API 端口，并通过 TLS 终止。
