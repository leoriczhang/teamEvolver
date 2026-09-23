# Ontology trusted + Root Key 发布调整

> 后续版本：[V6 原生 Compile Wiki 构建](45-native-compile-wiki-operations.md) 替代 TE 内置抽取路径；本文保留历史设计及仍有效的发布契约。

2026-09-21。替代 V5 中独立 Ed25519 发布签名方案，保留 TE 人工审核。只部署 TE、OV；本次不部署 SIT/PRD，不自动提交或 push。

## 职责与信任边界

TE 是任务和人工审批权威，OV 是实际版本生效权威。OV 信任经过原生 trusted + Root Key 认证的 TE 后端提交的审批记录；它不能独立证明 TE 页面上发生过一次人工点击。持有 Root Key 的后端必须遵守同样的审批流程。Root Key 只保留在后端配置，不发给普通 Agent 或浏览器。

来源、候选、审批、正式版本仍按租户隔离。TE 用可信租户上下文生成 OV account，用当前管理员映射生成 user，不采纳浏览器请求体中的身份。TE 租户有效配置 sharing.viking_api_key 使用现有 OV Root Key，不再优先取个人凭证。

## 提交契约与原子性

`POST /api/v1/enterprise/assets/commits` 请求为 `sf.ontology.commit.v2`，包含 prepared_id、commit_key、approval。approval 包含 tenant、reviewer、note、prepared_id、candidate_digest、manifest_digest、expected_generation、epoch、expires_at、approval_id。TE 审批生成五分钟有效记录，编辑候选会清除审批。

OV 原生认证解析 RequestContext，扩展验证实际认证模式为 trusted、Root Key 非空且匹配、不是 OAuth 代理身份；服务内部 Principal 的发布证明不从 JSON 解析。读接口保持原有身份解析。

提交事务继续检查发布者当前权限、批准者 approve 权限、当前来源与证据、Prepare 完整性、审批有效期和权限水位。CAS 切换版本，并原子写入唯一审批消费、回执及投影。复用历史 grants 表保存审批消费信息，无破坏性数据库迁移。原幂等键相同请求返回原回执；同键异请求拒绝，已消费审批不能换键重用。该信任模型不提供独立数字签名的离线验证能力。

## 开启与升级

TE 的 .env 只需启用 `TE_ONTOLOGY_ENABLED=1`，并配齐现有 PG、OV endpoint/account/Root Key 和租户权限。OV 启用 `OV_ONTOLOGY_ENABLED=1`、默认复用 OV 已有 PG（优先 pgvector，其次已启用 metadata 的 DSN），仅需要独立连接时设置 OV_ONTOLOGY_PG_DSN；保留观察句柄使用的 `OV_ONTOLOGY_SIGNING_SECRET`。无需发布公私钥，不新增鉴权方式开关。旧 YAML 签名字段被忽略，默认环境开关继续关闭。

升级时先停旧 TE worker。旧 publishing/commit_unknown 先按键对账：有回执则恢复 published；OV 不可用时继续等待；确认无回执后退回 review_ready，原审批作废，重新 Prepare 与审核。旧 approved 同样退回。不得自动把旧签名变成新的审批记录；保留历史审计、资产和回执。旧 grant 请求体不再被正式发布接口接受。

普通查询沿用原生认证；api_key 模式查询兼容，但不支持新的 Root Key 发布接口。普通 Agent MCP 不暴露发布能力。演示 harness 的 Agent token 仅用于本机隔离验证，不等同于实际外部 Agent 接入验收。

## 验证与结果

验证清单：无签名文件完整闭环；Root Key 缺失/错误、身份缺失、跨租户、普通用户及非 trusted 发布；未审批、摘要替换、过期、撤权、审批复用、CAS 竞争；响应丢失对账；旧审批迁移与 OV 不可用时保持不确定；HTTP/MCP 读取及 TE 停机。

本地新增与相关测试 73 项通过；TE 全量 574 通过、1 项既有失败、40 跳过；OV 相关回归 106 通过、6 项失败，失败项全部与此前基线证据一致。双服务 HTTP 完成两领域、发布回执、回滚与撤回，TE 停机时 OV 仍返回 1 条相同版本事实。编译、前端构建与文档检查通过。

本轮结果汇总见 `results/root-key-verification.json`，保留失败、阻塞及未执行项。完整原生服务部署、SIT frank 验收、规模与跨服务恢复不由本机测试替代。没有更改抽取逻辑，本轮不重复成本较高的真实模型抽取。

## 源码与知识库

TE Operations 生成结构化审批，ControlStore 恢复旧任务；OV authenticated_principal 校验原生后端凭证，AssetService 接收 v2 审批，公共事务服务保持历史回归兼容。权威 JSON Schema/OpenAPI 从 OV 导出并固定复制到 TE。

个人知识库保留 23—37 号历史资料，新增本调整，并按当前 SHA 提交总索引、架构演进、待验证事项及历史方案的后续版本指引。连接器保存或待审核不代表已发布，操作回执单独记录。

本轮知识库操作回执：2 项新建操作 saved、7 项修订 pending_review，均未报告为已发布。回执见 `results/root-key-personal-kb-receipts.json`。
