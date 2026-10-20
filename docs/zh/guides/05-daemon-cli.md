# 运维 CLI

`teamEvolver` 命令由 `teamEvolver/cli/__init__.py` 定义，安装 wheel 或 `pip install -e .` 后可用。命令分为两类：

- **本地命令**：直接读取启动配置或连接 PostgreSQL，包括 `start`、`stop`、`status`、`config`、`schema`
- **API 命令**：通过 HTTP 调用正在运行的 `api` 角色，包括 `datasource`、`skills`、`validation`

所有命令都从环境变量和 `TEAMEVOLVER_CONFIG_FILE` 指向的 YAML 读取启动配置，字段见 [配置参考](./01-configuration.md)。

## 进程管理

### start

```bash
teamEvolver start [--role api|worker|all] [--port PORT] [-d|--daemon] [--log-file PATH]
```

| 选项 | 说明 |
|------|------|
| `--role` | 运行角色，默认取 `TEAMEVOLVER_ROLE`，再默认 `api` |
| `--port` | `api` / `all` 时为 HTTP 端口（默认 `proxy_port`，即 52010）；`worker` 时为健康检查端口（默认 52011） |
| `-d, --daemon` | 后台启动，PID 写入 `~/.teamEvolver/teamEvolver.pid` |
| `--log-file` | 日志文件；后台模式未指定时标准输出写入 `/tmp/te_runtime.log` |

启动约束：

- 必须配置 PostgreSQL（`TEAMEVOLVER_PG_DSN` 或 `TEAMEVOLVER_PG_*`），否则直接退出
- `all` 只允许 `TEAMEVOLVER_ENV=local`，生产环境分别运行 `api` 与 `worker`
- 数据库 Schema 不会在启动时创建，首次部署先执行 `teamEvolver schema apply`
- 后台模式检测到已记录的 PID 仍存活时拒绝重复启动；`TEAMEVOLVER_MULTI_REPLICA=true` 时每个进程使用独立 PID 文件

`api` 角色提供控制台与全部 HTTP 接口，不执行任务；`worker` 角色领取 `worker_tasks` 执行，并只暴露 `/healthz`、`/readyz`。

### stop 与 status

```bash
teamEvolver status
teamEvolver stop
```

`status` 读取 PID 文件并输出是否在运行。`stop` 向记录的 PID 发送 SIGTERM，进程先撤销 readiness，再等待在途任务结束后退出。

## 配置与 Schema

### config

```bash
teamEvolver config
```

输出合并后的启动配置，密钥字段已脱敏。命令只读；租户与模块的在线配置通过控制台或 API 修改。

### schema

```bash
teamEvolver schema status
teamEvolver schema apply --dry-run
teamEvolver schema apply
```

`status` 列出 `teamEvolver/database/schema/` 中的版本与校验和。`apply` 按版本顺序执行未应用的 SQL 并记录到 `schema_versions`，可重复执行；`--dry-run` 只列出待应用版本。

## API 命令

API 命令默认访问 `http://127.0.0.1:<proxy_port>`，`--url` 可指定其他地址；`--tenant` 指定租户。

### datasource

```bash
teamEvolver datasource --tenant demo status
teamEvolver datasource --tenant demo bind example-doris.py
teamEvolver datasource --tenant demo test
teamEvolver datasource --tenant demo list --from-timestamp ... --to-timestamp ... --max-sessions 20
teamEvolver datasource --tenant demo pull --from-timestamp ... --to-timestamp ...
```

使用 `TEAMEVOLVER_ROOT_API_KEY`。`list` 与 `pull` 支持 `--from-timestamp`、`--to-timestamp`、`--session-id`、`--user-id`。`--tenant` 默认 `default`。详见 [数据源接入指南](./10-datasource-onboarding.md)。

### skills

```bash
teamEvolver skills --tenant demo list
teamEvolver skills --tenant demo pull --user-id alice
```

- `list` 使用 `TEAMEVOLVER_ROOT_API_KEY` 调用 `GET /api/skills`
- `pull` 使用租户 Key `TEAMEVOLVER_TENANT_TOKEN` 调用 `GET /sync/skills`，把可见 Skill 写入本地缓存 `skills_runtime_cache_dir`（默认 `~/.teamEvolver/runtime/skills`）

本地缓存可随时删除重建，权威版本始终在服务端 SkillHub。

### validation

```bash
teamEvolver validation --tenant demo status
```

使用 `TEAMEVOLVER_ROOT_API_KEY` 调用 `GET /api/skill-candidates?scope=open`，列出待验证或待评审的 Candidate。

## 退出码

命令成功返回 0。参数错误、缺少必需环境变量或 HTTP 非 2xx 时返回非 0，并在标准错误输出原因；数据库驱动错误不回显 SQL 内容。

## 相关文档

- [部署指南](./02-deployment.md)
- [配置参考](./01-configuration.md)
- [故障排查](./06-troubleshooting.md)
