# 快速开始

本指南在本机启动一套 all-in-one 开发环境：同一个进程同时运行 API 与 Worker，连接本机 PostgreSQL 和 OpenViking。生产部署使用分离的 `api`、`worker` 角色，见 [生产部署指南](../guides/02-deployment.md)。

## 前置条件

- Python 3.11（`pyproject.toml` 声明 `>=3.10`，release 镜像使用 3.11）
- PostgreSQL 14+，以及一个非 SUPERUSER、非 BYPASSRLS 的应用角色
- 一个可访问的 OpenViking 实例和 Root Key
- 一个 OpenAI 兼容的 LLM 端点和 Key
- Node.js 20+（仅修改 `web-ui/` 时需要）

## 1. 安装

```bash
bash scripts/install_teamEvolver.sh
source .venv/bin/activate
```

脚本按 `requirements.lock` 带 hash 安装依赖，再以 `--no-deps -e .` 安装本仓库，最后执行 `pip check`。

## 2. 准备数据库

Schema 必须先由数据库管理员创建并交给应用角色，`teamEvolver schema apply` 只在已有 Schema 内建表：

```sql
CREATE ROLE te_app LOGIN PASSWORD '...' NOSUPERUSER NOBYPASSRLS;
CREATE DATABASE teamevolver OWNER te_app;
\c teamevolver
CREATE SCHEMA teamevolver AUTHORIZATION te_app;
```

```bash
export TEAMEVOLVER_PG_DSN="postgresql://te_app:...@127.0.0.1:5432/teamevolver"
teamEvolver schema apply
teamEvolver schema status
```

`schema apply` 幂等执行 `teamEvolver/database/schema/` 下的版本脚本，并为每个租户写入 5 个模块的默认配置行。首次安装使用空数据库。

## 3. 最小启动配置

启动配置只读，来自环境变量和可选的 YAML 文件（`TEAMEVOLVER_CONFIG_FILE`）。在线配置保存在 PostgreSQL，由控制台修改。

```bash
export TEAMEVOLVER_ENV=local
export TEAMEVOLVER_ROOT_API_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export TEAMEVOLVER_SECRET_MASTER_KEY="$(python3 -c 'import secrets;print(secrets.token_urlsafe(32))')"
export TEAMEVOLVER_OV_ENDPOINT="http://127.0.0.1:1933"
export TEAMEVOLVER_OV_API_KEY="<OpenViking Root Key>"
export TEAMEVOLVER_LLM_BASE_URL="https://ark.cn-beijing.volces.com/api/v3"
export TEAMEVOLVER_LLM_API_KEY="<LLM Key>"
export TEAMEVOLVER_LLM_MODEL="<model id>"
```

完整的启动字段见 [配置参考](../guides/01-configuration.md)。

## 4. 启动

```bash
teamEvolver start --role all
```

`all` 只允许在 `TEAMEVOLVER_ENV=local` 时使用。验证：

```bash
curl http://localhost:52010/healthz
# {"ok":true,"role":"all"}
curl -i http://localhost:52010/readyz
# HTTP/1.1 200 OK  {"ready":true,"role":"all"}
```

## 5. 初始化并打开控制台

浏览器访问 `http://localhost:52010/`。首次访问显示管理员初始化页，PostgreSQL 模式下密码至少 12 个字符。初始化完成后，左侧导航包括：

- 工作台：运行总览、实验工作台、运行状态
- 智能体与能力：个人与团队资产、平台资产、成员与接入
- 知识与方法：经验库
- 进化与实验：数据集、进化链路
- 审查与发布：审查与发布入口、权限审计、链路观测
- 设置与接入：平台用户、租户管理、模型配置、数据源接入

![控制台总览](/assets/teamEvolver-console-dashboard.png)

## 6. 接入 Agent 并触发一次进化

1. 在「租户管理」创建租户，复制只展示一次的租户 Key（`tevt_...`）。
2. 按 [Agent 接入协议 v2](../agent-integrations/06-protocol-v2.md) 上报一条 Session：

```bash
curl -X POST http://localhost:52010/ingest_session \
  -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN" \
  -H 'Content-Type: application/json' \
  -d '{"schema_version":"teamevolver.agent-session.v2","protocol_version":"2.0",
       "session_id":"demo-001","runtime":{"type":"my-agent"},
       "runtime_context":{"user_id":"alice"},
       "turns":[{"turn_num":1,"prompt_text":"分析成本","response_text":"成本报告已完成。"}]}'
# 202 {"accepted":true,"job_id":"...:session.normalize","status_url":"/api/jobs/..."}
```

3. Worker 完成 normalize、split、analyze、trace_analyze、archive 五个阶段后，Session 出现在控制台；多话题 Session 会由 split 阶段生成可追溯的子 Session，再分别完成后续分析。
4. 在「审查与发布入口」触发 Skill Evolution（`POST /api/skill-evolution/trigger`），生成的 Skill Candidate 进入候选评审。

## 7. 停止

前台运行时按 `Ctrl+C`。以 `--daemon` 启动时使用 `teamEvolver stop`。

## 下一步

- [核心概念](../concepts/01-architecture.md)：API/Worker、权威状态与进化闭环
- [配置参考](../guides/01-configuration.md)：启动配置与在线配置
- [Agent 接入](../agent-integrations/01-overview.md)：接入你自己的 Agent
