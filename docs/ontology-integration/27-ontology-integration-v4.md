> 后续调整（2026-09-21）：发布改用 [trusted + Root Key](38-trusted-root-publication.md)，保留人工审批。下文保留当时设计与验收记录；独立发布签名不再是启用条件。

> 历史 V4 记录。2026-09-20 起部署职责调整为 TE 内置构建、OV 资产后端；后续见 [V5 实施说明](32-te-ov-v5-implementation.md)。本文测试结果不自动算作 V5 验收。

# Ontology 三项目集成 V4：源码实施版

日期：2026-09-19。继承 23—26 号研究与 V3；本文记录本地实际实现，不替代业务立项或生产签收。

## 目标与当前结果

teamEvolver、OpenViking、ontology-enhancer 已有可运行的隔离纵切：冻结来源 → Compile 受理 → 独立 Runtime → 私有候选 → Prepare → 人工批准 → Commit → 只读诊断。签收争议和 Invoice 两组合成领域通过同一路径，未执行任何真实业务写操作。

功能默认关闭。代码仍在三个工作区的未提交变更中，不能把 HEAD 版本号误当成包含本次实现的发行版。

## 职责与边界

| 组件 | 负责 | 权威资产 |
| --- | --- | --- |
| TE / team_ontology | 来源与 Schema 管理入口、构建记录、审核、发布请求、回执 | 本地任务和审计；不直写 OV 数据库 |
| OV / openviking.ontology | 来源冻结、授权、任务、候选校验、正式版本、查询、规则与撤回 | 唯一正式本体头和提交回执 |
| Enhancer / ov_runtime | 文档术语与 DomainPack 流水线、实例抽取、候选制品 | 私有工作目录；没有正式发布权 |
| Agent | 直接向 OV 查询、补证据、解释与反馈 | 不获得发布或业务写权限 |

现有团队 Skill 仍通过 SkillMutationService；团队 Memory 仍由 team_memory 负责。Ontology 是新增的领域语义资产，不重命名或迁移这两条已有链路。

## 两条主要数据流

构建：TE 冻结 Manifest → OV 同事务登记 submission、原生 ov_tasks、outbox 与 work → 有租约的 dispatcher → Runtime 使用短期 capability 读取冻结输入 → 上传不可变候选 → TE 查看证据并发起 Prepare → OV 预写新 generation → 审核签发有时效的 PublicationGrant → Commit CAS 激活并记录唯一回执。

读取：Agent 身份 → OV 当前主体权限 → 指定 generation/业务时点/记录时点 → 有界类型查询 → 对每个证明重新校验来源状态和读取权限 → 纯规则 AST → ContextPacket 和同源 prompt_fragment。客户陈述单独保存，不能升格为 SystemFact。工具观察句柄绑定 tenant、audience、task_ref、subject、源版本、epoch 与过期时间。

撤回先提升 epoch，再计算当前事实的来源影响；旧 grant、Runtime capability 与历史版本读取不得越过新水位。来源临时不可用产生 degraded/UNKNOWN，不删除正式历史。回滚重新创建候选与批准，不能直接向后移动 head。

## 源码核验后的关键选择

1. OV 已有 PostgreSQL metadata/task 基础设施，本次复用其原生任务表和事务 outbox；ontology_compile 队列由专属 dispatcher 负责，通用 drainer 明确排除该队列。
2. enhancer DomainPack 中的关系是概念关系，不是业务实体事实。原流水线保留；新增实例抽取和证据绑定，不能直接把 DomainPack 发布成企业本体。
3. enhancer 的 Python 3.11+ 要求隔离在 Runtime；TE 仍保持原有 Python 下限。
4. 候选放在 OV 私有 PostgreSQL 对象表，不进入 VikingFS/向量索引。目录名 private 不作为授权证明。
5. Schema、IssuePack、规则和只读 ToolContract 作为一个不可变批准版本发布。规则只允许 eq/all/any/not，不执行 Python、SQL 或来源里的 URL。
6. 初版多个引用按保守 AND 处理；不把多个支持集默认理解为经过独立验证的 OR。

## 当前局限及后续门槛

已落地的是隔离集成实现，仍不能称完整生产 V3。真实模型抽取、生产 IAM/连接器、本体与现有 Wiki 的自动修复编排、独立证明 OR、细粒度 scope 策略、稳定分页、批量制品流式处理及恢复后的授权水位对接仍需进一步验收或实现。现有工作台是面向工程管理员的 JSON 表单与候选详情，尚不是完整的图谱差异编辑器。

存储采用 generation 快照；Prepare 已移出正式提交事务，但增量版本复制仍存在写放大，历史清理需独立策略。5M 行测试是投影敏感性微基准，不能代替 5M 断言的全量构建/发布测试。详见验收册和实际结果文件。
