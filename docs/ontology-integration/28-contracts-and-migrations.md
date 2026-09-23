> 后续调整（2026-09-21）：发布改用 [trusted + Root Key](38-trusted-root-publication.md)，保留人工审批。下文保留当时设计与验收记录；独立发布签名不再是启用条件。

> 历史 V4 记录。2026-09-20 起部署职责调整为 TE 内置构建、OV 资产后端；后续见 [V5 实施说明](32-te-ov-v5-implementation.md)。本文测试结果不自动算作 V5 验收。

# Ontology 契约、存储与迁移

日期：2026-09-19。权威定义为 OV 的 openviking/ontology/contracts.py 和同目录生成的 JSON Schema / OpenAPI。26 号文档是历史协议提案；本实现仍处于实验版本，示例以本册与导出 OpenAPI 为准。

## TE 接口

前缀 /te/enterprise/v1：sources、schemas、jobs、jobs/{id}、cancel、prepare、rebase、approvals、commits、commits/by-key、rollback-candidates、source-events、context/compose、capabilities。

创建任务携带 submission_key 和完整 Manifest。tenant/user 不接受正文覆盖，由控制台认证及当前租户导出。TE 连接配置按 tenant + subject 映射独立 OV 凭证；不能把 tevt_ 当 OV API Key，也不能用共享管理员凭证冒充所有审核人。

## OV 接口

前缀 /api/v1/enterprise：sources、schemas、manifests、compile-submissions、compile-submissions/by-key、tasks/{id}、cancel、rebase、assets/prepare、assets/approvals、assets/commits、assets/commits/by-key、assets/rollback-candidates。

读取包括 ontology/capabilities、resolve、query、evaluate、explain、feedback，以及 context/compose。observations 仅授权连接器可调用；principals/{subject}/revoke 和 source-events 需要批准权限。actions/execute 不存在。

Runtime 仅访问 runtime/input 与 runtime/candidate；purpose=runtime 的签名不能用于出版授权或普通认证。原生 /api/v1/compile 接口语义未改变。

## 数据模型与版本

Source：source_id + revision 不可覆盖，规范 JSON 摘要含冻结文本及 readers；Evidence 的 start/end 是 Unicode 字符偏移，quote 必须与冻结文本完全匹配。暂不声称支持 PDF 页坐标或任意源系统的版本证明。

Entity：authority_namespace + entity_type + external_id 表示权威身份，label 不是身份。Assertion 带 polarity、qualifiers、epistemic_kind、半开业务有效期和支持集。generation/提交时间表达已知时点；历史读取仍执行当前撤回检查。

Manifest 固定 schema_revision、sources、expected_generation、delta/replace、extractor、deadline_seconds、max_assertions。CandidateBundle 不允许未知字段；未知类型、关系端点、无依据的 SystemFact 和证明循环拒绝 Prepare。

PublicationGrant 绑定 tenant、reviewer、prepared_id、candidate/manifest digest、expected_generation、epoch、expires_at 与 nonce。Prepare 只预写 staging generation；Commit 才激活，并原子消费 grant、保存 receipt、记录 projection 事件。

## 数据库与兼容

新增 ov_semantic schema：tenant/principal、immutable objects、source status、submissions、prepared、grants、commits、generations、facts/entities、events/cursors、projections。DDL 为 additive/idempotent；默认关闭不运行迁移。SQL 与 JSON 契约已加入 OV wheel 包资源。

原生 OV 接入要求 metadata.task_backend=postgres，并要求 OV_ONTOLOGY_PG_DSN 与 metadata 使用同一 DSN，使原生 task 记录可被同一权威任务后端读取。TE 和 Runtime 不连接该数据库。

回退部署时先关闭 ontology 入口和 dispatcher，保留表、制品与回执；不 DROP schema。仍有任务时先停止受理并 drain，或明确取消，再回退程序。旧任务、历史来源和当前撤回记录必须一起保留。

## 预算与错误

默认有界：每租户 32 个活跃构建、Runtime 单模型工作线程、最多 12 次模型调用、输入文本总计 100KB；Manifest 可选择更小断言/期限预算。大输入需分批，不会默默截断文本。

Query 上限：50 seeds、2 hops、100 nodes、200 assertions、5000 examined edges；PG 请求超时 3 秒。Context 采用 UTF-8 字节长度作为保守 token 上界，整个 prompt_fragment 计入预算，完整移除事实或观察，不能切断否定与证明。

409：幂等键冲突/基线变化/序列缺口；412：证据或授权水位失效；422：Schema、证明或类型非法；429：队列/预算不足；503：权威或来源不可用。UNKNOWN/CONFLICT 是知识状态，不能当作已知否定。

补充边界：模型预算按真实 HTTP 请求计数，包含 response_format 降级请求；每请求 max_tokens=4096，累计请求 JSON 字节不超过 1MB，未提供 usage 时成本字段为 null。任务内重试可按完整输入、模型、Schema 与抽取代码摘要复用已校验候选缓存。

证据展开使用 DAG 缓存，单事实最多 512 个叶证据、单批累计 100,000 个叶引用，超限拒绝。Context 规则逐 seed 实体求值，返回 entity_id，避免跨运单拼证据。

统一错误契约 sf.ontology.error.v1 含 detail、request_id、retryable、details；格式错误不回显输入正文。此约定仅应用企业新增路由，不改变原生 OV 接口。
