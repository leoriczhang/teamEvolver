> 2026-09-21 启动修复：[TE 本体默认复用 teamevolver schema，可配置](40-te-ontology-pg-schema.md)。

> 当前发布与启用方式：[trusted + Root Key 调整](38-trusted-root-publication.md)。

> 最新职责与代码见 [V5 双服务实施说明](32-te-ov-v5-implementation.md)、[契约与迁移](33-v5-contracts-and-operations.md)、[Agent 接入](34-v5-agent-guide.md)、[验收册](35-v5-acceptance.md)。23—31 号资料保留历史结论。

# Ontology 集成交付导航

2026-09-19：三个工作区已有默认关闭的隔离集成实现，尚未满足完整 V3 的全部验收门槛。代码未提交；没有部署测试或生产环境。

- [离线 HTML 工程说明](../reports/02-ontology-integration-explained.html)：职责架构、交互数据流、签收争议案例、模块、创新点与取舍。
- [原 teamEvolver 项目说明](../reports/01-project-explained.html)：已有 Skill、Memory、Miner、True Replay 等工程背景。
- [27 · V4 实施主文档](27-ontology-integration-v4.md)
- [28 · 契约、存储与迁移](28-contracts-and-migrations.md)
- [29 · 验收册与 O01—O36 状态](29-acceptance-and-status.md)
- [30 · 源码核验与架构决策](30-source-audit-and-decisions.md)
- [31 · 部署运维与实测结果](31-operations-and-results.md)

结果文件位于 `results/`，包括回归日志、两轮规模数据、查询计划退化和修复、恢复演练、依赖版本、源码摘要及个人知识库操作回执。`partial` 不能算通过，`saved` 不能算已应用，`pending_review` 不能算已发布。

隔离环境已通过 `scripts/ontology_lab.py` 启动。只读 Agent 示例：

```bash
/tmp/te-ontology-venv/bin/python scripts/ontology_agent_example.py --lab
```

真实模型配置缺失、原生 OV Rust 绑定不匹配，以及独立 OR 证明、自动 Wiki 修复、完整 CQ 金标和图谱差异编辑等剩余工作，均保留在验收册，不以合成测试替代。

最终补充：[V5 源码证据与架构决策](36-v5-source-decisions.md)。
