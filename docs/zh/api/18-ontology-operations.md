# Ontology 统一知识运营 API

当前新构建使用 [V6 原生 Compile 操作指南](../../ontology-integration/45-native-compile-wiki-operations.md)：TE 递归冻结 Wiki 目录或单文件，调用未修改的 OV Compile，通过版本化 Skill 同时生成 Schema 提案和事实草稿。人工确认后由 TE 后处理生成候选，再按 OV 回执确认发布。

[V5 契约和运维](../../ontology-integration/33-v5-contracts-and-operations.md) 保留历史设计；其中 TE 内置抽取的职责由 V6 替代。发布仍沿用 [trusted + Root Key](../../ontology-integration/38-trusted-root-publication.md)，无需独立发布签名密钥。

## 主要接口

新增 `/te/enterprise/v1/source-collections` 的创建、列表、详情、重试和取消接口；POST `/jobs` 使用 `sf.te.ontology.compile.v1` 请求及 collection_id；`/jobs/{id}/schema-confirm` 确认 Schema，`/jobs/{id}/retry` 重试或受理对账。存在覆盖缺口时，approve 请求必须明确设置 acknowledge_gaps。

来源集合持久化保存，可刷新页面后继续操作。成功来源冻结后显示 sources_ready；schema_review 表示等待 Schema 人工确认；review_ready 表示候选待审核；compile_unknown 表示原生 Compile 是否受理仍待对账。详情、配置和完整接口表见 V6 指南。

旧 `/snapshots/freeze` 仍为单文件原语，直接传目录返回 SOURCE_URI_IS_DIRECTORY。目录构建应使用来源集合，不能仅删除 URI 末尾的 `/`。

## HTTP 页面兼容

旧页面可能在生成幂等键时报 `crypto.randomUUID is not a function`，请求尚未发出。新版前端使用 getRandomValues 兼容 HTTP；更新静态产物并刷新，无需修改 PG／模型配置。受理响应丢失时复用同一请求，不生成第二个任务。

## 构建 Skill 路径

POST `/te/enterprise/v1/jobs` 增加可选 `skill_uri`。空字符串或省略时使用服务默认，默认值为 `viking://agent/skills/ontology-extraction-v1`。支持 Skill 目录或其 `SKILL.md` 路径，拒绝外部 URL、非 Skill 命名空间和路径穿越。`GET /te/enterprise/v1/capabilities` 返回 `compile_skill_uri` 供页面显示默认路径。实际路径保存在任务结果的 `skill_uri` 中，恢复和重试保持不变。指定路径不改变编译身份，也不跳过当前 ACL 与配套 Skill 内容校验。
