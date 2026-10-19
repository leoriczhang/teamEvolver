# teamEvolver

<div align="center">

### Agent 团队能力进化控制面

[![Python](https://img.shields.io/badge/Python-3.11-3776AB.svg?logo=python&logoColor=white)](https://www.python.org/)
[![FastAPI](https://img.shields.io/badge/FastAPI-Control%20Plane-009688.svg?logo=fastapi&logoColor=white)](https://fastapi.tiangolo.com/)
[![React](https://img.shields.io/badge/Console-React%20%2B%20TypeScript-149ECA.svg?logo=react&logoColor=white)](https://react.dev/)
[![OpenViking](https://img.shields.io/badge/Context-OpenViking-0F766E.svg)](https://github.com/volcengine/OpenViking)
[![License](https://img.shields.io/badge/License-MIT-18181B.svg)](./LICENSE)
[![English](https://img.shields.io/badge/README-English-2563EB.svg)](./README.en.md)

**把真实 Agent Session 转化为可复用、可验证、可治理的团队 Skill 与团队 Memory。**

</div>

---

## 产品定位

teamEvolver 位于 Agent Runtime 之外，负责团队能力的持续进化与治理。它接收 Agent 上报或数据源拉取的 Session，经强制分析得到 Evidence，生成 Skill Candidate 或 Memory Change，再经过静态检查、True Replay、Candidate Review、版本发布和 pull 分发形成闭环。

- Agent 继续使用自己的模型、工具、工作区和执行循环。
- teamEvolver 负责 Evidence、进化、验证、版本、审计和发布。
- PostgreSQL 是可变业务状态的唯一权威源；OpenViking 保存个人与团队 Memory、个人 Skill、Wiki 等知识制品。
- 同一个镜像以 `api` 与 `worker` 两种角色运行，两者都可以水平扩容。


## 进化闭环

```mermaid
flowchart LR
    A["Session 上报 / 数据源拉取"] --> B["normalize → analyze → archive"]
    B --> C["Skill Evolution"]
    C --> D["Skill Candidate"]
    D --> E["True Replay<br/>Baseline vs Candidate"]
    E --> F["Candidate Review"]
    F --> G["新版本"]
    G --> H["GET /sync/skills"]
    I["个人 Memory"] --> J["跨用户聚合 + DreamCycle"]
    J --> K["团队 Memory"]
    J --> L["Memory Replay"]
```

Checklist 是完成门禁，不是加权分数。双方都完成 Checklist 后，True Replay 依次比较交互轮次、工具调用数和 Token 用量。

## 核心能力

| 模块 | 能力 |
| --- | --- |
| Session 接入 | v2 协议上报、租户数据源 Adapter 拉取与定时拉取、价值分类与四维评分 |
| Skill Evolution | 按 Skill 分组规划、团队证据门槛、生成 Candidate 与同源 Test Dataset |
| True Replay | 通过租户绑定的 Replay Adapter 在真实 Agent 中运行 Baseline 与 Candidate，Checklist 裁判与渐进披露 |
| 团队 Skill 库 | PostgreSQL 追加式版本链、发布、回滚、tombstone、幂等变更、ETag pull |
| 团队 Memory | 跨用户聚合、DreamCycle 维护、Memory Change 与 Memory Replay |
| 成功经验 | 经验与 Pattern 编译、向 OpenViking 投影 |
| 知识库挖掘 | 上传资料转 Markdown，OpenViking Compile 生成 Wiki |
| 实验工作台 | 编辑 Skill Bundle，用数据集对未保存的 Candidate 运行 True Replay |
| 多租户 | 租户、成员、平台 / 租户管理员、RLS 隔离、租户 Key |

## 控制台

### 运行总览

<a href="./docs/assets/teamEvolver-console-dashboard.png">
  <img src="./docs/assets/teamEvolver-console-dashboard.png" alt="teamEvolver 运行总览" width="100%">
</a>

### 进化链路

<a href="./docs/assets/teamEvolver-evolution-pipeline.png">
  <img src="./docs/assets/teamEvolver-evolution-pipeline.png" alt="teamEvolver 进化链路" width="100%">
</a>

控制台导航分为技能挖掘、工作台、智能体与能力、知识与方法、进化与实验、审查与发布、设置与接入七组，见 [Web 控制台](./docs/zh/guides/03-console.md)。

## 系统架构

```mermaid
flowchart TB
    Agent["Agent Runtime<br/>租户 Key + user_id"] --> LB["负载均衡"]
    Browser["浏览器控制台"] --> LB
    LB --> API["teamEvolver api ×N<br/>认证 · 校验 · 短事务 · 202"]
    API --> PG[("PostgreSQL<br/>RLS · worker_tasks")]
    Worker["teamEvolver worker ×N<br/>Session 分析 · Evolution · Replay · Memory"] --> PG
    Worker --> OV[("OpenViking<br/>Memory · Skill · Wiki · Compile")]
    API --> OV
    Worker --> Replay["Replay Adapter<br/>客户 Agent"]
```

API 只做认证、授权、校验、短事务受理和查询；长任务写入 `worker_tasks` 后返回 `202`，由 Worker 领取执行。详见 [架构总览](./docs/zh/concepts/01-architecture.md)。

## 快速开始

要求：Python 3.11、PostgreSQL 14+、OpenViking、OpenAI 兼容模型端点。

```bash
bash scripts/install_teamEvolver.sh
source .venv/bin/activate

export TEAMEVOLVER_ENV=local
export TEAMEVOLVER_PG_DSN="postgresql://te_app:...@127.0.0.1:5432/teamevolver"
export TEAMEVOLVER_ROOT_API_KEY="<random>"
export TEAMEVOLVER_OV_ENDPOINT="http://127.0.0.1:1933"
export TEAMEVOLVER_OV_API_KEY="<OpenViking Root Key>"
export TEAMEVOLVER_LLM_API_KEY="<model key>"

teamEvolver schema apply
teamEvolver start --role all
```

打开 `http://127.0.0.1:52010/`，首次访问进入管理员初始化页。完整步骤见 [快速开始](./docs/zh/getting-started/02-quickstart.md)。

常用命令：

```bash
teamEvolver status
teamEvolver config show
teamEvolver schema status
teamEvolver stop
```

生产环境分别运行 `api` 与 `worker` 角色，见 [生产部署指南](./docs/zh/guides/02-deployment.md)。仓库根目录的 `compose.yaml` 从同一镜像启动两个角色：

```bash
TARGET_ARCH=x86_64 sh cicd_scripts/build.sh
docker compose build
docker compose up -d
```

## Agent 接入

使用 [协议 v2](./docs/zh/agent-integrations/06-protocol-v2.md)：服务地址、租户 Key 与 `user_id` 即可接入，不需要 Agent 注册。

| 用途 | 接口 |
| --- | --- |
| 上报 Session | `POST /ingest_session` |
| 获取上下文 | `/internal/agents/context/*` |
| 拉取团队 Skill | `GET /sync/skills` |

OpenViking Root Key 与模型 Key 只在服务端使用。True Replay 通过租户绑定的 Replay Adapter 为每个分支创建独立会话。

## 安全与一致性

- 所有业务表启用 RLS，按 `tenant_id` 隔离；应用角色必须是 NOSUPERUSER、NOBYPASSRLS。
- 凭证以 Secret Ref 保存，接口只返回是否已配置。
- Worker 通过租约与 fence 防止重复执行；结果未知的外部写入进入对账，不盲目重试。
- 团队 Skill 变更在一个事务内写入版本、文件与幂等记录；Agent 固定通过 pull 获取版本。
- Context 引用由服务端签发，绑定 `(tenant_id, user_id)`，15 分钟后过期。
- 团队 Memory 与团队 Skill 对 Agent 只读；Agent 只能写个人 Memory。
- 无法确定性重放的外部副作用工具在 Replay 中直接失败，不回退为真实调用。

## 项目结构

```text
team-evolver/
├── teamEvolver/          # 平台层：组合根、启动配置、数据库、运行时、存储、OpenViking 客户端、身份、CLI
├── session_ingestion/    # Session 上报、数据源拉取、强制分析阶段
├── team_skills/          # Skill 库、Candidate、Evolution、成功经验、导入导出、实验工作台
├── team_replay/          # True Replay、Test Dataset、Replay Adapter
├── team_memory/          # 团队 Memory 聚合、DreamCycle 维护、Memory Replay
├── team_miner/           # 知识库挖掘
├── web-ui/               # React + TypeScript 控制台源码
├── tests/                # 单元、集成、架构与文档契约测试
└── docs/                 # 中英双语 Markdown 文档
```

## 文档

文档索引见 [docs/README.md](./docs/README.md)。

| 章节 | 内容 |
| --- | --- |
| 开始使用 | [产品简介](./docs/zh/getting-started/01-introduction.md)、[快速开始](./docs/zh/getting-started/02-quickstart.md)、[安装部署](./docs/zh/getting-started/03-installation.md) |
| 核心概念 | [架构总览](./docs/zh/concepts/01-architecture.md)、[进化闭环](./docs/zh/concepts/02-evolution-loop.md)、[Skill 体系](./docs/zh/concepts/03-skills.md)、[Memory 体系](./docs/zh/concepts/04-memory.md)、[True Replay](./docs/zh/concepts/06-true-replay.md)、[存储空间](./docs/zh/concepts/09-storage-layout.md) |
| 使用指南 | [配置参考](./docs/zh/guides/01-configuration.md)、[生产部署](./docs/zh/guides/02-deployment.md)、[Web 控制台](./docs/zh/guides/03-console.md)、[可观测性](./docs/zh/guides/04-observability.md)、[故障排查](./docs/zh/guides/06-troubleshooting.md) |
| Agent 接入 | [接入概览](./docs/zh/agent-integrations/01-overview.md)、[协议 v2](./docs/zh/agent-integrations/06-protocol-v2.md)、[自定义 Agent](./docs/zh/agent-integrations/05-custom-agent.md) |
| API 参考 | [API 概览](./docs/zh/api/01-overview.md)、[端点索引](./docs/zh/api/00-endpoints.md) |
| English | [English Documentation](./docs/en/getting-started/01-introduction.md) |

修改代码后按 [文档维护指南](./docs/zh/api/99-docs-maintenance.md) 同步文档，并运行 `node docs/scripts/check-docs-refs.mjs --strict`。

## 开发验证

```bash
bash scripts/install_teamEvolver.sh
npm --prefix web-ui ci
bash scripts/verify_local.sh
```

`verify_local.sh` 执行 Python 编译、测试套件、前端测试与生产构建。

## License

[MIT](./LICENSE)
