> 后续调整（2026-09-21）：发布改用 [trusted + Root Key](38-trusted-root-publication.md)，保留人工审批。下文保留当时设计与验收记录；独立发布签名不再是启用条件。

> 历史 V4 记录。2026-09-20 起部署职责调整为 TE 内置构建、OV 资产后端；后续见 [V5 实施说明](32-te-ov-v5-implementation.md)。本文测试结果不自动算作 V5 验收。

# Ontology 源码核验与架构决策

日期：2026-09-19。开工基线：TE fe7f824；OpenViking 029faeff0；ontology-enhancer b8de0d1。未提交的新实现需结合工作区文件摘要，不能只凭这些基线复现。

## 复用与新增

| 核验对象 | 事实 | 决策 |
| --- | --- | --- |
| OV CompileService | 原生请求每次创建 task；向外部 Runtime 传递调用者 API Key | 新增企业幂等受理与任务能力凭证，不把企业字段塞进原生请求 |
| OV PostgresTaskStore | 已有 ov_tasks/outbox/work，支持事务与修订 | 复用表及原子受理关系；为 ontology_compile 明确单一 dispatcher |
| TE Memory | 当前 team_memory 是唯一模块接口；维护使用 Compile | Ontology 新建独立包，不恢复旧 Memory 别名、不夹带 Memory 发布迁移 |
| Enhancer DomainPack | 概念/关系/约束/工作流 + provenance sidecar | 复用 PipelineRunner，另建实例 CandidateBundle；原 CLI 行为保留 |
| TE 身份 | tenant Key + 声明 user_id；控制台管理员身份独立 | 工作台要求已认证 console admin，并使用按主体配置的 OV 凭证 |

## 关键源码入口

TE：team_ontology/api.py、standalone.py；web-ui/src/views/OntologyView.tsx；scripts/ontology_lab.py、ontology_smoke.py、ontology_load.py、ontology_scale.py；tests/test_ontology_integration.py。

OV：openviking/ontology/contracts.py、validation.py、rules.py、store.py、schema.sql、service.py、api.py、worker.py；server/app.py 为默认关闭的接入点；storage/metadata/tasks.py 将专属队列排除在通用投递之外。

Enhancer：src/ontology_enhancer/ov_runtime/app.py、extractor.py、contracts；tests/test_ov_runtime.py。

## 实施不变量

- 本体正式发布唯一入口在 OV；TE、Runtime 没有数据库发布旁路。
- 初版审核人资格由 OV 当前 principal permissions 决定，每次请求和 commit 重新检查；尚未接企业 owner 组织层级。
- 私有候选与正式 facts 是不同存储集合；查询只使用已激活 generation。
- 当前权限检查优先于历史快照；恢复备份也必须先恢复当前撤回水位，本地数据库恢复已验证，外部 IAM 恢复联动仍待系统测试。
- Schema、ToolContract 与原始材料中的自由文本 URL 分离；Runtime 不执行材料指令。
- 原生 task completed 只表示候选计算结束；TE 已发布状态必须以 OV commit receipt 为准。

## 取舍

PostgreSQL 起步使事务与审计清楚，也避免新增图数据库运维；代价是 generation 快照写放大、租户级授权锁会串行化同租户读取。保守证据 AND 防止放宽权限，但会降低部分可用性。字符偏移便于精确重放，却不能自动替代原始 PDF 页码。当前只读 ToolContract 是发现/建议接口，没有业务工具执行网关。
