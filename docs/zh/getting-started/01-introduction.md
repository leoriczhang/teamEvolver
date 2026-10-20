# 产品简介

teamEvolver 是 **Agent 团队能力进化控制面**。它不执行任务，而是架在现有 Agent Runtime 之上，把真实工作中产生的经验转化为可复用、可验证、可治理的团队 Skill 与团队 Memory。

## 核心定位

Agent Runtime 负责执行任务，teamEvolver 负责让团队越用越强：

- **经验回流**：Agent 推送或数据源拉取 Session，经强制分析后成为正式 Session 和 Evidence。
- **候选生成**：Skill Evolution 基于 Evidence 生成 Skill Candidate；Memory Evolution 生成 Memory Change。
- **真实验证**：True Replay 在隔离的真实 Agent 会话中执行 Baseline 与 Candidate，以 Checklist 为完成门禁，再比较客观效率。
- **门禁发布**：Candidate Review 结合静态检查与 True Replay 结论决定 Publish；Rollback 以新版本恢复历史内容。
- **持续进化**：团队 Memory 通过跨用户聚合与 DreamCycle 维护持续去重、修订。

## 运行形态

同一份 release 和镜像以两种角色运行：

| 角色 | 职责 |
| --- | --- |
| `api` | 认证、授权、参数校验、短事务受理和查询；长任务写入 `worker_tasks` 后返回 `202 Accepted` |
| `worker` | 领取 `worker_tasks`，执行 Session 分析、Skill Evolution、True Replay、Memory Compile、投递等长任务 |

两种角色都可以水平扩容，不需要会话粘滞。架构细节见 [架构总览](../concepts/01-architecture.md)。

## 数据归属

| 数据 | 权威存储 |
| --- | --- |
| 全局配置、租户、成员、Secret Ref、Adapter、Session、Skill 版本、Candidate、Dataset、Replay、Memory 运行、Worker 任务、审计 | PostgreSQL |
| Skill、Memory、Wiki 等外部知识制品，以及 Compile Task | OpenViking |
| 不可变 payload、报告和 Team Miner 水位等大对象 | PostgreSQL `storage_objects`（ObjectStore） |
| 临时文件、日志、PID 和可重建缓存 | 本机目录 |

本机目录不保存任何唯一副本，多副本之间只通过 PostgreSQL 协调。详见 [存储空间与目录布局](../concepts/09-storage-layout.md)。

## 适用场景

- **团队 Coding Agent**：多名成员共享团队 Skill，从完成的任务中提炼新技能。
- **企业内部 Agent**：私有化部署，从业务 Session 中沉淀领域 Skill 与 Memory。
- **多 Runtime 接入**：不同 Agent Runtime 通过同一套 v2 协议上报 Session、读取 Context、拉取 Skill。
- **评测与进化研究**：用 True Replay 做 Baseline 与 Candidate 的对照实验。

## 不做什么

- **不执行任务**：没有自己的 Agent Runtime，True Replay 通过租户绑定的 ReplayAdapterFactory 在接入方 Runtime 中执行。
- **不替代 Agent 配置**：模型、工具和系统 Prompt 仍由 Runtime 管理。
- **不绕过治理边界**：Candidate 只有在静态检查通过且 True Replay 给出 accept 时才自动发布；管理员可显式选择 `force`。

## 下一步

- [快速开始](./02-quickstart.md)：在本机启动一套 all-in-one 开发环境。
- [安装部署](./03-installation.md)：开发安装、release 构建与离线安装。
