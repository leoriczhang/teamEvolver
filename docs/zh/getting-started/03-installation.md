# 安装部署

teamEvolver 有两种安装方式：开发者从源码目录安装，交付环境从不可变 release 离线安装。两者安装的是同一组 Python 包（`teamEvolver`、`session_ingestion`、`team_skills`、`team_replay`、`team_memory`、`team_miner`）。

## 环境要求

| 组件 | 版本 | 说明 |
|------|------|------|
| Python | 3.11（最低 3.10） | release 镜像为 `python:3.11-slim-trixie` |
| PostgreSQL | 14+ | 应用角色必须是 NOSUPERUSER、NOBYPASSRLS |
| OpenViking | 兼容 Content、Session、Search、Compile API | 知识制品权威源；容器内置固定版本 `ov` CLI |
| Node.js / npm | 20+ | 仅构建控制台时需要 |
| Docker | BuildKit | 构建 release 与镜像 |
| uv | 任意新版本 | 仅重新生成锁文件时需要 |

## 依赖来源

- `pyproject.toml` 是 Python 直接依赖的唯一来源，只有 `dev` 一个 extra。
- `requirements.lock`（运行依赖）和 `build-requirements.lock`（构建工具）由 `scripts/lock_dependencies.sh` 生成，每个包都带 SHA-256，覆盖 CPython 3.11 的 Linux amd64/arm64。
- 修改 `pyproject.toml` 后必须重新执行 `scripts/lock_dependencies.sh`。

## 开发安装

```bash
bash scripts/install_teamEvolver.sh            # 可选 --venv-dir、--python
bash scripts/install_teamEvolver.sh --apply-schema   # 同时执行 schema apply（需要 TEAMEVOLVER_PG_DSN）
source .venv/bin/activate
```

脚本步骤：创建 venv → `pip install --require-hashes -r requirements.lock` → `pip install --no-deps -e .` → 安装 ruff、pytest 等开发工具 → `pip check`。

控制台源码在 `web-ui/`，构建产物写入 `teamEvolver/web/dist/` 并随 wheel 打包：

```bash
npm --prefix web-ui ci
npm --prefix web-ui test
npm --prefix web-ui run build
```

## 构建 release

```bash
TARGET_ARCH=x86_64 sh cicd_scripts/build.sh     # 或 TARGET_ARCH=aarch64
```

构建顺序：

1. 前端 `npm ci`、测试、生产构建（`SKIP_FRONTEND=1` 可复用已有产物）。
2. 按目标镜像的解释器版本和 manylinux 架构下载 hash 校验过的 wheelhouse；只发布 sdist 的依赖（如 `jieba`）保留锁定的 sdist。
3. 在运行时镜像内以 `--network none` 构建应用 wheel。
4. 生成 `release/manifest.json`（版本、目标镜像 digest、Schema 版本和全部文件 SHA-256），打包为 `app.tar`。

```text
release/
├── teamevolver-<version>-py3-none-any.whl
├── wheels/
├── requirements.lock
├── build-requirements.lock
├── install.sh
├── container_start.sh
└── manifest.json
```

## 离线安装

```bash
mkdir -p /app/deploy && tar -xf app.tar -C /app/deploy
python3.11 -m venv /opt/teamEvolver-venv
PYTHON_BIN=/opt/teamEvolver-venv/bin/python sh /app/deploy/install.sh
```

`install.sh` 先按 manifest 校验每个文件，再以 `--no-index --require-hashes` 安装构建工具、依赖和应用 wheel，最后执行 `pip check`。安装过程不需要网络，也不需要源码目录。

`scripts/verify_offline_install.sh [app.tar] [linux/amd64|linux/arm64]` 在 `--network none` 的空容器中验证这一流程。

## 容器镜像

```bash
TARGET_ARCH=x86_64 sh cicd_scripts/build.sh
docker build --platform linux/amd64 -t teamevolver:<version> .
```

Dockerfile 只 `COPY release/`，不复制源码，也不设置 `PYTHONPATH`。镜像内置固定版本和校验和的 OpenViking CLI，并安装 `git`（仅供 Skill Git 导入导出使用）。入口为 `container_start.sh`，必须设置 `TEAMEVOLVER_ROLE=api` 或 `worker`。部署方式见 [生产部署指南](../guides/02-deployment.md)。

## 初始化数据库

```bash
export TEAMEVOLVER_PG_DSN="postgresql://te_app:...@db:5432/teamevolver"
teamEvolver schema apply --dry-run   # 列出待执行版本
teamEvolver schema apply
teamEvolver schema status
```

Schema（默认 `teamevolver`，可用 `TEAMEVOLVER_PG_SCHEMA` 修改）必须预先由管理员创建并归属应用角色。`schema apply` 幂等，执行记录写入 `schema_versions`。业务模块不在运行时建表。

## 启动与管理

```bash
teamEvolver start --role api       # HTTP API + 控制台，默认端口 52010
teamEvolver start --role worker    # Worker + 健康端口，默认 52011
teamEvolver start --role all       # 仅 TEAMEVOLVER_ENV=local
teamEvolver start --role api --daemon --log-file /var/log/te-api.log
teamEvolver status
teamEvolver stop
```

## 验证安装

```bash
curl http://localhost:52010/healthz      # 存活
curl -i http://localhost:52010/readyz    # 就绪；PG 不可用或排空时返回 503
curl -i http://localhost:52011/readyz    # Worker 就绪
teamEvolver config show                  # 查看脱敏后的启动配置
python -m pytest                         # 源码安装时
```

## 升级

1. 构建新 release，在预发环境执行离线安装和 `teamEvolver schema apply --dry-run`。
2. 执行 `teamEvolver schema apply`。Schema 只向前演进；应用回滚只能回到兼容当前 Schema 的 release。
3. 逐个替换 API 与 Worker 实例。实例收到 SIGTERM 后先撤销 readiness，再等待在途任务结束。
