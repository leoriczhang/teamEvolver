# 成功经验同步到 OpenViking

TE 后台将 PostgreSQL 中按 Skill 聚合的成功经验 JSON 投影到 OpenViking。PG 是真源，OV 不可用不会阻塞经验保存。同步不调用模型，也不触发 Skill 发布或 Ontology 构建、审批、发布。

## 同步哪些经验

自动同步及“立即同步到 OV”只扫描当前租户 `objects` 表内的 `experience_library/{skill_slug}.json`，兼容已有路径前缀。排除 `sessions/`、隐藏文件以及其他文档，仅导出 `kind="exemplary"`。

**当前 Judge/Session 主链路不会自动写入这种旧聚合 JSON。** 自动同步不恢复旧写入入口。经验库页面中的 Session 分析记录可通过新增的“导入存量成功经验”显式导入，见下文；导入不改变 Judge 写入链路。

首次启用分批补齐历史，后续比较稳定正文的 SHA-256。新增经验创建文件；同一经验正文变化更新固定 URI。次数、Session ID、用户列表、分数和出现时间不参与摘要、不上传，因此仅成功次数增长不产生 OV 写请求。源端裁剪或删除经验时，已同步文件保留，本轮不提供自动撤回。

## 配置

```yaml
experience_sync:
  enabled: true
  target_directory: viking://resources/agent_knowledge_workspace/input/proven_experiences
  interval_seconds: 30
  batch_size: 100
  full_scan_interval_seconds: 86400
  import_max_source_mb: 64
```

代码及本地、PRD 模板默认关闭；SIT 模板开启。部署时同步实际挂载的 YAML 并重启 TE；只改仓库配置不会刷新运行实例。无需新增环境变量或 OV 凭证。

`target_directory` 可设为其他 `viking://resources/` 下的具体目录，省略时使用上面的默认值，末尾 `/` 可省略。不能设为资源根目录、用户私有空间，或包含 `..`、空路径段、查询参数、片段、百分号编码等歧义路径。切换目录会重新补齐当前源文档到新位置，保留旧目录文件，不自动搬移或删除历史文件。

要求 `storage_pg.enabled=true`，以及有效的 `sharing` 连接。账户、用户使用租户有效配置；后端凭证优先取 `sharing.viking_team_api_key`，为空时使用 `sharing.viking_api_key`。不使用浏览器提供的账户、用户或个人 API Key。

同步复用配置的 PG schema，不要求固定为 `teamevolver`。状态保存在同一 `objects` 表的 `successful_experience_sync/v1/` 前缀，沿用 RLS，无新增业务表或手工建表要求。每个后台 pass 使用独立于进化任务的 PG advisory lock；最多两个 pass 并行，PG 连接池至少需要四个连接以容纳锁及查询。

## OV 文件与读取

```text
viking://resources/agent_knowledge_workspace/input/proven_experiences/{source_key_hash}/{experience_id}.json
```

路径中的不安全标识转换为摘要；源对象 key 使用 SHA-256，避免清洗 Skill 名称后的碰撞。正文保留原经验 ID，并包含格式版本、Skill、experience_key、kind、description 和源 key。OV 中的正文不包含实时统计，计数继续在 TE 查询。

以上为默认目录；自定义时实际文件位于 `{target_directory}/{source_key_hash}/{experience_id}.json`，状态接口返回该租户的实际目标目录。

通过原生 `/api/v1/fs/mkdir`、`/api/v1/content/write` 和 `/api/v1/content/download` 写入与核对。写入使用 `wait=true`；只有内容回读摘要一致且 OV 返回 `semantic_status=complete`、`vector_status=complete`，才计为 `synced`。文件存在但索引未完成时仍待重试。

Agent 使用其受限 OV 凭证读取或检索该目录；这些文件是成功经验资源，不等于已发布 Ontology 事实。不再追加 TE 租户目录；共享 OV Account 的访问可见性仍由 OV ACL 决定。相同账户、目标目录、来源 key 和经验 ID 会对应相同文件。

## 增量与故障恢复

- 默认每 30 秒启动一次扫描，每批最多 100 个源对象。大批次分多轮执行，因此间隔不是完成时限。
- `(updated_at, key)` 游标带 120 秒重叠窗口，每日全量核对补偿长事务和历史数据；时间戳不能单独证明经验是新增的。
- 待上传记录先落 PG，再推进扫描游标；异常退出不会让未持久化的上传被跳过。
- HTTP 单请求超时 30 秒；失败按 5 秒起指数退避，上限 15 分钟，实际重试还受扫描调度间隔影响。
- 响应丢失后回读固定 URI 的摘要；匹配正文也不代表索引就绪，仍通过等待写入结果完成核对。
- 每日核对也会回读上述聚合 JSON 对应的已同步正文，摘要一致不重写；不一致时修复。这是最终一致的投影，不保证跨服务原子提交。
- endpoint/account/user/target_directory 变化会使用新的目标状态重新核对当前源文档。旧目标文件不会自动删除；已从源 JSON 裁剪的历史经验不会自动迁往新目标。
- 自动同步旧聚合 JSON 时，每个源 JSON 最大 1 MiB、最多 1,000 条经验；显式存量导入的 Session/历史来源使用下文的独立预算。超过限制或内容结构无效时报告错误并跳过，修正来源后再次扫描。

## 状态与排查

租户管理员调用 [GET /api/experience-sync/status](../api/16-experience-sync.md)，检查 `last_scan`、`counts`、`last_error`。接口无任意文件读取能力，不返回经验正文或凭证。

在服务日志中查找 `experience_sync.started`、`experience_sync.delivery`、`experience_sync.source_rejected`、`experience_sync.blocked`、`experience_sync.recovered`。日志关联租户、源 key、经验 ID、摘要和 OV 路径，不输出经验正文。

常见错误：`PG_REQUIRED`、`SHARING_DISABLED`、`OV_IDENTITY_OR_KEY_MISSING` 为配置问题；`UNAUTHENTICATED`／`FORBIDDEN` 为 OV 鉴权问题；`OV_TIMEOUT`／`OV_NETWORK_ERROR` 为连通性问题；`OV_INDEX_NOT_READY` 表示文件可能已写入，但检索处理尚未确认。

关闭 `experience_sync.enabled` 并重启会停止新上传，PG 状态和 OV 文件保留，重新开启后继续核对。此变更不会自动部署或启用 PRD。

## 本地验收记录（2026-09-21）

基线提交 `48150e8`，加本次未提交实现；Python 3.12.9，隔离 PostgreSQL 17.11，运行角色非 SUPERUSER、非 BYPASSRLS。

| 项目 | 结果 |
|---|---|
| 新增同步测试 | 41 项通过，其中 5 项使用真实隔离 PG；包含自定义目录、目录切换和路径校验 |
| PG 故障与隔离 | RLS、同时间戳分页、独立跨实例锁、锁连接丢失、CAS、迟提交后的全量补偿通过 |
| HTTP 契约 | 鉴权失败、超时、响应不确定、固定 URI 创建竞争、索引未就绪分类通过 |
| 真实 TE 启动及重启 | 使用隔离 PG 和 OV HTTP 契约测试服务，验证历史补齐、计数不写入、正文更新、索引待重试与重启不重复写入 |
| Python 编译、增量 Ruff、文档引用 | 通过 |
| 全量 pytest | 693 通过，47 跳过，3 项既有失败 |
| 真实 OV 检索、SIT／PRD 部署、规模性能 | 未执行；HTTP 测试服务的索引状态不代表真实 OV 检索效果验收 |

既有失败单列：

- `test_routes_accept_batch_and_preserve_historical_source_after_removal`（Dataset Collection 路由）。
- `test_envelope_only_prompt_title_comes_from_nested_query`（SF Agent 适配）。
- `test_agentshub_config_sync_merges_personal_sources`（Memory 配置合并）。

隔离 PG 测试入口为 `tests/test_successful_experience_sync_pg.py`，通过 `TE_EXPERIENCE_TEST_PG_DSN` 显式指定可创建临时 schema 的测试数据库；未设置时跳过，不读取部署数据库配置。每项测试清理自己创建的 schema。前端代码未改动，无需重新构建前端。


## 经验库手动触发（2026-09-22）

1. 使用管理员登录，切换到需要同步的租户，打开**经验库**。
2. 查看顶部“成功经验同步到 OV”的实际目标目录及开关状态；需有效 PG、sharing 和 `experience_sync.enabled: true`。
3. 点击**立即同步到 OV**。后台对当前租户经验 JSON 做分页全量核对，上传仍按摘要增量判断；不受列表搜索或筛选影响。
4. 页面每 5 秒刷新状态。已受理不代表上传成功；本轮核对结束后，查看待处理、已同步、等待重试和最近错误。已同步要求正文回读匹配且 OV 语义与向量处理完成。

不变正文（包括仅计数变化）不重复上传；失败项保持既有退避。若提示已有同步正在执行，稍后查看状态或再次触发。请求及进度持久化在现有 objects 表，TE 重启后继续处理。不会修改本体或删除 OV 文件，不需要数据库迁移或新配置项。

列表展示的 Session 分析经验与同步来源并不完全相同；当前 Judge 不会自动写入旧的按 Skill 聚合 JSON。“立即同步到 OV”不会补写这条链路；需要覆盖页面存量时使用“导入存量成功经验”。非管理员无按钮权限。接口详见[成功经验同步接口](../api/16-experience-sync.md)。


### 本地验证记录

2026-09-22，基于 `e4704c7` 加本次工作区修改，使用 Python 3.12.9、PostgreSQL 17.11 非超级用户隔离库及 OV HTTP 桩验证：

- 同步专项：48 通过，含 PG 持久化、跨实例锁、重启恢复、租户隔离及手动触发断连。
- 全量 Python：749 通过、48 跳过、3 项既有失败：`test_routes_accept_batch_and_preserve_historical_source_after_removal`、`test_envelope_only_prompt_title_comes_from_nested_query`、`test_agentshub_config_sync_merges_personal_sources`。
- 前端：71 通过；生产构建成功，保留既有的大 chunk 提示。
- Python 编译、修改文件 Ruff、文档引用检查、本地前台启动与 daemon 重启验证通过。

本轮没有部署 SIT/PRD，没有通过按钮触发真实 OV 上传；真实目标的内容和索引结果仍需部署后验收。


## 导入存量成功经验

管理员在**经验库 → 成功经验同步到 OV**点击**导入存量成功经验**，调用 `POST /api/experience-sync/import`。它覆盖当前租户的旧聚合 JSON，以及 Session 索引、`session_archive/`、`sessions/`、`experience_library/sessions/` 和 `skill_evidence/` 中已有的成功经验。只读取已生成的分析结果，不调用模型，不接收本地文件上传，也不重新评判 Session 是否成功。

导入按以下阶段异步执行：扫描历史记录 → 分页扫描 Session 索引 → 准备去重文档 → 核对旧聚合 JSON → OV 增量写入及回读。扫描不使用页面缓存或列表筛选，没有页面最近 10,000 条的截断。进度保存在既有 PG objects 表，重启续跑；同租户导入和同步共用锁，已有另一类请求时返回 409。

Session/历史来源按 `(skill_name, exemplary, experience_key)` 生成固定标识，取 `(timestamp 或 ingested_at, session_id)` 排序最新的正文；并列优先索引，其余并列按来源和正文摘要稳定排序。旧聚合 JSON 继续使用其原有来源和经验 ID，二者保留各自来源身份，不自动把不同来源的记录覆盖为同一条。重复导入相同正文不重复写入；修改正文替换固定 URI，统计不上传，源删除不删除 OV 文件。

导入文档的 `source_key` 为 `experience_library/session-derived-{skill摘要}.json`，是稳定逻辑来源标识，不代表 PG 中新建了旧聚合文件。归并中间结果只在同步器私有状态前缀中保存。后续新增 Session 仍需再次点击存量导入；它不会让自动扫描扩展为持续读取 Session。切换目标目录或 OV 账户后，也需重新导入 Session 存量到新目标。

页面显示已扫描来源数、发现成功记录数、准备的去重文档数、跳过来源数及安全错误码。成功记录数可能包含多次出现；去重文档数不等于本轮新增写入数，也不等于 OV 索引完成数。最终仍看同步计数及错误。

Session/历史来源由专用 PG 查询在数据库内解析并仅投影经验与归并所需的标识、时间；不会把完整轨迹传回 TE。来源发现只读取 key、大小、更新时间和行版本。`experience_sync.import_max_source_mb` 默认 64 MiB，可配置为 1—1,024 的整数，不改变其他业务读取接口。每页最多 100 条、投影总量最多 1 MiB，单条投影最多 256 KiB；历史数组不再受 1,000 条总数限制。单次查询最长 30 秒，已有更短的 PG 超时仍生效。大数组分页会重复解析当前来源，因此提高来源预算需同时评估数据库资源，不能据此保证吞吐。

来源异常时继续其他来源；单条经验超限时保留该来源其他有效记录。`manual.import.error_counts` 按错误码计数，`errors_sample` 最多返回 20 条缺口摘要；完整失败记录保存在同步私有前缀的 `import-errors/{request_id}/` 下。日志 `experience_sync.import_source_rejected` 包含来源标识、版本、阶段、可用的大小与限额，不含轨迹或经验正文。页面在本轮结束且有缺口时显示“部分完成”，区分来源缺口、待上传/重试，以及正文已写入但索引未确认。旧任务缺少新增明细时保留其原有计数和错误，不自动清零。

| 错误码 | 含义及处理 |
| --- | --- |
| `IMPORT_SOURCE_TOO_LARGE` | 原始来源超过配置预算；查看 source_bytes/limit_bytes，拆分来源或评估资源后调整预算 |
| `IMPORT_RECORD_TOO_LARGE` | 单条经验投影超过 256 KiB；查看 ordinal/record_bytes，处理对应记录后重导 |
| `INVALID_IMPORT_SOURCE` / `INVALID_IMPORT_RECORD` | JSON 或经验结构无效；修复指定来源或数组位置 |
| `IMPORT_QUERY_TIMEOUT` | 解析或确认查询超时；检查 PG 负载和有效超时后重导 |
| `IMPORT_SOURCE_CHANGED` | 分页中行版本变化；未确认内容不归并，下次导入读取新版本 |

每个来源保存版本、解析阶段和数组游标，分页经验先落私有暂存；全部读取并确认版本后才进入归并。中断可续跑；变更来源的暂存内容会清理，已确认的其他来源继续。经验正文不会额外截断，也不调用模型猜测或修复。

每轮按受理时的数据库时间划定读取上界，分页期间源仍可变化；这不是跨来源一致快照。执行中修改或晚提交的记录可能需下一次导入才能覆盖。


### 存量导入本地验证（2026-09-22）

基于 `5a954c8` 加本次工作区修改，Python 3.12.9、隔离 PostgreSQL 17.11、非超级用户及 OV HTTP 桩：同步/导入专项 58 通过，含 10,005 条 Session 的分页读取、跨租户、断连、恢复、统计不重写、正文原址替换和无效来源缺口。全量 Python 759 通过、48 跳过，仍为前文单列的 3 项既有失败。前端 72 通过，生产构建、编译、修改文件 Ruff、文档检查及本地前台/daemon 启动重启验证通过。补充的畸形 Judge 字段跳过用例也通过。

未在 SIT/PRD 部署或执行真实存量导入，PG/HTTP 桩不代表真实 OV 检索效果验证。万条用例验证分页完整性，不作为吞吐性能达标结论。


### 升级与 OV 冲突恢复

本修复仅修改 TE，不修改 OV、表结构或已有文档身份。部署并重启 TE 后，原有待同步记录（包括此次日志中的 73 条）沿用原 URI、摘要与退避时间继续重试；不要清理同步状态。待旧导入结束后，再点击一次“导入存量成功经验”，覆盖先前因旧限额跳过的来源。已成功且正文不变的记录不重复写入；仅统计变化仍不会写入。

`mkdir` 返回 `409 CONFLICT` 或 `ALREADY_EXISTS` 时，TE 用同身份调用 `/api/v1/fs/stat`，只在返回相同 URI 且 `isDir=true` 时继续。确认过的目录仅缓存在当前 worker 批次。若路径是文件，返回 `OV_PATH_TYPE_CONFLICT`；stat 的 403、404、超时及格式异常均不能当作成功，格式异常为 `OV_INVALID_STAT`。创建文件遇到 409 时同样先确认目标为普通文件，最多重试一次 replace。

日志 `experience_sync.ov_request` 标明 mkdir/stat/write/readback 阶段、接口、URI、耗时、状态和 OV request ID；成功高频请求为 DEBUG，故障为 WARNING。`experience_sync.index_pending` 表示语义或向量处理未完成，仍需重试；正文存在不等于索引就绪。最终完成仍要求回读摘要一致和两个处理状态均为 complete。SIT 的真实内容与检索结果须部署后另行验收，本地 HTTP 契约测试不替代该验收。


### 本次大来源与冲突修复验收（2026-09-22）

基线 `a789d9c` 加本次工作区修改；Python 3.12.9、隔离 PostgreSQL 17.11（非超级用户）、OV HTTP 契约桩。

| 验证 | 结果 |
| --- | --- |
| 同步/导入专项 | 79 通过，含 16 项真实隔离 PG 用例 |
| 来源预算与恢复 | 大于 1 MiB 的 Session 只投影经验；1,005 条有效历史经验及单条超限缺口；10,005 条 Session 分页；跨页变更、私有暂存、重启恢复、租户隔离通过 |
| OV HTTP 契约 | mkdir 冲突后 stat 确认、路径类型冲突、403/404/超时、创建竞争、索引待确认和重复导入通过 |
| 全量 Python | 801 通过、48 跳过、5 项基线已存在失败；本次专项无失败 |
| 前端 | 73 通过，生产构建通过，保留已有 chunk 大小提示 |
| 工程检查 | Python 编译、修改文件 Ruff、文档引用和 diff 空白检查通过 |
| 本地服务 | 隔离配置前台启动与 daemon 重启通过；此启动检查关闭 PG/OV 功能，不作为真实 OV 联调证据 |
| SIT/PRD、真实 OV 内容与检索 | 未执行，未触发真实导入 |

本基线的五项既有失败：`test_routes_accept_batch_and_preserve_historical_source_after_removal`、`test_engine_candidate_is_visible_to_tenant_validation_worker`、`test_tenant_model_settings_are_isolated_and_secrets_are_masked`、`test_envelope_only_prompt_title_comes_from_nested_query`、`test_storage_status_reports_local_skills_and_pg_sessions`。上方记录对应更早基线，保留作为历史，不代表本次测试结果。规模用例用于验证完整性，不宣称性能达标。


### 取消 TE 租户子目录（后续路径调整）

当前 URI 为 `{target_directory}/{source_key_hash}/{experience_id}.json`，不再追加 `t_...`。无需新配置。此调整取代上文历史验收中的租户目录路径约定；PG 状态仍按租户隔离，现有 PG 租户锁继续防止同租户跨实例重入。固定 URI 和摘要去重保留。目录冻结固定内容版本，不能代替执行锁；本轮按约定不处理跨租户共用目标的互相覆盖。

升级后 worker 逐条识别旧租户前缀 URI，在原状态 key 内以 CAS 迁移到新 URI，保留经验 ID、正文与摘要，并将旧 URI/错误/次数保存在 `previous_destination`。目标地址已变化，所以新地址需要重新核对并从 pending 开始；旧目标的退避不继续阻挡新地址。包括此前已同步记录，也会补齐到新位置。日志 `experience_sync.destination_migrated` 记录路径变化。迁移幂等，不重建经验身份、不清库，也不删除 OV 旧目录或其中的文件。目标地址未变化的重试仍保持原退避。

本次路径调整验证：专项 72 项通过；全量 Python（含真实隔离 PG）810 通过、48 跳过，仍为上述 5 项既有失败。新增用例覆盖旧 retry/synced 记录转向新路径、重复迁移不重写、旧文件保留和租户锁防重入。本地服务已用隔离配置重启验证；未部署 SIT、未删除真实 OV 文件。
