> 后续调整（2026-09-21）：发布改用 [trusted + Root Key](38-trusted-root-publication.md)，保留人工审批。下文保留当时设计与验收记录；独立发布签名不再是启用条件。

> 后续版本：[V6 原生 Compile Wiki 本体构建](45-native-compile-wiki-operations.md)。本文保留 V5 实施历史，新任务不再使用这里的 TE 内置抽取流程。

# TE 统一知识运营与 OV 本体接入：V5 源码实施说明

日期：2026-09-20。基线见 [仓库记录](results/v5-baseline.json)，验收见 [结果册](35-v5-acceptance.md)。本文取代 V4 的部署职责，不追溯改写历史实验结论。

## 1. 两套服务，各有一个权威

只部署 teamEvolver 与 OpenViking。Ontology 是领域知识能力，不是必须单独部署的服务。enhancer 的解析、术语、分类、关系、约束、工作流、校验修复和导出源码迁入 TE 的 `team_ontology/engine`；原仓库 CLI 保留兼容。许可证及逐文件来源摘要随包发布。

| 事项 | TE：运营权威 | OV：资产生效权威 |
|---|---|---|
| 来源 | 选择、上传、确定范围、组合 Manifest | 当前 ACL 读取、逐来源不可变快照与摘要 |
| 任务 | PostgreSQL jobs/outbox 同事务，单 worker，取消与恢复 | 私有制品及执行代次围栏，不调度本体构建 |
| 评审 | 证据、实体/概念图、逐项编辑批准、差异、历史导入 | 确定性验证候选完整性 |
| 发布 | 记录意见、签署短期 PublicationGrant、对账 | 验签、重查权限/来源、CAS、授权消费和回执同事务 |
| 查询 | 配置权限与处理反馈 | Agent 直接通过 HTTP/MCP 读取 |

TE 任务库和 OV 资产库可以是两个 PostgreSQL 数据库。不存在跨服务事务：TE 的受理只保证 TE 任务与执行事件原子写入，OV 的提交只保证 OV 生效状态与回执原子写入；中间靠幂等键、摘要、围栏和对账恢复。旧“本体必须与原生 Compile 共库”的要求不再适用于 V5。普通 Skill、Memory 发布路径不迁移。

## 2. 从资料到回答的数据流

```mermaid
flowchart LR
  D[文档 / 已导入 Wiki] --> T[TE 知识运营]
  T -->|冻结请求| S[OV 快照与当前 ACL]
  S -->|revision / digest| J[TE jobs + outbox]
  J --> E[TE 内置 enhancer 引擎]
  E --> C[私有 CandidateBundle]
  C --> R[TE 逐项审核]
  R -->|Ed25519 PublicationGrant| P[OV 原子提交]
  P --> G[正式 generation + receipt]
  A[外部 Agent] -->|HTTP / MCP| Q[OV 检索 / 查询 / 求值]
  G --> Q
  Q -->|事实 + 证据 + 缺口| A
```

冻结不同外部来源的实际版本，不声称是跨系统同一时刻的快照。来源内容固定不代表权限固定；证据读取、构建、提交和查询继续校验当前授权。私有候选只在隔离存储中，不写普通 Viking 检索索引。

一次签收争议的操作顺序：上传合成签收记录，保存 Shipment Schema，创建构建；TE 抽取“运单 A 的 status=signed”及原文位置；运营人员检查时间、限定和原文后批准；OV 返回 generation 与 receipt；Agent 对客户“未收到”的陈述作单独标记，查询状态记录并求值。状态记录只能支持“系统记载已签收”，不能直接推出“本人实际收货”或“应当赔付”。证据不足返回 UNKNOWN；相反证据同时保留为冲突。写业务系统的动作未开放。

## 3. 核心实现与迁移清单

| 实现位置 | 功能与边界 |
|---|---|
| `team_ontology/config.py`、`api.py` | 统一 TE 登录、租户有效配置、OV sharing 连接；默认关闭 |
| `control.py` | TE PostgreSQL 任务、outbox、审计；幂等键、32 个未完成构建上限、全局单并发租约 |
| `service.py` | 冻结编排、内置抽取、私有制品上传、Prepare/Approve/Commit、取消、重基准、回滚与超时对账 |
| `engine/corpus` | PDF/DOCX/XLSX/Markdown/HTML/TXT 文本解析、分块、术语统计 |
| `engine/pipeline` | 五阶段领域包提案，确定性校验及有界修复；领域概念关系不会自动成为业务事实 |
| `extractor.py` | 独立事实抽取、唯一引文定位、冻结来源摘要、候选契约校验和输入摘要缓存 |
| `review.py` | 逐项批准/驳回/编辑、证据图、历史导入、版本比较、已评审领域包导出 |
| `web-ui/src/views/OntologyView.tsx` | 六个运营入口；回执确认后才显示已发布；回滚生成新候选 |
| OV `ontology/assets.py` | 来源快照、执行围栏、隔离候选、TE 公钥验签、版本与回滚素材 |
| OV `ontology/service.py`、`validation.py` | 不可变资产、带时间及限定的断言、证据校验、事务发布与有界查询 |
| OV `ontology/agents.py` | 原生检索窄适配、别名扩展、HTTP/MCP 共用服务、证据读取 |

解析后的单批来源上限 100 KB，单文件 5 MB；构建最多 12 次实际模型 HTTP 调用、累计模型请求 1 MB、每次输出 4096 token、Manifest 期限上限 600 秒。任务租约 660 秒，最多 3 个执行代次，模型调用复用 TE 专用线程池及全局并发预算（上限 8）。缓存受输入、模型、端点、实现和契约摘要约束；密钥不写入工作目录配置。

旧 enhancer `.ontology-enhancer` 项目用 `scripts/ontology_import.py` 导出 AnnotatedPack、决策与历史版本，TE 将其标记 `historical_import_only/requires_enrichment`。缺失原始证据的旧图只作隔离参考，不能冒充新业务事实。现有已发布 OV generation 不重新发布。迁入清单和许可证见 `team_ontology/engine/SOURCE.json`、`LICENSE`。

## 4. 为什么这样设计

主要改进是把“知识如何被运营”与“什么知识当前有效”分开：统一入口减少账号、模型和部署配置；签名批准绑定确切候选，编辑后旧批准失效；Agent 不依赖 TE 在线；HTTP/MCP 共用事实服务，减少两个协议各自解释事实的偏差。结构化上下文与模型文本使用相同事实，裁剪时连同限定与证明完整移除。

适合规则清晰、证据需要追溯、能接受人工审核的履约异常、签收争议、采购合规等场景。长处是审计与撤回边界明确、模型只提议、版本可对账、已有检索可复用。代价是维护 Schema 和人工审核的成本、整版本物化的写放大、保守证明策略以及全局单构建吞吐限制。本次没有证明行业诊断准确率、万人并发或大规模成本达标；不能将合成用例成功扩大成真实业务验收。

## 5. 上游合并策略

OV 本轮修改集中在 `openviking/ontology`；宿主接入仍是既有扩展注册，未新增运营页面，未向原生 Compile/Session/Memory 注入业务调度。保留历史 `ontology_compile` 隔离规则，避免普通队列误消费旧任务。新增数据库结构在 `schema_v5.sql`，不改写 V1 迁移文本。每次合并需运行契约摘要、原生认证、MCP、检索与扩展集成测试；Git 无冲突不是兼容性证明。

已实现的代码与本地通过证据、阻塞和未执行项分别列在 [V5 验收册](35-v5-acceptance.md)。
