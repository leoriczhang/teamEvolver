# 当前入口：KEE R6 简化实施（2026-10-08）

本次更新为待本人审核的实施修订，不能据此认为 SIT／PRD 已部署。最新职责为统一 KEE 运营与方法治理，OV 通用内容／ACL／检索／索引后端；不采用 R5 的 TE＋独立 KEE＋OV 三服务分工。保留 Python 包名、CLI/API、数据库 schema 与历史资料。

| 当前材料 | 内容 |
|---|---|
| [09_KEE_R6简化实施_源码基线与功能矩阵_20261008.md](09_KEE_R6简化实施_源码基线与功能矩阵_20261008.md) | 企业知识库、团队 Memory、Skill Evolution／True Replay、方法治理四类职责；当前源码与差距 |
| [10_KEE_R6契约接口与迁移部署恢复_20261008.md](10_KEE_R6契约接口与迁移部署恢复_20261008.md) | 四契约、内部 Compile、Wiki 发布、账户权限、迁移干跑与恢复 |
| [11_KEE_R6本地验收与待办_20261008.md](11_KEE_R6本地验收与待办_20261008.md) | 本地验证、已有失败、真实服务未执行项与后续任务 |

本轮不改 UpClaw，不接工具／Skill 调用前检查或调用后核验，不新增可靠轨迹队列、执行台账或第二套 Session/Judge。普通 Agent 沿用现有 OV 接入读取 Wiki。可选结构 query/check 默认关闭、不列入 MCP，也不提供执行授权。

结构和审批用 KEE 既有 PG objects／任务／审计，Wiki 正文在 OV；Memory 和已发布 Skill 保留原维护入口与发布规则。不能将各种资产统称为同一“正式本体发布”。

源码与隔离验收不是生产验收。真实 OV 索引、模型、新版完整浏览器和外部 True Replay Runtime 尚待验收。以下为保留的历史导航正文，其中“最新”与部署结论以其原日期为准，职责以本节为准。

---

# 知识进化平台｜总览与阅读导航

## 2026-10-02 最新目标：R5 方法 Wiki 与既有 Skill 协作

最新方案见 [07_知识进化系统_方法Wiki与既有Skill协作蓝图_R5_20261002.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/07_知识进化系统_方法Wiki与既有Skill协作蓝图_R5_20261002.md)。

**TE 保留 upstream/main 原生功能，尤其团队记忆生成与维护、Skill Evolution、True Replay 和 Workspace。KEE 承接独立 Compile 与企业新增的成功经验、团队记忆到企业 Wiki、Ontology 工作台及结构化存储。OV 只管理普通 Wiki、Memory、Session 等通用上下文，不保留本体专用表和推理路由。**

R5 保留 R4 的三方边界，依据用户本轮上传的《Ontology与既有Skill闭环_五项目深化调研及TE_OV方案_V2_20261001(1).md》，补齐 SOP 澄清、ProcedurePack、CapabilityBinding、个案证据、调用前检查和执行后核验。可选的是 KEE query/check，不是业务授权；默认 Agent 通过 OV 使用双向链接 Wiki。动作检查复用现有 Agent／Skill 边界，不新增第四个公共服务。

方法、条件和可选执行规格来自同一批准版本。普通 JSON 附件对 OV 仍只是资源，不引入本体表或解析器。业务算法团队可自行生成和审核 Wiki，直接上传 OV，不依赖 KEE；需要受约束执行时按实际流程补充能力契约和检查。业务系统继续是实时事实与事务权威，不先统一全企业库表。

R5 包含四张图、六阶段迁移、部署与性能边界、A/B/C/D 对照和十五项关键验收。正文 SHA-256：`66118ac86e42ce2fabf38c95ffd6fec534cd46d3d136dff8476186557ed4f360`。本轮已创建并全文回读核对；不代表平台已经迁移、部署或通过业务验收。

### R4：本版继续采用的三服务边界

[06_知识进化系统_上游保留与双向链接Wiki蓝图_R4_20261001.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/06_知识进化系统_上游保留与双向链接Wiki蓝图_R4_20261001.md) 明确：TE 原生团队记忆流程不搬家；企业新增工作台迁 KEE；OV 移除本体扩展；算法团队可绕过 KEE 发布 Wiki；结构服务只保留可选 query/check。R5 在此基础上细化方法如何实际使用，不撤销这些决定。

**使用顺序：R5 用于最新蓝图，R4 用于职责与 Wiki 发布详细边界，历史专题用于各自版本的实现和验收。** S1 研究材料中较早的 TE/OV 本体分工不直接照搬；R3 冲突目标不得再作为当前迁移依据。

## 2026-09-30 历史目标：三服务分工（冲突部分已由 R4/R5 调整）

历史目标设计见 [05_知识进化系统_三服务蓝图与迁移部署_R3_20260930.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/05_知识进化系统_三服务蓝图与迁移部署_R3_20260930.md)。

R3 按用户当时要求调整两项分工：将 ov.compile 与团队记忆、成功经验到企业 Wiki 的加工流程抽为独立知识进化引擎（暂称 KEE，由我们负责）；TE 的 Ontology 工作台仅作试验，生产业务算法服务通过受限接口直接向 OV 提交本体数据，由 OV 管存储、版本和推理。TE 继续承担阅读、知识运营和审查入口。R3 包含生产者契约、本体模型与接口、RAG/个人记忆等分阶段迁移，以及有明确假设的容量和部署方案。

**R3 是历史目标方案，不是拆分或部署完成报告。** 其中团队 Memory 流程迁 KEE、本体表与推理留 OV、Ontology 工作台留 TE 的目标已改变。以下正文保留 2026-09-22 的实现与历史规划口径，不能据 R5 倒写历史已经实现，也不能据历史记录恢复已撤回的目标。

## 2026-09-22 归档基线

归档日期：2026-09-22。源码基线：TE `be32fa7e5d89288d90889efb31daa046dda607a8`；OV `ca92f7802b4af59a080f1feb23107b92102477c8`。

证据标记：**设计**为目标或约定；**源码已实现**为本次静态核验；**本地验证**须有历史验证记录或本轮明确结果；**SIT 观察**仅代表会话中的具体时点；**待验证／待修复**不视作完成。本次归档没有重新执行业务测试或部署。

[返回平台总览](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/00_平台总览与阅读导航.md) · [版本与验收台账](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/部署运维与验收/版本证据验收与待办_20260922.md)

## 平台解决什么问题

teamEvolver（TE）把真实工作中的 Session、Evidence 和知识资料组织成可运营的团队能力。OpenViking（OV）提供上下文存储、检索、原生 Compile 与正式 Ontology 资产服务。统一的是运营入口和可追溯关系，各类资产仍遵循各自的生效规则。

例如，处理签收争议时，Agent 可以先检索制度原文，读取带证据的本体事实，再使用已发布 Skill 执行诊断方法；团队 Memory 提供长期背景。成功经验 JSON 只是知识资源，上传成功不意味着它已成为本体事实或 Skill。

## 从哪里开始

| 阅读目的 | 文档 |
|---|---|
| 理解双核心目标、六条进化路径、Agent 轮次贡献、Owner 工作台和旧系统退役 | [03_知识进化系统_双核心蓝图_R1_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/03_知识进化系统_双核心蓝图_R1_20260922.md)（历史目标设计，非完成报告） |
| 理解服务边界、权威存储与主要数据流 | [01_整体架构与主要数据流_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/01_整体架构与主要数据流_20260922.md) |
| 了解为何放弃独立 Runtime、发布签名和共库要求 | [02_架构演进与决策记录_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/02_架构演进与决策记录_20260922.md) |
| 资料、Wiki、检索、Account Key 与 Agent 接入 | [企业知识库与OV基础能力_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/企业知识库/企业知识库与OV基础能力_20260922.md) |
| 跨用户聚合、私有快照和 DreamCycle 维护 | [团队Memory聚合与维护架构_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/团队记忆/团队Memory聚合与维护架构_20260922.md) |
| 从 Wiki 目录构建、审核并发布 Ontology | [Wiki本体构建审核发布与Agent接入_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/Ontology/Wiki本体构建审核发布与Agent接入_20260922.md) |
| Session 到 Skill Evolution、True Replay 和拉取 | [SkillEvolution与TrueReplay闭环_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/Skill自进化/SkillEvolution与TrueReplay闭环_20260922.md) |
| 成功经验存量导入、增量上传、重复与重试 | [成功经验导入同步与待改进事项_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/Skill自进化/成功经验导入同步与待改进事项_20260922.md) |
| 启用条件、日志、权限及常见故障 | [配置权限日志与故障排查_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/部署运维与验收/配置权限日志与故障排查_20260922.md) |
| 版本、验证边界和下一步待办 | [版本证据验收与待办_20260922.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/部署运维与验收/版本证据验收与待办_20260922.md) |
| 找回旧资料的原始位置 | [历史目录与文档映射.md](viking://user/telegramuser8685262224/resources/documents/顺丰工作/知识进化平台/历史资料导航/历史目录与文档映射.md) |

## 双核心目标蓝图的使用方式

03号蓝图依据本目录新增资料与用户2026-09-22要求编写：OV作为统一上下文中间件，TE作为Human in the Loop知识进化管理平台；覆盖六条业务管线、Agent自主MCP使用与轮次后轨迹贡献，以及旧知识中台、独立RAG接口和mem0的分阶段替换。

**03号用于当时的目标规划；01、02及各专题用于对应版本实现现状；验收台账用于验证结果。** Agent统一向OV提交轨迹及OV→TE增量桥接、跨资产知识提升、统一Owner审查和旧服务退役仍有待建设部分。不能据目标蓝图把当前TE Session入口、成功经验PG真源或Skill注册表静默描述成已经迁到OV。

## 四类资产不要混用

| 类型 | 回答的问题 | 生效边界 |
|---|---|---|
| 企业知识资源 | 原始制度和资料怎么说？ | OV 内容写入、可读与检索处理分别核验 |
| 团队 Memory | 团队长期需要记住什么？ | 聚合及维护 Compile 更新目标，保留运行和快照记录 |
| Ontology | 哪些带时间和限定的事实支持结论？ | TE 人工审批，OV 原子激活 generation 并给回执 |
| 团队 Skill | 如何可靠地完成任务？ | SkillMutationService 固化版本，按配置使用验证与审核门禁 |

True Replay 是效果验证，不是普通文本评审。Ontology Candidate 不等于正式本体；Compile 完成不等于所有知识资产统一“发布”。

## 当前可用能力与边界

**源码已实现：** 两服务职责分离、原生 Compile 本体适配、团队 Memory 两阶段编排、Skill 发布与拉取、成功经验持久化同步状态、日志及权限诊断。

**SIT 观察：** 2026-09-22 的会话中，Ontology capabilities 已返回 200；成功经验出现正文可读但索引等待超时。它们只证明对应请求的状态，不能证明本体全链路或全部同步已通过。

**待修复：** 成功经验去重按记录身份与稳定正文，不做语义去重；正文匹配但索引未确认时仍可能再次写入并触发处理。**待验证：** 原生 Compile 真实模型端到端、安全故障竞态、规模成本及双服务恢复，按验收台账补证。

## 归档与整理原则

本目录是新入口，旧企业知识库、记忆系统和根目录散落文档保留原 URI。无关的 UpClaw、个人项目、履历和研究不调整。旧文档只增加后续版本指引，不把过去方案改写为今天已经实现的事实。

知识库文档“保存且可读”、索引完成和本人审核后发布是不同状态。本轮连接器操作回执保存在本地归档包；待审核修订未生效前，旧页面仍可能显示历史方案。

## 来源与核验入口

- TE：CONTEXT.md、README.md；本目录各专题列出的源码证据。
- [历史企业知识库总索引](viking://user/telegramuser8685262224/resources/documents/顺丰工作/企业知识库_OpenViking_2026-07_2026-09/00_项目总览与资料索引.md)
- [历史记忆系统总索引](viking://user/telegramuser8685262224/resources/documents/顺丰工作/记忆系统_mem0_MemTree_2026-05_2026-08/00_索引与来源台账.md)

