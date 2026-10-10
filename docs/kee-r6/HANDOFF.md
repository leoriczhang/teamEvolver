# KEE R6 简化实现交付

本轮交付可运行的统一控制台、方法治理、内部 Compile、确定性 Wiki 导出／发布恢复、Memory Port、历史迁移工具与本地验收材料。UpClaw 未修改。使用前先阅读 [验收限制](ACCEPTANCE.md)；真实 OV／模型和新版完整浏览器流程仍需验收，本轮没有部署或 push。

## 阅读入口

- [离线中文工程说明](architecture.html)：职责架构、主要数据流、签收核验案例、核心实现、创新点、适用场景及长处与不足。
- [基线与差距](BASELINE.md)、[功能映射](FEATURE_MATRIX.md)、[架构决策](DECISIONS.md)、[任务清单](TASKS.md)。
- [中文接口](../zh/api/20-kee-r6.md)、[部署／操作／恢复](../zh/guides/20-kee-r6.md)，英文材料有对应目录。
- `deploy/kee-r6/config.example.yaml`：无凭据、默认关闭的增量配置；SIT／PRD 模板只更新仓库材料。
- `results/acceptance.json`：机器可读状态和证据索引；`knowledge-base/`：提交内容、SHA 与连接器回执。

## 关键入口与持久化

`team_ontology/kee/` 集中管理契约、来源、隔离 Compile、结构、Wiki 和审核。新构建 `/kee/v1/methods/build` 使用原 `ontology_jobs/outbox/audit`；不可变结构和来源用现有 PG objects 的 `kee/r6/` 前缀，继承租户 RLS。不新增图数据库、工作流总线、执行台账或 Agent 可靠队列。

内部子进程只得到当前冻结批次和模型配置，拒绝继承 OV/root 凭据，不执行工具。调用、期限、输入、输出和尝试次数有上限。Schema 确认和导出不调用模型。Memory 保持现有维护入口和两阶段流程，通过 Port 复用状态恢复。

Wiki 上传到私有不可变目录，回读、ACL 和索引核对后切入口；公开状态必须有回执。OV 不提供包级原子提交，回执明确 `package_atomic=false`。撤回覆盖版本目录、反链和入口，真实检索清理必须部署后验证。

原件与已发布 Wiki 是不同 OV 资产。仅修改原件 ACL 不会自动传播到 Wiki；本轮未增加权限事件总线或持续扫描器，回收派生资料须执行 KEE 来源撤销／版本撤回并核对回执。KEE 结构读取会重新检查原件权限；Agent 直接 OV 读取以 Wiki 当前 ACL 为准。

可选 query/check 默认关闭，不注册 MCP，也不参与执行授权。现有 Agent 仍直接通过 OV 读取 Wiki。

## 本地复测

创建只监听127.0.0.1的临时 PostgreSQL，使用独立数据库及 `NOSUPERUSER NOBYPASSRLS` 的 schema owner。通过 `KEE_R6_TEST_DSN` 指定；勿指向 SIT／PRD。安装项目与测试依赖后：

```bash
python -m pytest tests/test_kee_r6.py
python -m compileall teamEvolver team_skills team_memory team_miner team_replay session_ingestion team_ontology tests
npm --prefix web-ui test -- --run
npm --prefix web-ui run build
node docs/scripts/check-docs-refs.mjs
```

`scripts/kee_lab.py init` 只在该临时库初始化 `kee_console`；`mock` 监听52012、`console` 监听52010。运行前核对端口，PID记录在 `/tmp/te_kee_r6_*.pid`，只终止对应精确PID。两个 HTTP 服务都是本地验证工具，没有真实 OV 或模型。无该环境变量时 PG 专项测试跳过，不能计为通过。

迁移脚本 `scripts/kee_migrate.py` 默认干跑，账户限定、流式导出、摘要校验；`--apply` 只在明确确认目标后使用。`scripts/kee_wiki.py` 离线导出与校验允许外部算法团队直接上传 OV，但外部上传不会产生 KEE 管理回执。

## 升级前需要完成

先排空旧任务并备份，检查模型凭据、账户 ACL、普通身份的拒绝结果和只读用户权限。真实服务执行小样本、索引等待、响应丢失、撤回检索及恢复验收；核对 Memory 的实际 Skill 是否使用工具或超过适配预算，再启用内部执行器。完整旧功能回归存在已列出的4项失败，不应直接据本地桩结果宣布生产就绪。

个人知识库只能通过连接器提交。待本人审核的提案单列；“保存成功”不是“正式发布”。历史 R5/R6 全蓝图保留，并以本轮简化范围增补后续版本指引。

本次同步：3篇新文档已创建且回读 SHA 一致，索引未确认；3份旧文档修订待本人审核。完整内容和操作回执见 [知识库归档包](knowledge-base/README.md)。
