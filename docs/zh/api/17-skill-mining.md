# Skill 挖掘 API

这些控制台内部接口管理 OpenViking Compile 多轮执行与人工 Evidence 补充。需要有效的控制台 Session Cookie，当前租户身份为 `tenant_admin`，或平台身份为 `platform_admin`；普通成员与租户机器凭据不能调用。Agent 数据面协议保持不变。

接口由 `team_miner/api/` 统一装配，Skill 挖掘业务位于 `team_miner/skill_miner/`，知识库挖掘位于 `team_miner/knowledge_miner/`。任务、轮次、补证与制品内容通过 [PostgreSQL Repository](../../../team_miner/skill_miner/repository.py) 持久化；临时工作目录只是可重建缓存。操作流程见 [多轮 Skill 挖掘](../guides/17-multi-round-skill-mining.md)。

## 默认交付与候选资格

当前默认编译器为 `knowledge_compiler_v8.py` 的知识点 Skill 编译器，入口为 `team_miner/skill_miner/knowledge_compiler.py`。它声明 `completion_policy: te-compile-finished.v1`：TE 以远端 Compile 完成并保存返回产物记录挖掘完成，不把内容验收、Test Dataset 或人工批准作为完成条件。该模式不要求生成 `benchmark.json`，产物清单明确 `content_validated=false`、`can_submit=false`。

因此，“挖掘完成”不等于“可进入 Candidate Review”。本文的能力计划、12–24条案例和人工 Evidence 问题契约，仅适用于显式使用该评审契约的任务；只完成交付的包不能直接提交，也不会由 TE 补造评测或案例。此类任务在轮数上限内可提交明确的修订要求继续下一轮，不能空转。支持评审的完整包通过当前独立候选的重新校验后，才进入下文的提交接口。

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/mining/jobs` | 创建任务并提交第一轮 Compile |
| GET | `/api/mining/jobs` | 查询任务列表和摘要 |
| GET | `/api/mining/jobs/{job_id}` | 查询轮次、产物、日志和人工复核状态 |
| POST | `/api/mining/jobs/{job_id}/supplements` | 在当前人工复核阶段保存附件 |
| POST | `/api/mining/jobs/{job_id}/continue` | 保存补证并启动下一轮 |
| POST | `/api/mining/jobs/{job_id}/finish` | 保存最终复核并结束挖掘 |
| POST | `/api/mining/jobs/{job_id}/resume` | 恢复已识别失败，或显式授权一次格式修复实测 |

## 执行与配置

### 后台写入结果

上传、转换、产物保存和其他需要外部 I/O 的长操作先写入 PostgreSQL `worker_tasks`，返回 HTTP 202 和以下回执。API 不在请求内执行 Compile 轮询或批量文件转换。

```json
{"job_id":"skill-miner-command:example","status_url":"/api/mined-jobs/skill-miner-command:example"}
```

客户端查询回执中的 `status_url`，在 `queued`、`running`、`retry_wait`、`waiting_external` 或 `reconciling` 时继续等待。`succeeded` 的响应必须包含 `result`，其中保留对应业务操作的原响应结构，例如上传的 `file_count`、保存后的 `content` 或更新后的 `job`；`failed`、`cancelled` 返回 `error_code`，不能当作保存成功。查询始终使用提交任务时的租户。控制台最多等待20分钟，超时不会重发写请求；后台任务继续保留。

Skill 挖掘任务本身的 `/api/mining/jobs` 列表和详情沿用下文业务状态。业务任务与承载短命令的 Worker 任务分别标识，不能仅凭 HTTP 202 判断 Compile 已执行或制品已保存。

### 设置版本

租户的 `miner` 模块配置必填；缺失时返回503，不从 YAML 或本机目录回退。它提供知识库挖掘与 Skill 挖掘共同需要的运行配置。Skill 挖掘的轮数、运行期限与编译指令另外保存在 PostgreSQL `skill_mining_settings`，不会改写知识库挖掘的设置。

`GET /api/mining/settings` 返回独立的 `revision`。`POST /api/mining/settings` 和 `POST /api/mining/prompts/ov_compile/reset` 必须提交对应的 `expected_revision`；更新采用 compare-and-swap，版本变化返回409，需要刷新后重新核对。新任务固定受理时的 `miner` 配置版本与 Skill 设置版本，已受理任务不读取后来改写的默认值。

## 创建任务

`POST /api/mining/jobs` 使用以下主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 任务名，最多 120 字符 |
| `source_id` | string | 已登记、带 Skill 用途标签的知识源引用 |
| `input_dir` | string | 所选知识源的 OpenViking Resources 目录 URI |
| `max_rounds` | integer | 真实轮数上限，1–5，默认 3 |
| `human_checkpoints` | boolean | 评审契约是否每轮后等待人工复核，默认 true |
| `mining_mode` | string | `auto` 自由挖掘（默认）、`single` 单 Skill 整合、`target` 指定数量 |
| `mining_direction` | string | 最多 4000 字符；自由挖掘和指定数量时选填，单 Skill 整合时必填完整方法目标 |
| `target_skill_count` | integer/null | 指定数量时必填 1–100；单 Skill 固定为 1；自由挖掘为 null 或省略 |
| `runtime_timeout_seconds` | integer | 每轮执行保护期限，60–7200，默认3600秒；不是预计完成时间 |

整轮任务使用 `execution_protocol: "te-native-mining.v1"`，每轮正常提交一次Compile；首轮完整构建，后续轮次使用本次补证、涉及的冻结原文和变更的产物上下文，增量更新相同的 `stable_output_uri`。`max_rounds` 是上限；问题为0时结束，不强制进入下一轮。

首轮从冻结的原始来源确定并保存主要语言，中文主导时使用简体中文。后续轮次沿用该语言，不由旧英文产物或补证语言重新决定。任务语言规则由运行时加入新的 Compile 请求，面向人的正文、说明、案例 Query、Checklist 自然语言、共享分析和复核问题按该语言生成；字段名、ID、文件路径、来源 URI、代码、固定枚举、原文直接引用及 `Checklist`、`Evidence`、`Materials` 等结构标签保持原样。当前接口与控制台均无独立的输出语言选择项。

保存的自定义业务编译指令保持原样，无需为语言更新而重置。已受理的请求继续使用其冻结版本；已完成产物不自动翻译或重新 Compile。

采用多候选评审契约的任务使用 `candidate_contract_version: 2`，在一次原生Compile内部规划完整独立方法并生成配套产物。数量由任务目标与来源决定，不按文件、客户或子步骤机械拆分，不受旧宿主每批8候选限制。证据不足时保留真实处置说明，不凑数。

此前保存的自定义编译 Skill 若仍采用单候选契约，新任务保留 `candidate_contract_version: 1` 并提示兼容性警告。重置为当前默认编译 Skill 后创建的新任务才启用多候选；已有任务仍保留其冻结契约。

整轮执行见 [原生整轮编译](../../../team_miner/skill_miner/native_round.py) 与 [编译 Skill](../../../team_miner/skill_miner/native_compiler.py)。每个方法的 METHOD.json 回执汇总为已有能力拆分计划；Test Dataset 的Markdown展示由JSON确定性渲染。受理响应丢失用原幂等键对账；只有明确的产物校验失败或输出截断允许每轮最多两次整轮修订。编译附件、来源、鉴权、网络及未知故障保留结构化诊断，不作为业务指令重新发送，原请求、错误和结果保留。

暂缓提议在生成最终补证问题前回到相关原始文件分批复核；参考摘要遗漏或运行时输入尚未给定不能直接视为 Evidence 缺口。同一候选在后续批次或轮次中复用身份，语义相近的方法按有界任务复核并保留合并原因及完整来源谱系。共享来源、公式或流程中的单一步骤不自动合并；相似检索不保证穷尽所有重复。每个阶段编译器只携带当前任务的输出契约，公共业务规则与完整产物布局分别维护，已冻结请求不追溯改写。

历史分批任务继续沿用其冻结的身份、抽取与案例协议；以下分阶段说明仅适用于这些历史任务。采用 METHOD 回执契约的任务通过 METHOD.json.supersedes 显式提出替代关系，保留旧文件谱系，由模型整合业务范围。

自定义分阶段编译 Skill 使用唯一的 `te-mining-policy.v1` 类型标记的 JSON 代码块，字段为 `schema_version`、`common` 和 `stages`：`common` 保存完整公共业务规则，`stages` 按阶段名称保存额外规则。无法安全识别的自由文本不会被自动删改，而会报告迁移错误；既有非分阶段自定义编译方式保持原样。默认模板与已冻结的已知默认模板由 TE 明确识别。

新的原文复核使用最多 768 字符的连续原文片段。模型选择当前任务中准确的片段标识，TE 按原文范围和哈希还原引用；原文只出现一次，按完整 JSON 大小分批，不截短材料。已冻结的文本引用请求仍使用连续原文精确匹配；仅 Unicode 引号、空白和独立列表符号存在排版差异时，TE 必须在准确来源 URI 下唯一定位原文。数字、运算符、业务词变化或多个匹配位置仍拒绝。模型原始结果和旧请求保留；这个校验确认出处，不证明业务规则完整或推理正确。

Test Dataset 的案例生成使用现有公共 Compile 的文件级 Map 和 Finalize：`from` 提交包含最多四个独立任务文件的专属输入目录，符合公共接口只接收目录的约定；每个模型任务仍只生成一个有界案例。TE 核对目录成员，输入、来源范围和输出路径按文件隔离。即使 OV 返回完成状态，TE 仍逐案例验证准确标识、来源、完整性及重复 Query；部分成功时保留合格案例，只对缺失或不合格案例作有界修复。十二个案例全部校验通过后才组装该候选的 Dataset；已冻结的旧单案例请求继续按原请求恢复。

旧批处理若将文件 URI 用作 `from`，仅在准确的提交 Key 查询确认未受理后建立目录版请求并记录审计；原任务文件、Case IDs 与修复次数保留。旧请求不改写，已受理任务仍恢复原任务编号；网络故障或停止请求不会触发替换。

## 能力拆分与产物契约

方向与数量冻结在任务的 `mining_intent` 中，参与后续轮依赖校验。数量仅控制最终选择，不是拆分或归并依据。匹配判断合并到已有工作流元数据任务中，不额外启动 Compile；按完整方法选择后再生成正文和 Test Dataset。选择前的完整计划及排除原因留在本轮审计，`analysis/mining-intent.json` 记录选择及最终数量，结构校验不代表业务正确。达不到目标数量时强制进入人工复核，即使请求关闭人工检查点也不静默通过。

### 从已修复的失败断点恢复

不带请求正文的原生任务 `POST /api/mining/jobs/{job_id}/resume`，仅支持管理员针对已确认的“调用本编译 Skill 包内未声明附件”错误，显式升级附件指导后恢复。只有 TE 已提供不同版本的指导正文和 SHA 时才可恢复；仅改变包装标记不算新版本，也不能清空错误后原样再试。恢复前重读准确远端终态并核对同一错误事件、全部冻结输入、原请求及已验收轮次 SHA。真实来源或声明附件丢失、鉴权、网络、输出协议错误、未知语义错误和用户停止不适用此附件恢复入口。每轮同一指导正文只允许一次升级，总上限两次（包含历史资源声明恢复），原重试次数保留。首次及后续恢复均沿用本轮已有预算起点和期限，不通过恢复续时；预算已到期时拒绝恢复。新请求使用独立幂等键，原编译 Skill、`from` / `to` 和历史产物保持不变，恢复指导在首次 POST 前冻结；丢回包继续用原键对账。

TE 从冻结清单生成实际可用附件说明，明确 `record_fields` / `scope_fields` 是内联字段，不新增虚构文件，也不修改 OV。附件清单用于指导模型，不是 OV 读取工具的强制白名单；现有 Compile 接口不提供按该清单限制工具读取的选项。TE 将运行诊断与业务指令分离，依赖错误不会自动触发整轮重编译；这不能保证 OV 内部模型永不请求清单外路径。

“重新实测本轮”通过同一入口提交显式请求正文，单独授权一次格式修复实测：

```json
{
  "action": "retry_with_new_budget",
  "request_id": "653dad9a-4bfd-47ad-9a6c-ec79779b323b",
  "expected_task_id": "<当前失败的 Native 任务编号>",
  "budget_seconds": 3600
}
```

`request_id` 必须为 UUID；同一次操作重试时保持一致。`expected_task_id` 必须匹配当前失败任务，`budget_seconds` 必须等于原任务的 `runtime_timeout_seconds`（本例为 3600 秒）。服务端从已登录控制台身份记录授权管理员，不接受请求指定授权人。仅已确认的 `output_protocol` 失败、已有新的格式指导且本任务尚未使用该指导时可操作；每个 TE 任务最多授权一次，恢复前仍需核对真实远端终态、冻结请求和输入。它保留同一 TE 任务、原 `round_started_at` 及全部历史，以新 Native 任务执行整轮 Compile，不保证只重跑失败的 Map。

新增预算独立记录在 `native_explicit_retry.attempt_budget`，包括 `started_at`、`seconds` 和 `deadline_at`；`old_budget` 保留旧预算。丢回包时用原请求标识及受理幂等键对账，不重复 Compile。公开字段 `can_retry_native` 与 `native_retry_context`（`expected_task_id`、`budget_seconds`、`format_guidance_sha256`）决定是否显示按钮。界面区分包含中断等待的本轮经过时间、本次重试经过时间和本次运行期限；运行期限用于保护预算，不是预计完成时间，重新实测也不代表业务或 True Replay 通过。

重新实测还要求能够证明紧邻前轮的已验收原始文件包，且复核版本与归档一致。若上次失败已改写远端目标，TE 先完整归档部分结果，再恢复同一目标的已验收基线并回读全部 SHA。文件集合变化、出现第三种内容或无法证明基线时停止；请求已经受理时只对账，不再回滚目标。新编译指令冻结中间记录格式与原任务执行角色的一致性复核要求，不新增事实 Evidence，也不修改旧编译 Skill。

历史分批任务的 `POST /api/mining/jobs/{job_id}/resume` 允许管理员恢复已识别的“上一轮同名候选业务边界变化”控制面失败。要求当前冻结轮位于 `identity_group`、没有在途子任务且尚未完成；其它失败或已请求停止的任务返回 409。恢复保留输入、原请求、复核记录及未解决问题，失败原因记入 `recovery_history`，继续相同轮次；并发已满返回 429。此入口不新建任务或重跑已完成抽取，不提交 Skill Candidate、True Replay 或 Publish。

该入口也支持已确认的增量正文容量失败：远端已完成、同批结果均有验收记录、失败候选有已接受正文，且三次错误均为整篇输出超过 12K。恢复账目保存原状态与修复次数；后续使用独立的新编译器版本，只生成有界新增内容并插入完整旧正文。旧正文、原文、已受理请求及合格兄弟结果保持完整，旧编译器不覆盖。单次模型输出仍限 12K，本地合入结果逐字保留旧内容，校验来源编号、中文和正文链，并受本地 60K 字符上限约束。该协议转换只允许一次，不支持无限清零修复次数；其它格式、来源或业务失败仍需独立诊断。

同名提议若改变业务边界，会取得独立机器身份并记录旧值、新值和来源范围。上一轮身份不会被静默改写；正常业务归组仍不能丢弃不同的已接受元数据。

每轮生成四个共享分析文件，以及每个候选独立的四个文件：

```text
analysis/evidence-map.md
analysis/gaps.md
analysis/review.json
analysis/capability-plan.json
compiled_skill/<skill-name>/SKILL.md
compiled_skill/<skill-name>/EVALUATION.md
compiled_skill/<skill-name>/benchmark.json
compiled_skill/<skill-name>/BENCHMARK.md
```

`analysis/capability-plan.json` 固定候选名称、任务边界和来源引用；后续写入阶段遵守这份计划。产物不能包含计划外的候选，候选名称唯一，并与目录名和 `SKILL.md` frontmatter 的 `name` 一致。计划中的某个候选缺失 `SKILL.md` 或其他必需文件时，该候选标记为 `incomplete`，不阻止其他结构合格的候选进入后续流程。

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `schema_version` | string | 固定为 `skill-miner.plan.v1` |
| `skills` | object[] | 本轮候选集合的方法规划；分阶段流程不受单任务 8 个候选上限约束 |
| `deferred` | object[] | 暂不形成 Skill 的内容及原因；零候选时不能为空 |

每个 `skills` 条目包含：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 唯一的小写 kebab-case 名称 |
| `task` | string | 可独立完成的任务 |
| `trigger` | string | 适用场景 |
| `inputs` / `outputs` | string[] | 任务输入与预期输出，均不能为空 |
| `boundary` | string | 方法覆盖的范围和约束 |
| `rationale` | string | 拆分或合并该方法的理由 |
| `source_refs` | string[] | 支持该方法的原始来源或人工补证 URI，不能为空 |

历史分批流程另生成 `analysis/method-dispositions.md` 和 `analysis/dispositions/page-0001.md`、同名 JSON 等分页。它们逐项说明方法提议如何保留、合并、归为事实信息或暂缓，保留来源及原因。事实信息不自动触发人工补证。

每个 `deferred` 条目包含非空的 `reason` 和 `source_refs`。来源引用必须属于本任务知识源或补证范围，不能引用上一轮模型产物。候选间可共享 Evidence，但重叠方法应合并，单个完整流程的步骤不应被拆成无法独立使用的候选。

各候选的 `benchmark.json` 分别使用 `team-replay.dataset` 格式并包含 12–24 个案例，只引用本候选。初次产物同步时，系统从结构校验通过的 `benchmark.json` 确定性地规范化已生成的 `BENCHMARK.md`，使案例、Checklist 和来源保持一致，不增加案例或业务事实。缺失必需文件仍会阻止该候选提交，不以规范化补齐缺失产物。

每个候选有自己的 Test Dataset、True Replay、Candidate Review 和 Publish 结果；编译完成的集合不会直接成为已发布 Skill 库。零候选结果仅保留分析与暂缓原因，不能提交候选。已有任务保留单候选及三个分析文件、四个包文件的旧契约，恢复或继续时不会自动切换契约。

提交候选时，服务为文档案例设置 `replay.execution`：`kind: "document"`、`runtime_type: "document"`、`binding: "tenant_replay_adapter"`。执行器由当前租户的 ReplayAdapterFactory 配置决定，不由模型选择。原文档 URI 保留为 Evidence 引用，不作为可恢复的 Session。Baseline 与 Candidate 分别开启独立 Agent 会话；新建 Skill 的 Baseline 不加载候选，修订 Skill 的 Baseline 使用已发布版本。执行时不向 Agent 提供包中的评测定义或完整 Checklist，完成条件只通过渐进反馈披露。

候选评估响应的 `status` 和 `replay.status` 区分 `completed`、`skipped`、`not_run`、`failed`，并保留 `replay.reason` 与实际双分支案例数量 `replay.case_count`。未绑定执行器时显示跳过及原因；未观测的指标不计为零或持平。只有真实完成且通过发布门禁的结果可按回放 Publish；未完成时调用自动发布返回 HTTP 409 `replay_incomplete`，候选保持待评审，可配置执行器后重新评估。管理员显式强制发布沿用原有权限与确认流程。

## 保存补证附件

`POST /api/mining/jobs/{job_id}/supplements`：

```json
{
  "checkpoint_id": "<当前 pending_checkpoint.id>",
  "files": [
    {"name": "clarification.md", "content_base64": "IyBDbGFyaWZpY2F0aW9uCg=="}
  ]
}
```

附件内容与当前任务引用持久化到 PostgreSQL，临时目录只用于执行时重建；等待继续时冻结到下一轮输入。后台任务完成后的 `result` 包含 `ok`、`saved` 和更新后的 `job`；`saved` 列出保存的名称和引用。此步骤不会直接修改 OpenViking 原始知识源或已生成产物。

单次上传 1–10 个文件，每轮最多保存 20 个附件；原文件每个最多 10 MB，单次总计最多 20 MB。转换为 Markdown 后，本轮附件总计最多 8 MB。

## 继续或结束

`POST /api/mining/jobs/{job_id}/continue` 和 `/finish` 接收相同的复核字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `checkpoint_id` | string | 必须与当前 `pending_checkpoint.id` 一致 |
| `answers` | object | 问题 `qid` 到回答文本的映射 |
| `unresolved` | string[] | 明确标记暂时无法解决的问题 `qid` |
| `references` | object | 问题 `qid` 到 OpenViking 来源文件 URI 数组的映射，每题最多 20 个 |
| `feedback` | string | 对本轮产物的复核意见，最多 20000 字符 |
| `request_id` | string | 客户端请求标识；重试同一操作时保持一致 |

必答问题需要有回答或明确列入 `unresolved`；同一问题不能同时回答并标记未解决。回答与复核意见总计最多 100000 字符。来源引用必须是可读的 Resources 文本文件，每个最多 2 MB；上一轮产物和修订上下文不能作为 Evidence 来源。未知问题、过期复核标识和重复操作中的参数冲突会被拒绝。

示例使用已经登录的控制台 Cookie 文件：

```bash
curl -X POST 'http://localhost:52010/api/mining/jobs/<job_id>/continue' \
  -b '<console-cookie-file>' \
  -H 'Content-Type: application/json' \
  -d '{
    "checkpoint_id": "<checkpoint-id>",
    "answers": {"q1": "按来源中的计费规则执行。"},
    "unresolved": [],
    "references": {"q1": ["viking://resources/team/source/rules.md"]},
    "feedback": "修正该规则在 Skill Candidate 和 Test Dataset 中的描述。",
    "request_id": "review-round-1-continue"
  }'
```

`continue` 冻结补证后提交下一轮；达到 `max_rounds` 后禁止继续。`finish` 结束当前挖掘，保留复核记录和未解决问题，不自动重新 Compile 或 Publish。

## 响应状态与恢复

任务详情包含当前状态、真实轮次 `current_round`、轮次列表 `rounds`、当前 `pending_checkpoint`、产物以及质量信息。新任务还记录 `candidate_contract_version`；多候选产物同步后提供以下字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `execution_protocol` | string | 整轮执行时为 `te-native-mining.v1` |
| `stable_output_uri` | string | 同任务各轮保持不变的远端产物目录 |
| `native_compile_history` | array | 实际受理的远端任务与轮次；不等于内部模型调用次数 |
| `native_repairs` | array | 有界整轮修订的错误与原任务记录 |
| `native_failure` | object | 失败类别、原始诊断、指纹及持久化审计归档 |
| `native_resource_recovery` | object | 历史资源声明恢复回执，保留并计入升级上限 |
| `native_contract_recoveries` | array | 未声明附件错误的显式指导升级记录：版本 SHA、轮次、原任务和幂等键 |
| `can_retry_native` / `native_retry_context` | boolean / object | 是否可显式实测，以及当前失败任务编号、原任务预算和新格式指导 SHA |
| `native_explicit_retry` | object | 一次人工格式重试的授权摘要、独立 `attempt_budget` 和原 `old_budget` |
| `capability_plan` | object | 已校验的能力拆分计划 |
| `candidate_count` | integer | 本轮候选数量 |
| `valid_candidate_count` | integer | 通过各自包与 Test Dataset 结构校验的候选数量 |
| `candidate_quality` | object | 以候选名称为键的质量结果；包含 `level`、`warnings` 和 `can_submit` |

`candidate_quality` 的 `ready` / `incomplete` 与 `can_submit` 反映候选结构是否合格；任务仍需完成当前人工复核或执行轮次，之后才能提交合格候选。结构合格不代表已通过 True Replay 或 Candidate Review。

`pending_checkpoint` 包含 `id`、`round`、`questions` 和 `attachments`。每个问题包含 `qid`、`question`、`reason`、`source_refs` 和 `required`。

`remote_warnings` 保留 OpenViking 的部分产物和来源处理失败警告，并合入 `artifact_quality.warnings`；存在警告时质量为 `caution`（需复核）。各轮记录保留自身的警告和 `compiler_sha256`。远端 `completed` 及所有必需产物格式通过，不代表 Evidence 覆盖完整。

`compiler_source_sha256` 标识冻结的编译器来源正文，`compiler_sha256` 标识经过包名替换和原生格式规范化后实际安装的正文。`compiler_uri` 与 `skill_uri` 指向该正文对应的独立包；轮次归档保留这些字段。已存在包的正文不一致时拒绝执行，不将它覆盖为另一版本。

| 状态 | 含义 |
| --- | --- |
| `preparing` | 准备或恢复本轮输入与提交 |
| `queued` | 本轮 Compile 已受理，等待执行 |
| `running` | 正在执行、同步或物化本轮产物 |
| `waiting_human` | 本轮已完成，等待人工补证或最终复核 |
| `stopping` | 已请求取消，等待远端终态 |
| `succeeded` | 挖掘已结束，最终产物可进入后续验证 |
| `failed` / `stopped` / `interrupted` | 失败、停止或中断，查看任务详情 |

每轮有自己的运行期限；`waiting_human` 不消耗期限或执行名额。受理未知时，同一轮使用原始幂等键和原始请求恢复；新的轮次使用新的键。服务重启不增加轮数，不重复提交已经受理的任务。

## 错误处理

受理前的错误使用 HTTP 状态码和 `detail` 描述。已返回202的长操作通过 `status_url` 返回终态 `failed` 与 `error_code`；下表中的执行期错误通过该后台任务结果查看。典型情况如下：

| 状态码 | 情况 | 处理 |
| --- | --- | --- |
| 400 | 创建任务或附件请求格式无效 | 修正请求 |
| 401 / 403 | 未登录或无当前挖掘管理权限 | 使用有权操作的控制台身份 |
| 404 | 任务或来源不存在 | 重新查询有效引用 |
| 409 | 不在人工等待状态、复核标识过期、未知问题、必答问题未处理、轮数达到上限或请求冲突 | 刷新任务详情并修正回答，或明确标记未解决 |
| 413 | 附件超出允许大小 | 缩减上传内容 |
| 429 | 同时执行的挖掘任务已达到 8 个 | 等待已有任务结束后重试 |
| 502 | OpenViking 来源读取暂不可用 | 查询已有任务状态，恢复连接后重试 |

任务创建和继续请求会先保存 PostgreSQL 状态，远端输入准备与 Compile 提交在后台执行。后台错误通过任务的 `error`、`submission_error` 或状态同步信息查看；收到成功响应不代表远端 Compile 已开始执行。

统一 Test Dataset 结构检查不等于业务事实校验。即使格式合规，Evidence 缺口仍需人工复核；最终产物仍须通过 True Replay 与 Candidate Review。

分阶段执行契约在提交后保存 `round_work_units`（预计输入分片与产物生成工作量）和 `round_timeout_seconds`（本轮实际执行期限）。每个阶段任务按基础期限的八分之一、至少 180 秒预留时间，总期限至少为基础期限。能力规划确定后计入文件和案例生成任务；同一轮期限只增不减，起始时间不变，下一轮重新估算。重试已接受的相同请求不会重复计入工作量。已冻结的旧请求不追溯修改。原始来源逐文件持久化，最多 10000 个文件、每文件 20 MB，不受旧的 16 MB 总量上限限制。

来源通过现有 OV 文件列表接口逐目录分页遍历，不因固定深度或树节点上限遗漏文档。服务端返回的截断、权限错误、重复分页或范围外条目不会被当成完整来源。Skill 正文逐段校验、原子写入，最终身份校验与归档哈希分块读取；正文没有组装阶段的截短操作。

## 提交独立 Skill Candidate

`POST /api/mined-jobs/{job_id}/skills/{skill_name}/submit` 只接收可选的 `artifact_path`，它必须指向该任务当前工作区中的对应 `SKILL.md`。接口受理为202后台命令；`result` 返回 `created`、候选 `job_id`、`skill_name`、`candidate_revision` 和 `question_count`。

Worker 从 PostgreSQL 读取完整包与能力计划，并重新校验当前独立候选；另一个候选失败不阻止完整候选提交。完整包包含 `SKILL.md`、`EVALUATION.md`、`benchmark.json`、`BENCHMARK.md` 以及实际返回的 `METHOD.json`、脚本和引用文件。单独存在入口文件或完成回执不能替代该校验。

同一原始包内容与执行绑定版本在同一租户内重复提交时返回同一候选，不增加版本或重置已进行的评审。相同名称存在另一个待评审候选时，已受理的后台任务进入 `failed` 并返回冲突 `error_code`，不覆盖人工编辑或进行中的验证。候选与文件写入由 Worker fence、任务快照版本和 PostgreSQL 事务锁保护。旧案例中的内联材料只在候选包副本中转换为 `references/mining-materials/<case_id>/<sha256>.txt` 真实文件；规范化 Test Dataset 引用该文件，Replay 两个分支获得相同且校验过的材料内容。转换遵守主平台包大小限制，路径冲突或缺失内容会拒绝提交，原始挖掘包与来源哈希保持不变。接口只创建 `open` 候选，不启动 True Replay 或 Publish。

Test Dataset 中的来源 Viking URI 保留；Compiler 提供的 Session 身份、运行时、Adapter 和私有 Context 快照不会进入执行输入。TE 通过可信的 `document` 运行类型绑定租户已配置的 Replay Adapter，实际评审时仍由平台冻结配置与 Adapter 版本。

用于这些文档案例的租户 Replay Adapter 必须支持 `document` 运行类型与没有来源 Session 的独立执行。仅支持特定来源运行类型的 Adapter 会返回不支持；不能伪造来源 Session、私有 Context 或改选其他 Adapter 来通过验证。适配器兼容性必须单独实测，固定本地测试 Factory 只能验证接口连通性。
