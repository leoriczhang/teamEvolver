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


## 日期缺口与目录 ACL 等待

断言分为两类。有时效的断言必填 `valid_from`（ISO 8601），须带时区；无时区的值会被接受并归一化为北京时间（UTC+8）。常设断言——定义、规则及无固有时间的属性——可省略 `valid_from`：其自来源文档快照起有效，直到 `valid_to` 或后续修订取代。`valid_to` 可空，填写时必须严格晚于起点。瞬时事件不应把同一时间填入起止字段。TE 不会用当前时间、文档更新时间或其他事实的时间补齐日期。

Compile 草稿中日期不合法的断言进入审核缺口，有效断言仍可生成候选；候选保留缺口的主体、谓词、来源与证据标识。`result.assertion_gaps` 在 Schema 审核时提供缺口，候选生成后合入 `result.gaps`，发布批准仍需 `acknowledge_gaps=true`。没有任何有效断言时返回 `COMPILE_ASSERTION_DATES_INVALID`，最多补编译两次；`repair_issues` 只传最多 20 项字段路径和稳定错误码，`repair_issue_count` 保存总数，不含原文或无效值。

OV `mkdir` 会异步提交目录上下文索引，HTTP 成功不保证 ACL 可立即设置。TE 遇到 `COMPILE_ACL_TARGET_NOT_READY` 后保持 queued/stage_inputs，并持久化 `result.acl_wait`（起始时间、目标、次数、下次重试时间）；按 2—15 秒退避，累计最多 300 秒。未完成 restricted ACL 回读和普通身份拒绝检查前不写入来源正文。超时返回 `COMPILE_ACL_WAIT_EXHAUSTED`，需检查 OV 索引队列及对应目录上下文。手动 retry 保留同一 round 和工作目录，重新给一次等待窗口，不新建编译轮次。

部署应同时更新 TE 和 OV 上的配套 `ontology-extraction-v1/SKILL.md`；只更新一侧会触发摘要不匹配。本次未修改 OV 原生 Compile、mkdir 或 ACL 接口。
