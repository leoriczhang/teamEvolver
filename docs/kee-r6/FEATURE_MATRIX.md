# 功能映射与回归矩阵

| 一级导航 | 保留功能／路由 | R6 行为 |
|---|---|---|
| 工作台 | 运行总览、实验工作台、运行状态 | 原 Session／队列／实验入口保留 |
| 智能体与能力 | 个人与团队资产、平台资产、成员与接入 | Workspace、Skill、Memory 原维护入口 |
| 知识与方法 | 知识源、经验库、知识库挖掘、原 ontology 页面键 | 新 KeeMethodsView；原 OntologyView 留作历史源码 |
| 进化与实验 | 进化链路、挖掘总览、挖掘任务、数据集 | Skill Evolution、True Replay、SkillMiner 原行为 |
| 审查与发布 | 候选评审、审计、链路观测 | 聚合入口链接原维护者，不统一写事务 |
| 设置与接入 | 租户、用户、模型、数据源、使用文档 | 现有身份、服务级配置与租户上下文 |

视角 `agent-manager`、`knowledge-manager`、`research` 只改变布局。权限仍以现有 authenticated user、有效租户成员及平台支持模式校验。读者只能读取授权 Wiki 并提供反馈；直接调用写 API 返回 403。

资产边界：Skill 走 SkillMutationService；团队 Memory 走 team_memory；方法包走 KEE 人工批准与 Wiki 发布；成功经验资源同步保持 PG 真源。上传经验不是发布 Skill 或方法包。

原 Session／Evidence／Judge 不搬迁，不创建 UpClaw 队列或逐单执行库。既有 True Replay 页面和任务保留；本轮没有新注册业务 Skill、外部 Agent Runtime 或真实业务动作。
