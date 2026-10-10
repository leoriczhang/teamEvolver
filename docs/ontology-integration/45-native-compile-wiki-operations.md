# V6：TE 基于原生 OV Compile 的 Wiki 本体构建

状态：本地实现与验证；未部署。V5 的 TE 内置抽取不再是新任务入口。OV Compile 核心保持不变，旧发布资产、审批与回执协议保留。

## 职责和流程

TE 负责来源集合、冻结、Compile 受理对账、Schema 确认、确定性后处理及审核发布；OV Compile 负责来源分批、Agent 草稿和合并；OV Ontology 负责证据权限、私有候选及正式 generation。两个服务不要求共库，也不增加 Ontology Runtime。

目录／单文件／本地上传 → 持久化来源集合 → 成功来源冻结 → 受限 Compile 输入副本 → 原生 Compile + 版本化 Skill → Schema 提案与事实草稿 → 人工确认 → TE 校验转换 → Prepare → Approve → Commit。

一次 Compile 的各来源批次同时产出事实草稿和类型关系提案，合并复用结构化草稿。确认相同 Schema 或明确改名映射无需再次调用模型；语义变化按人工指定 affected_sources 补编译，未指定则全量重编译。影响范围不明确时不要填写局部列表。最多两轮自动修复／Schema 补编译，失败后保留错误与产物位置供人工处理。人工明确重试失败任务会开启新执行目录。

## 启用与部署检查

TE 仍需有效 PG、Ontology 开关、当前 OV 账户本体授权及 sharing 中的 trusted Root Key。新配置位于服务级 ontology：

```yaml
ontology:
  enabled: true
  compile_skill_uri: viking://agent/skills/ontology-extraction-v1
  compile_workspace_uri: viking://resources/te_ontology_compile
  compile_user: te-ontology-compiler
  compile_deadline:
    base_seconds: 1800        # 固定成本：抽取地板(~1000s) + 父流程(~500s) + 零头
    wave_seconds: 4500        # 每个抽取波次（6 批并行，单满批实测 ~4100s）
    merge_wave_seconds: 1800  # 每个合并波次（10 并发）
    safety_factor: 1.5        # 吞吐波动余量（实测 50-60 tok/s）
  compile_model_revision: ""
  source_max_files: 1000
  source_max_depth: 20
  source_max_entries: 10000
  source_max_bytes: 20971520
  source_max_file_bytes: 2097152
```

模型使用 OV 原生 Compile 配置；TE ontology.model 保留兼容读取但不再驱动新任务。固定超时已由按批次数自适应的墙钟替代，见 docs/superpowers/specs/2026-09-28-adaptive-compile-deadline-design.md。compile_model_revision 是运维维护的 OV 模型／Provider 配置修订号，留空禁用跨任务缓存；模型配置改变必须更换该值。缓存仍绑定来源摘要、Skill 内容摘要、输出契约和连接账户，复用前重新校验来源权限和草稿。不是模型自动发现功能。

专用 Skill 随 TE 发布在 `team_ontology/skills/ontology-extraction-v1/`，不会启动时自动安装或覆盖 OV Skill。使用目标 OV 账户的已配置运维 CLI 显式安装：

```bash
ov skills add ./team_ontology/skills/ontology-extraction-v1/ -p viking://agent/skills --wait
```

确认 compile_user 在该账户下能读取 Skill，并可在工作区父路径创建目录。TE 比较实际 SKILL.md 与随包版本的 YAML 元数据及正文（兼容 OV 对 YAML 的换行排版）；实质内容不一致返回 COMPILE_SKILL_VERSION_MISMATCH。修改 Skill 应升级版本、URI及部署包，而不是在线改写旧版本。

compile_user 是后端固定服务身份，不接受浏览器传入；与当前业务映射用户必须不同。继续复用现有后端 Root Key，不新增另一套凭证。工作区按 TE 租户／审批用户／任务／轮次区分，在写正文前设置 restricted ACL 并读回校验，使用单独的普通身份探测拒绝访问。提交和结果收集阶段进一步检查正文读取及搜索不能泄漏。管理员／Root 仍具备原生管理权限，不声称目录对管理员不可见。

OV 无新增配置或页面。它必须已具备可用的原生 Compile Runtime、模型和所需 CLI；Skill／工作区问题在 TE 准备阶段阻止提交，Runtime／模型错误由原生 Compile 受理或任务阶段报告。不能只启用 OV Ontology 就假定 Compile 已配置。当前适配沿用原生 Compile 的可信服务运行边界，不宣称将其现有 shell 工具变成安全沙箱。

## 页面步骤

1. 在“来源”填 Viking URI 和 OV 可读用户，点“创建并冻结来源集合”。目录递归处理；本地文件先上传，再创建集合。刷新后从“来源集合”选择已保存记录。
2. 查看文件、失败清单和预算错误。单个缺失／不可读文件可跳过；根目录、认证、全局服务或规模预算错误终止。没有成功资料不能构建。可重试失败来源。
3. 选择 sources_ready 集合，点击“使用这些来源构建”。“编译 Skill URI”可填 OV Skill 目录或 SKILL.md 路径；留空使用服务默认 `ontology.compile_skill_uri`，出厂值为 `viking://agent/skills/ontology-extraction-v1`。页面按当前租户记住填写值；首次提交后，受理未确认期间锁定原请求，刷新或重试不替换路径。再点“开始 Compile／受理对账”。TE 不再要求预填运单 Schema。
4. 构建任务达到 schema_review，点“查看”。检查提议的 Schema；映射框可填写 type_mapping、predicate_mapping、affected_sources，默认 `{}`。
5. 点“确认 Schema 并生成候选”。Schema 原样确认不会再次调用 Compile。新增类型或语义变化将触发补编译，不自动批准新的 Schema 提议。
6. review_ready 后核查证据、冲突和图视图。存在缺口时阅读清单，并在批准发布前勾选接受未覆盖资料；后端同样强制检查。
7. 依次准备、批准、发布。仅收到 OV 回执才显示 published。默认 delta，无自动删除或业务写操作。

索引文件也属于普通资料：冻结 index.md 不代表冻结了它引用的全部正文。递归范围由目录实际包含的可读文本决定；不追踪范围外链接。不支持的二进制、隐藏和派生文件不作为正文。默认规模上限如上；超过上限明确失败，不默默选前 N 份。

## HTTP 契约

公共前缀为 `/te/enterprise/v1`，使用 TE 控制台管理员认证和当前租户。新增请求 JSON Schema 位于 `team_ontology/contracts/`。

| 方法与路径 | 用途 |
|---|---|
| POST /source-collections | sources.v1；submission_key、roots、references、readers，返回持久化任务 |
| GET /source-collections | 列举当前租户／用户最近来源集合 |
| GET /source-collections/{id} | 恢复进度、冻结回执及缺口 |
| POST /source-collections/{id}/retry | 重试失败项，保留成功引用 |
| POST /source-collections/{id}/cancel | 取消来源任务 |
| POST /jobs | compile.v1；submission_key、collection_id、expected_generation、可选 skill_uri（空值用服务默认） |
| POST /jobs/{id}/retry | 明确重试失败任务；未知受理只进行对账 |
| POST /jobs/{id}/schema-confirm | schema_body、expected_digest、可选 type_mapping／predicate_mapping／affected_sources |
| POST /jobs/{id}/approve | 原 note 加 acknowledge_gaps，存在缺口时必需为 true |

Compile 使用原有 POST /api/v1/compile、GET /api/v1/tasks、GET /api/v1/tasks/{id}、POST /api/v1/tasks/{id}/cancel。没有新增 OV 本体 Compile profile、模型参数或内部模块依赖。

输出契约 `sf.te.ontology.drafts.v1`：schema-proposal.json、coverage.json、drafts/*.jsonl 和 result-manifest.json，详见随包 Skill。TE 验证路径、数量、总字节、Schema、覆盖、引用及正文偏移。Schema 页面列出从已核验事实推导的类型／关系依据位置（每项最多三处）；没有示例支持的定义需人工核查，模型覆盖声明本身不能证明没有遗漏。每轮模型生成 ID 加独立前缀后再汇总，避免补编译与旧草稿碰撞；实体仅按 namespace/type/external_id 合并，反向断言保留。

## 恢复、迁移和限制

新工作流复用 TE 现有 ontology_jobs/outbox/audit 表，不新增 schema，不要求手工建表。每一步通过租约及 attempt 校验回写；人工确认和重新排队为本地事务。无旧构建任务迁移；已有发布回执不改变。

来源集合保存每个实际冻结版本，不代表全目录同一时刻快照。提交前和生成候选前重新检查来源授权。冻结副本在独立工作区中按同字节写入、读回核对；不把可变 Wiki 直接传给 Compile。

POST Compile 前先保存提交意图、唯一目标和完整请求。响应丢失或进程中断后，只用原生任务列表中的精确请求匹配对账。原生列表没有无限历史分页，无法唯一匹配时保留 compile_unknown，需要人工查询原 OV 任务；不会自动重复 POST。OV 任务过期后也不能把“未查到”当作“未执行”。

TE 的 queued/running 是运营步骤，不等于 OV 执行状态；页面另列 remote_status、stage 和 compile_id。取消先阻断 TE 状态推进，再取消已知或对账取得的 OV 任务；结果未知时保持 cancelling。运行超时不代表远端必然停止，必须确认取消回执。

预算不包含对 OV 内部模型策略的覆盖：子 Agent 并发、轮数、模型 Token 上限沿用部署的原生 Compile 配置。TE 限制来源规模、输出 30 MiB、每次远程 HTTP 30 秒、单个运营步骤 600 秒及整体 Compile 等待期限。输出过大／校验不通过不会被当作完整成功。

工作区和失败产物保留用于审计，不自动删除业务来源。清理时按明确任务 URI 操作，不递归删除整个资源根。普通 Compile 仍可索引其输出；隔离依据是原生 ACL，不是“不创建索引”。

### stage_inputs 阶段 PUT /acl 返回 403

如果日志显示 `stage=stage_inputs`、`PUT /api/v1/acl`、`PERMISSION_DENIED`，且 OV 用户是
`te-ontology-compiler`，任务尚未进入 Compile 或候选评审。旧 TE 调用以编译用户的 `user` 角色
设置新资源目录 ACL，但创建目录不保证获得 ACL 的 `manage` 权限，因此首次授权会被拒绝。

2026-09-23 修复仅让 ACL 设置使用已配置的可信 TE 后端身份（Root Key 与映射后的 `admin` 角色）；
授权条目仍仅包含编译用户。ACL 回读、正文处理和 Compile 继续使用编译用户的 `user` 角色，
普通用户读取／检索拒绝探测仍须通过后才能推进；授权失败不会写入来源正文或提交 Compile。
无需把编译用户设成管理员、关闭 ACL 或修改 OV Compile。部署修复并重启 TE 后，对失败构建点击
“重试”；TE 会使用新的执行目录重新准备输入。若后端身份仍被拒绝，应核对有效 Root Key、
账户映射及工作区权限，不绕过隔离检查。

### stage_inputs 阶段 CONFLICT

工作区根目录会被多个任务复用，输入／输出目录也会在设置 ACL 前再次确认。OV 原生 mkdir 对
已存在路径可能返回 `409 CONFLICT`；旧 TE 仅识别 ALREADY_EXISTS／DIRECTORY_EXISTS，因此首次
准备或重试也可能失败。这与发布版本的 CAS 冲突不同，重基准不能解决工作区目录冲突。

TE 现在对明确的 409 已存在／冲突响应，以同一编译身份调用 stat，确认返回的 URI 与目标一致、
isDir 为布尔值且确为目录后继续。日志 `ontology.compile_path_check` 记录目标及预期类型，
`ontology.workspace_reused` 表示目录复用核验通过。遇到同名文件返回 COMPILE_PATH_TYPE_CONFLICT；
stat 响应不合法返回 COMPILE_INVALID_STAT；403、404、超时等仍保留为失败，不跳过 ACL 检查。

输入文件创建遇到同类 409 时，必须确认是同路径普通文件并回读原文；仅完全相同内容可以复用，
不同内容返回 COMPILE_INPUT_CHANGED，不覆盖既有文件。部署并重启 TE 后重试失败的构建即可，
无需清空工作区或修改 OV。回归覆盖目录重复创建、多任务复用、同名文件、stat 失败及输入正文变化；
HTTP 桩验证不替代 SIT 实际验收。

### stage_inputs 阶段写入超时

冻结输入使用原生 content/write 的 `mode=create, wait=false`，随后以编译身份读取 `raw=true`
的正文并逐字核对。原生 Compile 直接读取这些文件，不必等待输入副本的语义和向量处理完成。
这只表示输入正文已核验，不表示索引已就绪。写响应丢失或 5xx 时同样先 stat 和回读核对，
只有文件类型及原文完全一致才能继续；不重复覆盖或把超时无条件当成成功。

### 新目录 ACL 返回 INVALID_ARGUMENT

真实联调中，mkdir 成功后立即 PUT /acl 可能返回
`ACL target has no context record; index it first`。这是目录上下文记录尚未就绪，
TE 将这一精确响应映射为 COMPILE_ACL_TARGET_NOT_READY，最多尝试 6 次，等待合计不超过
11.5 秒（不含接口耗时）。其他参数错误和权限错误不重试。耗尽后保留失败，不写来源正文。

### 原生 Compile 取消与模型等待

原生取消接口可能先返回 cancelling。TE 保持持久化取消围栏，由恢复循环查询终态后再显示
cancelled，不以 POST 成功代替远端已结束。排查模型等待时，以 Compile ID 对照 vikingbot
日志的 agent_id、LLM_START／LLM_END 和追踪记录；主任务 wait_subagents 可能是在等待子任务的
模型调用，不能仅凭 running 判断队列卡死或构建成功。取消确认前不要重复提交同一批构建。

### restricted 已保存但 COMPILE_WORKSPACE_NOT_PRIVATE

目录 ACL 写入成功，不等于账户已开启 ACL enforcement。用管理身份读取
`GET /api/v1/admin/accounts/{account_id}/settings`，检查 `settings.acl.enabled`。
2026-09-23 对 product_agent 的真实联调中，此值为 false：编译目录 ACL 回读为 restricted，
但普通身份仍能 ls，TE 因隔离探测失败停在 stage_inputs，尚未写入冻结原文或提交 Compile。

开启账户级 ACL 使用 OV 原生 `PATCH /api/v1/admin/accounts/{account_id}/settings`，请求体为
`{"acl":{"enabled":true}}`。这会使账户已有权限规则一并生效，需由账户运维确认影响范围；
TE 不自动改变共享账户的开关。开启后重试构建并再次验证普通身份的读取与检索被拒绝，
不能通过删除隔离探测或把编译身份设为管理员继续执行。

每轮保存提交与完成时间及原生任务提供的 Token 用量；未提供的用量保留未知，不记为零。确定性后处理单列 postprocess_ms 与 postprocess_compile_calls=0；这不能替代真实模型的成本测量。

## 验证记录

确定性模型桩验证目录 >100 KB、分页、权限、受理响应丢失、Schema 映射及补编译；独立 PostgreSQL 验证并发受理、检查点持久化、重启及取消围栏。具体运行结果记录在同目录 results/native-compile-validation.json。

真实模型、真实 OV Compile 服务端到端及 SIT 发布验收单独记录；不得把模拟 HTTP 或模型桩计为真实抽取通过。现有全量回归失败单列，性能、Token 和人工审核成本未实测不宣称达标。

现有正式断言契约要求 valid_from。资料没有可支持的业务有效时间时，Skill 不得编造日期；对应事实不进入候选，需补齐资料。冻结日期不等于事实生效日期。原始覆盖声明只能校验结构和范围，不能证明语义上没有遗漏。

2026-09-22 已将随包 Skill 安装到 SIT 的 product_agent 账户，team 和 te-ontology-compiler 均已读回核验；上传响应超时，按内容对账确认写入，索引处理状态未确认。见 [安装核验记录](results/ontology-skill-sit-install.json)。页面路径输入改动需部署新版 TE 后生效，未自动部署。
