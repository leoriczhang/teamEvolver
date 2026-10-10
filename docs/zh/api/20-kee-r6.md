# KEE R6 方法治理 API

实现：[治理路由](../../../team_ontology/kee/api.py)、[四契约](../../../team_ontology/kee/contracts.py)、[源码基线](../../kee-r6/BASELINE.md)。所有请求使用现有控制台 Session／可信服务 Root Key和租户上下文；不接受浏览器任意 OV Account/User。

## 接口与参数

前缀 `/kee/v1`。JSON 请求须 `Content-Type: application/json`，最大 8 MiB。领域写操作限当前租户管理员／有效支持模式；读取限现有 `team.read` 与对象读者范围。

| 方法／路径 | 请求或作用 |
|---|---|
| GET /capabilities | 执行器、三视角、可选结构接口状态；无执行拦截 |
| POST /sources | roots[] 文件／目录、uploads[{name,text}]、readers[] TE 用户（最多20） |
| POST /documents | filename、content_base64、source_id、readers；复用 PDF/Word/Excel 等解析器，不经过 OV 本体 API |
| GET /sources/{id} | 冻结原文、摘要与逐项缺口；仅管理员且在读者范围 |
| POST /sources/{id}/revoke | 先围栏和阻断依赖版本，再在原队列清理；可能返回 cleanup_jobs |
| POST /methods/build | contract=kee.method-build.v1、submission_key、collection_id、pack_id、clarification；202 |
| GET /methods/jobs[/{id}] | 当前主体任务与旧执行器历史，保留 ID |
| PUT /methods/jobs/{id}/candidate | expected_digest、candidate ProcedurePack；使旧批准失效 |
| POST /methods/jobs/{id}/confirm-schema | expected_digest；确认后确定性导出 |
| POST /methods/jobs/{id}/prepare | target_uri 安全 resources URI、readers[]；校验来源、引用、基线 |
| POST /methods/jobs/{id}/approve | note、acknowledge_gaps；五分钟有效 |
| POST /methods/jobs/{id}/commit | commit_key；202/queued 仅受理，不代表 published |
| POST /methods/jobs/{id}/retry、cancel、withdraw | 恢复对账、取消围栏、整个版本撤回 |
| POST /methods/jobs/{id}/rollback | commit_key；新任务／新版本，重新 Prepare 与批准 |
| GET /methods/jobs/{id}/export、impact | 文件与结构导出；变化列表、来源／链接数量 |
| GET /wiki/releases?after=... | 按 key 分页100；只返回授权读者条目 |
| GET /wiki/releases/{target_hash}/{generation}/pages/{path} | 固定版本、原权限与页面摘要核验；不读取任意文件 |
| POST /feedback | note；只写原审计，不修改资产 |

## 契约及身份

[ProcedurePack](../../../team_ontology/kee/contracts/ProcedurePack.schema.json) 包含方法的条件、禁止、例外、步骤、完成标准、引文和 Schema。
[CapabilityBinding](../../../team_ontology/kee/contracts/CapabilityBinding.schema.json) 记录既有 Skill ID/version/input/output，Prepare 查询当前租户已有 Skill registry，不新增注册表。
[CaseEvidence](../../../team_ontology/kee/contracts/CaseEvidence.schema.json) 只记录引用和元数据；reference 是既有 objects key，如 `sessions/...`、`skill_evidence/...`、`kee/r6/releases/...`。支持／矛盾结论须绑定原记录 digest，不复制轨迹。
[WikiRelease](../../../team_ontology/kee/contracts/WikiRelease.schema.json) 分别记录上传、索引等待、入口未知、生效、撤回；回执 `package_atomic=false`。

## 示例

```bash
curl http://127.0.0.1:52010/kee/v1/sources \
  -H 'Content-Type: application/json' -H 'X-Tenant-Id: demo' \
  -H 'Authorization: Bearer <已配置的可信后端凭据>' \
  -d '{"roots":["viking://resources/wiki/handover"],"readers":["运营用户ID"]}'
```

生产优先使用已登录控制台；Root Key 永不下发浏览器。服务 Root 主体只可声明自身读者；多人运营走现有已认证用户映射。

## 可选结构读取

只保留 POST `/ontology/query` 和 `/ontology/check`，`ontology.kee_query_enabled` 默认 false。未开启返回404，不暴露 MCP。query 固定 generation，扫描最多10000、结果最多100、求值时间最多1000ms。check 使用 subject/predicate/expected 精确条件，返回 TRUE/FALSE/UNKNOWN、conflict、gaps、facts、generation、截断与 caller_supplied 非权威标记，`execution_authorized=false`。

首期不执行递归规则、任意推理、动态业务事实或跨案核验。方法中的时间／限定条件保留在正文，简单静态 Fact 检查不覆盖全部业务语义。

## 状态与错误

`queued → running → schema_review → review_ready → prepared → approved → queued → published`；导入回滚候选为 imported；取消 cancelled；异常 failed；撤回 withdrawn。索引等待体现在 result.release.state，不伪装成已发布。

| 状态／错误 | 处理 |
|---|---|
| 403／SOURCE_REVOKED、READER_MEMBERSHIP_REQUIRED | 检查当前租户及原件／Wiki ACL，不改浏览器身份头绕过 |
| 409／CANDIDATE_DIGEST_CONFLICT、VALID_APPROVAL_REQUIRED | 重新读取、编辑、Prepare 和审核 |
| 409／WIKI_BASELINE_CONFLICT_REAPPROVAL_REQUIRED | 基线变化，重新审核；不自动覆盖 |
| 409／WIKI_DEADLINE_MANUAL_RECONCILIATION_REQUIRED | 对账实际正文、索引和入口，撤回清理后重新构建 |
| 413／SOURCE_*_LIMIT、STRUCTURE_SIZE_LIMIT | 拆来源集合，不静默裁正文 |
| 422／SOURCE_EVIDENCE_MISMATCH、CAPABILITY_SKILL_VERSION_NOT_FOUND | 修正证据字符位置或实际引用 |
| 429／ONTOLOGY_QUEUE_FULL | 队列最多32，延后重试 |
| 503／WIKI_INDEX_NOT_CONFIRMED | 原任务继续对账，不重编译／重写相同正文 |
| 410／KEE_R6_USE_METHOD_BUILD | 原生 R6 安装退出旧新建本体路径；旧任务查询取消保留 |

原生安装的旧 `/te/enterprise/v1` 写操作统一返回410，取消除外；capabilities 返回后续接口指引，不继续调用已退休 OV 本体能力。旧任务保留查询／取消，其他历史源和资产需按迁移工具导出核验，不能将接口路径保留理解为仍有旧 OV 写者。
