# Ontology 统一知识运营

默认关闭。TE 内置 enhancer 引擎，统一控制台认证、租户配置、模型和 PostgreSQL 任务；OV 提供来源、隔离制品、正式版本和 Agent 查询。只部署 TE 与 OV。

- [V5 实施说明](../docs/ontology-integration/32-te-ov-v5-implementation.md)
- [契约与启用迁移](../docs/ontology-integration/33-v5-contracts-and-operations.md)
- [Agent HTTP/MCP 接入](../docs/ontology-integration/34-v5-agent-guide.md)
- [验收与限制](../docs/ontology-integration/35-v5-acceptance.md)

`engine` 来源与许可证随包保留，原 enhancer CLI 不受影响。`standalone` 仅为明确启用的本机实验 harness，真实部署接入原有 TE FastAPI 服务。导出领域包或构建完成均不表示正式发布，以 OV 回执为准。

Ontology 发布复用 trusted + Root Key，保留人工审批，不再配置发布公私钥。见 [发布与升级说明](../docs/ontology-integration/38-trusted-root-publication.md)。

本体任务表复用 `storage_pg.schema`，默认 `teamevolver`，使用 `ontology_` 表名前缀。已存在的 schema 不要求数据库 CREATE 权限；旧表升级前需停止旧 worker。详见 [schema 配置与升级](../docs/ontology-integration/40-te-ontology-pg-schema.md)。
