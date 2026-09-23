> 后续调整（2026-09-21）：发布改用 [trusted + Root Key](38-trusted-root-publication.md)，保留人工审批。下文保留当时设计与验收记录；独立发布签名不再是启用条件。

# V5 源码证据、架构决策与交付边界

## 固定基线

| 仓库 | 修改前 HEAD | 本次改动 |
|---|---|---|
| teamEvolver | 6c7eee414c526c773962792276154a2170ea265d | 内置引擎、PG 任务、运营 API/React、配置、文档与测试 |
| OpenViking | f1b5a07eceba425a179257abf248ad3b982bca4a | ontology 扩展资产、TE 验签、Agent HTTP/MCP 与 additive migration |
| ontology-enhancer | 613f65a29a91477e367955d1742b64b0d8818932 | 原 CLI 保留，README 追加迁入指引，更新兼容的候选契约副本 |

这些 HEAD 不包含当前未提交源码。迁入原始文件摘要与许可证分别保留在 TE 的 `team_ontology/engine/SOURCE.json`、`LICENSE`，变更后模块摘要在 `docs/ontology-integration/results/v5-source-hashes.json`。OV 原有 cicd 配置及 enhancer 原有 README/脚本等工作区改动保留。

## 架构决策与源码定位

- **ADR-V5-01：双服务部署。** TE `team_ontology/api.py` 包装现有 FastAPI lifespan；`service.py` 内置 worker 调用 `extractor.py`，没有 Runtime HTTP 调度。OV `ontology/api.py` 不启动本体 worker。原生 Compile 语义保留。
- **ADR-V5-02：两个本地事务。** TE `control.py` 的 jobs/outbox/audit 同事务；OV `service.py` 提交中完成 CAS、nonce 消费、回执、版本事件。双数据库测试确认 V5 OV DB 不创建 ov_tasks，TE DB 不创建 ov_semantic。
- **ADR-V5-03：批准由 TE 签发。** TE `Operations.approve` 签 Ed25519，OV `AssetService.verify/grant_record` 验签与一次性消费；任务详情隐藏 grant。候选编辑由新制品摘要使旧批准失效。
- **ADR-V5-04：认证保持原生约束。** OV `host.install_native` 复用 get_request_context，api_key 模式忽略伪造身份头并固定用户；Root 数据访问继续禁止。TE 读取当前管理员保存的 team_space 凭证并校验 capabilities 真实主体。trusted 无后端凭证时禁止开启本体。
- **ADR-V5-05：Agent 直接读取。** OV `agents.py` 通过窄适配调用原生 fs/search；增强检索与结构化诊断均基于可见的正式 generation，MCP 不暴露发布或业务动作。
- **ADR-V5-06：可复核迁移。** OV `schema_v5.sql` 独立于 V1；TE 记录 v5-001 迁移。功能关闭不导入依赖 PG 的 API 或启动 worker。历史导入是隔离参考，不代替正式发布。

## 最终验证补充

64 项新/迁入测试通过；旧本体 PG 回归 19 项通过。TE 全量 529 通过、5 失败、34 跳过。OV 较大回归 255 通过、16 失败；已将这 16 项在未修改的 HEAD f1b5a07ec 隔离检出中逐项复现（相同本机 native 二进制和配置，结果 16 项均失败），所以本轮未把它们归为新增回归，也未把原生验收写成通过。

真实模型使用显式配置的 `volcengine/glm-5.2-aicc`，仅处理合成资料：4 次调用、19,762 请求字节、4,438 报告 tokens、76.27 秒、1 条断言，JSON Schema 与 OV 确定性证据校验通过。抽取质量、人工审核成本与真实业务准确率仍未验证。

轮子已构建并检查包含迁入引擎、许可证与 OpenAPI；前端构建、编译、代码 lint、文档引用和离线 HTML 交互通过。TE 停机实验仍可从 OV 查询已发布 generation；后续以 1 条非空事实再次验证，停机前后结果一致，不能替代高可用压测。

## 知识库回执口径

新增 32—35 号内容当前连接器回执为 `saved`（仅提交保存操作，尚未确认落盘/索引/发布）；7 份历史文档的修订建议为 `pending_review`，使用实时读取的 SHA，分页合并后重新计算哈希确认内容未丢失。历史正文保留，只追加 V5 指引。本文件为后续源码与验收补充，不覆盖这些待审核建议。

## 尚未达到完整计划门槛

原生全链路部署、SIT frank 验收、跨来源自动修复、旧 OV 任务审计映射、完整概念层级扩展与反馈工单闭环尚未完成；V5 规模和跨库备份恢复未执行。PRD 关闭，本轮未部署或执行真实业务写动作。

知识库内 32—35 的相对代码/结果路径是 **TE 仓库中的证据位置**，不代表知识库存在同名附件。完整机器报告、测试日志、回执及当前源码摘要保存在 TE `docs/ontology-integration/results/v5-*`。
