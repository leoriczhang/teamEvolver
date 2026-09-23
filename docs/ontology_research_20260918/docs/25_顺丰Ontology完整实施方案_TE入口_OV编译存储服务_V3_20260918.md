# 顺丰 Ontology 完整实施方案 V3
## teamEvolver 构建入口 · OpenViking 编译、结构化存储与 Agent 服务

> 2026-09-18｜设计提案，尚未部署、联调或压测。
> 基线：《企业知识平台_TE与OV分工_Ontology及WeKnora集成_工程指导V2_20260918.md》，对应个人知识库21、22号。V3细化其 Ontology 相关章节，不覆盖旧历史，不改变记忆系统／Wiki／Skill 已确定的单一责任边界。
> 配套：23号论文与开源选型、24号飞虎精读与业务蓝图、26号接口与验收册。公开来源编号见23号，内部业务约束见09、10、21号。

## 0. 决策摘要

采用 **TE 本体工程工作台 + OV Ontology Compile Profile + OV 结构化语义服务**。构建、审核入口全部在 TE；单任务执行经 OV Compile；Runtime 只产出私有候选；正式 Schema、实体、断言、证据、规则和 Issue Pack 由 OV 存储和发布；业务 Agent 直接调用 OV 的只读语义与上下文接口。真实业务动作仍由原业务系统／工具网关控制。

首版不新建通用“本体平台”微服务，不整体引入 HugAgentOS，不把 Neo4j 当先决条件，不让 WeKnora 形成第三套 Wiki 主库。OV 管理的 PostgreSQL 是首个结构化存储适配器；对象存储／NAS 保存不可变制品和人读导出；向量与图索引仅为可重建投影。

将“本体”定义成受治理的运行语义层，而不止一张实体关系图。首个纵切建议是签收争议／履约异常只读诊断：对象识别、缺失证据、状态解释、规则适用、后续查询与转交建议。没有获得业务确认的规则只使用合成数据，不自动改派、赔付或改变履约状态。

## 1. 相对 V2 的十项实质细化

| V2 已确定的方向 | V3 新增落实方式 |
| --- | --- |
| TE 构建、OV 保存 | TE OntologyWorkspace／Pipeline；OV SemanticRepository、Prepare、Commit、Context 各自明确输入与状态 |
| Wiki→Ontology | 接上 OV 公开 knowledge-graph Skill 的候选导入；持续运行改用结构化增量 Profile，避免重复 LLM 抽取 |
| Schema／Entity／Assertion | 增加 Issue Pack、EvidenceSlot、ToolContract、BindingSpec、规则语义及运行态观察边界 |
| 有证据的断言 | 原始片段版本、支持集合、引用 DAG、推导链及防循环证明；Wiki 摘要不成为唯一证明 |
| Compile 幂等扩展 | 企业受理记录与原生任务登记需要在同一 OV 持久化边界完成，不能仅在外层 HTTP 包一层就承诺不重复任务 |
| 原子发布 | 批量预写不可变版本，短事务激活 generation；正式头、grant 消费、receipt、outbox 同事务 |
| 来源有效性 | 当前权限与来源撤回的同步读门禁，增量依赖传播和迟到任务 fence |
| Ontology→Prompt | typed query、运行证据句柄、规则求值、完整事实包预算与确定性 renderer |
| 动作边界 | 区分知识发布授权、语义 preflight、实际业务执行授权；任何一种都不能替代另一种 |
| 工程验收 | CQ 金标、提炼／使用双评测、权限与时态反例、故障恢复、工作包和真实未运行状态 |

公开能力边界：OV v0.4.20 的原生 Compile API 文档与 main 的 knowledge-graph Skill 已查；不等于本地已部署，也不证明本文新增企业接口已存在。[O1–O3] TE 本轮仍只有公开 README 层核对。[T1]

## 2. 整体架构与责任

```text
管理员／业务负责人／知识运营人员
                 │
                 ▼
TE 本体工程工作台
  场景与CQ → Schema治理 → 来源范围 → 编译计划 → 候选diff → 审核／批准
                 │ 企业Compile提交／冻结Manifest
                 ▼
OV Compile受理层 ──→ 独立Ontology Runtime
  任务／幂等／配额          受限读源／抽取／消歧候选／规则候选
                 ◄── 私有Candidate Bundle ──┘
                 │ Prepare与确定性校验
                 ▼
TE PublicationCoordinator ──批准声明──→ OV AssetCommitService
                                        │
                           结构化版本＋正式generation＋outbox
                                        │
                                        ▼
业务Agent ──受信身份──→ OV Ontology／Context API／MCP
                             │             │
                       正式知识图       受权ObservationHandle
                                           │
                                 企业查询工具网关 → 源系统

实际业务写动作：业务Agent → 独立ActionGateway／原业务API
                         （重查当前身份、实时版本、确认和幂等）
```

TE 拥有业务计划、领域语义、审核策略和审阅记录；OV 拥有执行任务、权威内容、结构化语义、正式发布头和读取；Runtime 没有正式写权限；业务系统继续拥有其业务实例。TE 故障不应使已发布知识读取依赖 TE 在线，但 OV 仍必须能确认当前权限。

数据库可以共用 PG 实例，但分别使用 `te_control`、`ov_assets`、`ov_semantic` schema 和专用数据库角色。TE 不直接写 OV 表。OV 不重新建立一套 TE 审批系统，只存校验发布必需的批准引用、绑定摘要和消费记录。

## 3. 本体范围与模型分层

### 3.1 三类长期资产与一种短期数据

**Schema（类型定义）**：EntityType、PropertyType、RelationType、值类型、单位、限定维度、约束和兼容策略。通常称 TBox；不需要为了采用这个概念就先部署完整 OWL 推理服务器。

**Knowledge（受控知识）**：稳定实体引用、类型化断言、规范性规则、流程、工具契约和证据。通常包括 ABox 式实例知识，但不意味着要把全部业务流水复制到 OV。

**Issue Pack（场景模型）**：Issue、必要对象、EvidenceSlot、状态解释、候选原因、允许的下一步、ActionGate、转交规则和 CQ 测试。它引用共享 Schema／Rule，不复制一份可独立变化的规则正文。

**Observation Overlay（运行证据覆盖层）**：一次受权查询得到的短期观察。与正式知识分区、分 TTL、分可见域；默认只在当前主体／任务范围使用。不得因为被 Agent 调用就自动进入团队 Wiki 或长期本体。

### 3.2 保留既有知识组织与治理

四类 Page Role 与14业务域保持原状，它们是分类与呈现维度，不作为全部 EntityType 的继承树。业务域之间共用 Shipment、Location、Process、Evidence 等核心定义，再按场景扩展。一个对象只有稳定 ID 和一个正式 head，多视图只是引用。

双 Owner 延续10号文档的 `data_owner + accountable_owner`；非自然人 Owner 必须绑定可追责人或岗位。R0—R3 沿用原审批语义：符合条件的权威源确定性映射、低风险有证据抽取、跨源／影响动作结论、高风险／自动执行／权限扩大，逐级提高审阅要求。本方案不重新命名这套等级，也不把模型评分当审批。

### 3.3 Schema 编译方式

设计期可采用 LinkML 表达受支持的类型与字段，生成 JSON Schema、Python／TypeScript DTO 以及选定 SHACL／RDF 导出。[E2,E3] 正式权威是 OV 中经批准的 Schema Revision，包括模型源、编译器版本、生成物摘要和支持特性清单。

首版只承诺有限子集：基础类型、枚举、必填、范围、关系端点、显式限定和有限唯一性。复杂继承、反属性、闭包和跨对象约束必须逐条测试，不能因“能生成 OWL”就认定所有格式语义等价。跨对象业务约束由确定性验证器承担。未经批准的新类型只能进 SchemaCandidate。

## 4. 结构化数据与物理存储

### 4.1 正式实体与语义版本

| 表／逻辑对象 | 最低字段与不变量 |
| --- | --- |
| schema_revision | schema_id、version、digest、model、compiled_shapes、compiler_version、approval_ref；不可变 |
| entity | tenant、entity_id、entity_type、authority_namespace、external_id；身份与名称分离 |
| entity_revision／alias | 名称、别名、地区／语言、来源、有效期；不能用名称作跨租户唯一键 |
| assertion_revision | assertion_id、revision、subject、predicate、entity_object或typed_value、polarity、qualifiers、epistemic_kind、valid_range、recorded_range、state |
| support_set／support_member | 一条断言的一组必要证据，引用不可变来源片段或已证明前提 |
| source_revision／source_segment | source_id、revision、digest、解析器版本、稳定片段／页／行定位、源时间和授权引用；由OV资产层维护 |
| rule_revision | 输入类型、受限表达式AST、输出类型、未知／冲突处理、适用条件、证据、测试、风险、版本 |
| issue_pack_revision | Schema／Rule／ToolContract锁定引用、必要证据、转交定义、CQ集、语义版本 |
| tool_contract_revision／binding | 工具注册ID、版本、输入输出Schema、字段语义、读写类型、时效与源系统映射；不保存业务密钥 |
| semantic_generation／member | scope、schema_revision、base_generation、bundle_digest、members、prepared／active状态 |
| commit_receipt／outbox／revocation | 幂等提交键、批准消费、资产／分区序列、来源与授权水位 |

所有主外键和唯一约束都应包含 tenant 或用不可跨租户引用的组合键。PG RLS 可作纵深防护，但不能替代应用层的当前授权及来源检查。

### 4.2 ID、限定与时间

权威实体先以 `(tenant, authority_namespace, entity_type, external_id)` 定位，再映射到内部不变 entity_id。别名可变。旧 knowledge-graph 文件 ID 仅作 legacy_alias，不能以中文名称的哈希直接推断实体同一性。没有权威ID的实体创建暂定ID，经人工消歧后以有版本映射合并；保留历史指向和受影响关系，禁止静默改边。

断言不能只存三元组。地区、产品、客户范围、流程阶段、单位、币种、接口版本、例外、正负性等是事实含义的一部分；按 Schema 标出必需 qualifier。不要把必要限定全塞进不可查询的 reason 字符串，也不能在去重时忽略它们。

采用半开区间 `[from,to)`：`valid` 表示业务上何时成立，`recorded` 表示系统何时获知／记录。历史更正创建新 revision；历史查询同时指定业务时点和知识记录时点。开放区间的结束用 null，不以极大魔法日期混淆未知。客户端时间只作请求，服务端时间与源时间分开记录。

### 4.3 来源证明不是简单证据列表

一个支持集合中的全部成员构成 AND：缺少任何必要源就不够。多个完整独立支持集合之间可以是 OR，但必须经过明确的独立性与语义等价检查；首版采用保守交集，不自动把原生 Skill 合并的 evidence 数组认成多个独立证明。

每条 DerivedFact 引用规则版本与前提断言；证明图必须有原始 SourceSegment 叶子，不能由 Wiki→Ontology→新Wiki 的循环引用自我证明。原文提及、模型认为相关、URL 可打开，与“足以证明这条带限定的断言”分别记录。

ACL 可见性与事实是否为真也是两件事。调用者必须有权读取该断言、端点及当前选定证明的必要来源；只得到一个宽松的结论标签不能绕过来源限制。对外返回裁剪后的证明不能假装是完整证明。

### 4.4 索引、版本与大批发布

首版建立主体／谓词、客体／谓词、权威外部ID、generation成员和 source→support→assertion 反向依赖索引。常查限定建类型化列或受控侧表；JSONB 用于扩展，不假定一个 GIN 可解决所有邻接与时态查询。

大批数据分批写入不可变 revision 与待激活成员集；校验完成后只在短事务中 CAS 切换 scope head、消费 grant、写 receipt 和 outbox。不要在最终事务内插入百万行、调用模型或上传大文件。旧 generation 保留用于审计，默认读取仍叠加当前撤权与有效性守卫。

## 5. TE 的本体构建入口

新增逻辑模块 `OntologyWorkspace`，入口可位于 TE 知识运营台，复用既有身份、候选与审核机制，不另建账号系统。

工作台顺序是：选择业务域和 CQ → 选择已发布 Wiki 与授权来源 → 配置／提议 Schema → 预估影响与预算 → 创建编译 Job → 查看候选语义 diff → 查看证据与冲突 → 风险路由与批准 → 跟踪 OV commit receipt 和可读水位。

界面必须能区分“抽取完成”“等待审核”“已批准但提交冲突”“正式发布”“索引尚未追平”。模型生成了 JSON 或 Runtime 返回 completed，不等于本体已发布。

允许业务人员编辑：定义说明、别名候选、实体映射、适用限定、证据选择、Issue 路由和测试预期。任何会改变正式语义的保存都创建 ChangeSet；手工编辑、自动修复、回滚与模型更新走同一入口。WeKnora 只借用目录、diff、证据、lint 和任务恢复机制，不接入其第二套主表或直接正式写入接口。

## 6. 基于现有 OV Wiki 的编译管线

### 6.1 两条兼容路径，一个正式输出契约

**已有图制品迁移路径**：读取现有 `entities/*.md` 与 `relations.jsonl` → NativeKGImporter → 映射到候选 IR → 补齐可取得的原始片段版本／限定／时间 → OV Prepare。无法可靠补齐时标 `legacy_evidence_unverified` 或 `requires_enrichment`，不伪造证据和时点。[O2]

**持续建设路径**：TE 冻结变化来源和目标基线 → 调用 OV Compile，执行 `sf-ontology-build.v1` → 一次生成结构化增量 Candidate Bundle → OV Prepare／TE 审核／OV Commit。必要的可视化节点与 Markdown 从同一 IR 渲染，避免再调用一个模型重复抽取整个图。

NativeKGImporter 是确定性适配器，不让模型重写所有已有条目。完整图重建仅用于首导、小型实验或显式离线重构；每天新增文档不应重新输出所有实体和边。

### 6.2 冻结 Manifest

Manifest 至少绑定：租户与scope、业务Job与step、来源对象和revision／segment／digest、Wiki revision、当前批准Schema与Issue Pack、base_generation／asset heads、Skill digest、模型Endpoint及参数版本、解析器／抽取器版本、允许工具、授权引用、预期输出版本、预算和deadline。

其中 tenant、principal、授权信息取受信身份映射，不信任普通请求任意填写。Manifest 描述允许范围，但本身不是权限机制。运行凭证应由受控代理或凭证经纪器限制到指定输入和私有输出，禁止转交能读全公司的服务Key后仅靠Prompt限制。

### 6.3 原生 Compile 的使用方式

下面仅使用公开 v0.4.20 文档中的请求字段；URI 是示例，需在部署环境登记及鉴权后使用。[O1]

```json
{
  "from": ["viking://resources/enterprise-private/compile-inputs/job-demo-001"],
  "to": "viking://resources/enterprise-private/compile-staging/job-demo-001",
  "skill": "viking://resources/enterprise-skills/sf-ontology-build-v1/SKILL.md",
  "instruction": "按冻结manifest与批准Schema生成候选增量；不得改写正式资产；证据不足时报告缺口。"
}
```

发送到原生 `POST /api/v1/compile`。`from` 是目录数组；若引用原文不复制正文，受限读工具也必须能在同一授权范围读取准确版本。上述 `enterprise-private` 只是示意命名，**路径叫private并不产生隔离**，PR00必须验证整个原生 list/read/find/search 路径都无法越权看到 staging。

正式 TE 应通过新增企业受理层调用，而不是依赖原生用户重复提交去重。不要把 `output_schema`、`approval` 或 `profile` 等新增协议字段直接塞入原生接口，假装其已经支持。

### 6.4 Runtime 产物适配与故障兜底

先验证所选 Runtime 能写入正确 JSON／JSONL 制品且 OV 保留原始字节、路径与摘要。原生 KG Skill 提供制品格式的参考，并不证明每个用户部署的 Runtime 都可无损输出本方案 Bundle。

不具备时采用独立 Ontology Runtime：仍接受 OV 原生任务生命周期，由新增 ArtifactSink 以短期受限令牌把 Bundle 写到 OV staging。Sink 是新实现，不是隐藏已有API；制品摘要由接收端重新计算。禁止以模型回答中声称的 sha256 当可信存储证明。

Skill 只规定抽取步骤与内容契约；隔离、预算、幂等、取消、路径限制、文件大小与JSONL行数限制由宿主执行器和 OV 实施。来自原文的指令按不可信数据处理，不得提升为系统命令。

### 6.5 九阶段业务流程

`SOURCE_FROZEN → SCHEMA_READY → EXTRACTED → RESOLVED → VALIDATED → REVIEW_READY → APPROVED → COMMITTED → PROJECTION_READY`。

抽取包括类型、关系、限定、认识状态和逐条证据。消歧优先权威ID，再在同租户同类型与相容业务范围内召回别名；模型只能提议匹配。校验包括JSON形状、端点、约束、时间单位、证据、权限、重复、冲突、规则安全和CQ回归。

失败状态细分 `EVIDENCE_GAP`、`SCHEMA_PROPOSAL_REQUIRED`、`QUARANTINED`、`REBASE_REQUIRED`、`SUBMISSION_UNKNOWN`、`REVOKED_INPUT`。不足项可以缩小候选发布集合，但不得删掉必要限定以换取验证通过。涉及互相依赖的规则、Schema和Issue Pack必须作为兼容闭包共同提交。

## 7. 执行、幂等与批准发布

### 7.1 两级幂等要落实到持久化边界

TE 保存 `job_id/step_id/submission_key/request_digest/ov_task_id`。新增企业受理端对同租户同键同请求返回同任务，对同键不同请求返回409。OV内部受理记录和原生任务登记必须原子关联，派发通过outbox完成；不能简单“先写submission，再HTTP调用原生Compile”，否则调用成功但未记录task的崩溃窗口仍可能重复任务。

如果现有任务存储无法提供同事务扩展，PR00必须选择明确替代：新增可按submission标识查回的原生登记能力，或接受候选计算至少一次并以租约／fence降低重复，**不得承诺执行恰好一次**。无论计算是否重试，正式发布必须由OV commit_key保障一次生效。

TE管理业务重规划；OV管理任务派发和恢复；Runtime只做有界模型重试。三层共享父预算。模型切换、Skill／Schema升级改变缓存键；相同内容NO-OP发布不代表没有模型费用。

### 7.2 Prepare → Approve → Commit

Prepare 导入候选，验证制品完整性、允许类型、目录与租户、证据版本、Schema和基线，生成 `prepared_id`、candidate_digest、validation_report_digest。它不改变正式head。

TE依据同一摘要做风险审核，签发短期且限定目标的 PublicationGrant，绑定候选、证据、Schema、期望heads、批准主体、有效期及一次性nonce。独立审核者只可在其业务范围批准；普通Worker不能自签grant。

OV提交时重新检查当前源有效性、授权水位、Owner／维护epoch、摘要、批准资格与签名或受保护批准记录、预期head。事务内激活generation、消费grant、写receipt与outbox。超时后通过commit_key查回原receipt，不能重复激活。NAS、索引和外部IAM不在PG事务内，需分别做制品存在性证明、索引水位和授权有效性守卫。

### 7.3 取消、回滚和撤回

取消只是停止后续执行，不等于撤销已写产物；因此Runtime只能写私有staging。取消后到达的结果须受job fence拒绝进入prepare或commit。staging按保留策略清理，清理不能误删正式generation引用的制品。

回滚是新发布事件：旧内容、旧证据组合重新做当前权限与源有效性验证。源撤回提高OV当前revocation epoch并同步拒绝依赖资产默认使用；TE随后影响分析和重编译。恢复备份时先恢复最新撤回日志和epoch再开放读服务，防止复活被撤内容。

## 8. 增量建设：万级文档与每日更新

### 8.1 以证据依赖图决定重编译范围

建立 `SourceRevision/Segment → SupportSet → Assertion → DerivedFact/Rule → IssuePack → Context/Index` 的反向依赖。按源事件／游标水位驱动；无可信事件时使用可对账的增量拉取，不靠全目录遍历猜变化。

| 变化 | 处理 |
| --- | --- |
| 新增来源 | 匹配既有实体／主题，仅编译相关片段；新Schema进入候选 |
| 正文变化 | 比较片段语义／摘要与revision；重抽受影响部分，重新验证局部关系 |
| 别名、显示或索引机械变化 | 确定性投影或候选元数据修订，不重跑整批模型 |
| 源被撤回／权限收窄 | 先同步阻断默认读，再失效支持集合与缓存；随后修复 |
| 读取超时／源服务503 | 标UNKNOWN／暂不可用；不得当作源删除自动清掉知识 |
| 规则或Schema不兼容升级 | 建新generation、计算依赖闭包、影子回归，通过后激活 |
| 工具输出Schema改变 | Binding和依赖状态解释失效；暂停相应判断，不能用旧字段映射继续执行 |
| 模型或Skill变化 | 开发评测后选择性重编译；不默认全库同时刷新 |

某来源失效后，即使可以找到另一独立证明，也必须重新确认其完整性和权限；不能凭数组里还有一条URI就继续显示旧事实。

### 8.2 分片与并发

TE按业务域／Issue／变化来源制定分片；Runtime只在已授权分片内微批。以实体候选集和必要Schema索引替代每任务携带全企业词表；热点实体归并在提交前做局部决议。全局统一Token、RPM、并发和队列公平配额，防止业务分片乘Runtime并发造成放大。

缓存键绑定来源与片段摘要、Schema、Skill、模型、解析器、消歧策略版本及scope。缓存重用仍检查当前权限与源有效性。源信息不允许跨租户“相同文本命中”而扩大可见性。

### 8.3 Wiki 与本体的一致性口径

Ontology generation绑定输入Wiki revisions和原始证据，不把“当前Wiki head”无条件混入旧本体结果。Wiki更新而本体尚未完成时，API返回 `source_watermark`、`semantic_generation` 与 `lag`。

当前仍合法且未失效的旧规则可按明确as-of读取；对要求最新状态或高风险问题，落后意味着缺口或需要实时查询，而不是悄悄拼接“新Wiki＋旧规则”。权限变化无论是否落后都即时生效。

## 9. OV 向 Agent 提供的接口

本文所有 `/api/v1/enterprise/...` 和 `ov.ontology.*` 名称均为新增提案，不是现有原生接口。

| 能力 | OV服务与主要结果 |
| --- | --- |
| Discover | 受权Schema、可用业务域、Issue Pack、版本、支持能力；不能枚举隐藏对象 |
| Resolve | 输入类型和别名／权威ID，返回候选、匹配性质和歧义；不自行合并 |
| Query | 类型化查询AST，按scope／时点／谓词／限定返回有界事实与证据 |
| Compose | 对Issue／实体／预算装配结构化ContextPacket和确定性prompt_fragment |
| Evaluate | 用固定Rule版本和受信观察句柄执行受限规则，返回成立／不成立／未知及冲突 |
| Explain | 从已授权断言追到可展示的证据和推导，返回缺口与裁剪说明 |
| Feedback | 保存使用问题、错误候选和最小必要证据，经事件回流TE；不直接改正式事实 |

建议MCP最小工具集为 `ontology_resolve`、`ontology_query`、`ontology_context`、`ontology_evaluate`、`ontology_explain` 及单独授权的 `ontology_feedback`。发现能力可用受控资源描述或HTTP接口。业务Agent不开放commit、任意SQL／SPARQL、原始数据库连接或全库导出。

### 9.1 ContextPacket

包含：契约版本、Schema digest、semantic_generation、业务时点和记录时点、选中实体与断言、必要限定、认识状态、证据定位、规则引用、缺失槽、冲突、源时效、允许继续查询的工具契约、裁剪／降级信息、renderer版本及审计水位。

结构化部分与文本必须由同一个选中事实集合产生。每个“断言＋限定＋必要证明＋不确定状态”为完整预算单元：预算不够就完整省略并标truncated，不可保留结论、截掉地区例外或否定条件。

确定性渲染不需要再调用LLM总结。自然语言问题可选用Agent或Planner形成受限QueryPlan，但计划仍经服务端类型、scope与预算验证，不能生成任意数据库语句。

### 9.2 实时观察的可信入口

Agent自己提交的JSON只能作为UserClaim或未验证输入。若要用于SystemFact，必须由受信工具网关／连接器生成可验证的 `ObservationHandle`，绑定主体、tenant、任务scope、源系统、工具版本、查询参数摘要、源记录版本、获取时点、时效与权限引用。OV取回并验证句柄，不能信任客户端的 `verified:true`。

句柄过期、主体不符、源版本失效或授权无法确认时返回UNKNOWN／刷新需求，不沿用缓存肯定结论。可按需求保存受控原始快照或只保存安全引用；是否保留、保留多久由源Owner和企业政策决定，不由Skill决定。

### 9.3 在线性能与故障边界

从V2的实验起点继续：至多50个实体种子、2跳、100节点、200断言、约4,000上下文Token；同时限制实际扫描边数、查询时间、全请求deadline和候选集。具体值需按真实分布校准，不能只裁输出而后台全表扫描。

缓存至少绑定principal授权指纹、scope、generation、query、时点、用途和renderer／预算。最终返回前再次检查当前撤回水位或有效授权租约；无法确认时fail closed。TE不可用不妨碍合法读取，IAM不可确认不能靠旧缓存绕过。索引落后可使用受权PG事实或明确降级Wiki检索，但不能编造缺失的图事实。

## 10. 规则与动作：语义可执行，但不能越权

首版规则采用纯数据AST的有限运算：类型化比较、集合包含、显式逻辑组合、所需证据存在且新鲜、状态映射。禁止模型生成任意Python／SQL／shell执行。未知值按三值逻辑传播，存在相反证据单独标CONFLICT；缺失不能因默认false而产生“已满足”结论。[S1,S2]

领域规则决定某事实或建议的语义前提；权限由企业IAM和源系统决定。PublicationGrant仅允许发布这份知识，不允许执行业务动作。OV preflight最多返回语义上满足／不满足／待补条件，不返回可绕过业务网关的万能执行令牌。

真正写动作经过ActionGateway：重新核对当前用户和Agent身份、业务角色、实时业务对象版本、前提、用途、确认或审批、幂等键以及防重放。仅在下游有条件更新／版本校验时才能关闭检查与写入之间的竞态；下游不支持则首版禁止自动执行。运行后需要业务回执，不能把HTTP请求已发送当动作成功。

HugAgentOS的确定性门禁和分级审阅给了有用参考，但顺丰企业强制规则不能由用户关闭，也不能只约束一个Agent框架而允许另一个MCP入口绕过。[H1–H4] 高风险待审结论不能先流式作为正式建议显示后再撤回；显示层应区分草稿、检查中和可用结论。

## 11. 安全、审计与知识生命周期

权限检查覆盖节点、边、证据、别名、搜索候选、邻接扩展、计数、错误信息和解释路径。不能先把无权信息交模型再在最终回答过滤。默认不暴露“有一条隐藏赔付规则”等可枚举细节；具体错误对外表现由企业安全策略定义。

数据最小化覆盖姓名、地址、手机号、合同价格、员工信息。查询参数与日志不自动进入共享Wiki；必要审计用受控引用、摘要和适当脱敏。对原文中的提示注入、外链和工具说明按数据处理；工具端点来自批准Registry，不从正文URL直接执行。

Source Owner、Accountable Owner、Agent Manager变化会触发审核或交接。Owner失联停止自动发布新版本；明确到期的规则进入STALE并按既有政策撤回，不把“旧版曾审批”当永久有效。

## 12. 代码改造落点

以下是建议逻辑模块，实际文件合并方式以PR00源码核查为准；不要求制造全部新package。

```text
teamEvolver/
  ontology/          workspace, schema_governance, pipeline, resolution_plan
  governance/        changeset, review, publication_coordinator
  orchestration/     job/task_binding, budget_plan, reconciliation
  integrations/ov/   compile_adapter, asset_client, ontology_client, events
  web-ui/            schema/CQ, semantic_diff, evidence, issue_pack, review

OpenViking/
  enterprise_assets/ prepare, commit, revision, source_catalog, outbox
  ontology/          models, repository, pg, validators, query, rules, evidence
  ontology/importers/native_kg.py
  context/           ontology_provider, deterministic_renderer
  server/routers/    enterprise_compile, ontology, context, assets
  workers/           projection, dependency_invalidation, validation

independent-runtime/
  sf-ontology-build/ frozen-input reader, extraction operators, bundle writer
```

OV官方文档定位的原生 `server/routers/compile.py`、`server/routers/tasks.py` 和 `service/compile_service.py` 是PR00重点核查入口。[O1] 本轮没有读取这些部署文件的完整执行链，因此不写未经确认的函数级补丁。

## 13. 实施顺序与出口条件

| 工作包 | 主要交付 | 必须通过的出口 |
| --- | --- | --- |
| P0 版本与业务取证 | 本地HEAD／差异／依赖、部署镜像、原生接口能力、源字段与权限、许可清单 | 能解释任务受理、Runtime凭证、输出制品、所有正式写入口；试点CQ与Owner确定 |
| P1 契约与唯一发布 | Schema IR、候选Bundle、OV PG版本／head／receipt、源依赖与撤回 | 未批准不可读；并发CAS；响应丢失不重复生效；撤回阻断旧读和旧写 |
| P2 Wiki纵切构建 | 现有KG制品候选导入、一个受限Ontology Profile、TE diff／审核 | 原始证据可定位；未知类型与歧义不会自动发布；小样本端到端可复查 |
| P3 Agent只读接入 | Resolve／Query／Compose／Explain、观察句柄、受限规则 | 正反例CQ、权限和时效全通过；TE停机仍合法读；未知不冒充否定 |
| P4 增量与规模 | 来源游标、局部依赖重编译、分片预算、影子评测 | 万级文档配置下量化积压／成本／审核负载；撤回和故障注入通过 |
| P5 扩域／受控动作 | 第二领域模块与交叉概念治理；可选业务网关 | 无新权威库、无复制规则；只有业务负责人批准且下游条件写支持时才扩动作 |

这些是依赖顺序，不是已经完成的PR或承诺日程。应先完成一个垂直闭环，不以先画全企业本体图作为P1目标。粗略排期需在P0确认团队规模、既有接口和实际文档质量后形成。

## 14. 评测与验收

建立同语料、同模型、同权限快照、同上下文预算的三个对照：原始RAG、Wiki检索、Wiki＋Ontology。分别测构建质量和使用效果，不把“多了结构化数据”当改进证明。

构建质量至少包含实体合并精度、类型／关系正确率、限定与单位完整度、来源可验证性、重复与冲突处理、增量失效正确性；使用质量包含CQ回答正确、缺证据时弃答、工具选择、越权或无依据动作建议、可解释性与成本。

检验样本按来源家族与时间留出；历史工单、同义改写和派生Wiki不得跨入相互泄漏的集合。由业务专家确认金标，模型评分只是辅助。关键权限／错误肯定／危险动作测试集中零失败是发布门槛，不是证明所有未来输入零风险。

沿用V2待测性能目标而非宣称结果：10并发类型化Context P95≤500ms，联合召回P95≤2s，投影可用延迟P95≤60s；须记录硬件、冷热缓存、授权选择性、断言与证据规模、背景编译负载。分别用10万／100万／500万断言作敏感性实验。首导和日更同时记录模型Token、真实有效产物、重试、写放大与人工审核耗时。

配套代码只验证合成契约和有限纯函数不变量；它不能验证网络、数据库事务、真实IAM、模型抽取、生产工具或端到端性能。全部系统联调与压测项目仍为not_run，详见26号及测试结果文件。

## 15. 当前未确认项与采用边界

本轮已经完成研究与设计，未修改本地工程。三个DevSpace连接失败，因此部署版本、原生Compile内部事务、Runtime输出和真实源权限尚未核验。公开main、tagged文档及用户企业分支不可混为同一快照。

需要特别关闭的P0问题包括：原生任务与企业submission能否原子登记；运行凭证如何真正下放最小权限；staging是否可能经普通搜索泄漏；SourceRevision能否稳定读取；原生取消是否仍可能写制品；知识库既有Skill／Wiki版本头如何避免双写；撤权水位如何在故障时fail closed。

最后的采用原则不变：**TE 管业务知识怎么建和谁批准，OV 管受限编译执行、正式结构化本体与Agent读取，源系统管实时业务事实，业务网关管真实动作。** 四者有明确契约，但不能各自建立互相竞争的事实或授权主库。
