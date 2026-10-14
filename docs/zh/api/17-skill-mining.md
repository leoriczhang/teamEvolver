# Skill 挖掘 API

这些控制台内部接口管理 OpenViking Compile 的真实挖掘轮次与人工补证。需要有效控制台 Session Cookie，并沿用当前挖掘管理权限；租户机器凭证不能调用这些管理接口。Agent 数据面协议不变。

执行和状态同步位于 [主服务桥接](../../../team_miner/bridge.py)，本地任务、轮次和补证记录位于 [任务仓库](../../../team_miner/ov_compile_mining.py)。操作流程见 [多轮 Skill 挖掘](../guides/17-multi-round-skill-mining.md)。

## 接口

| 方法 | 路径 | 用途 |
| --- | --- | --- |
| POST | `/api/mining/jobs` | 创建任务并提交第一轮 Compile |
| GET | `/api/mining/jobs` | 查询任务列表和摘要 |
| GET | `/api/mining/jobs/{job_id}` | 查询轮次、产物、日志和人工复核状态 |
| POST | `/api/mining/jobs/{job_id}/supplements` | 在当前人工复核阶段保存附件 |
| POST | `/api/mining/jobs/{job_id}/continue` | 保存补证并启动下一轮 |
| POST | `/api/mining/jobs/{job_id}/finish` | 保存最终复核并结束挖掘 |

## 创建任务

`POST /api/mining/jobs` 使用以下主要字段：

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `name` | string | 任务名，最多 120 字符 |
| `source_id` | string | 已登记、带 Skill 用途标签的知识源引用 |
| `input_dir` | string | 所选知识源的 OpenViking Resources 目录 URI |
| `max_rounds` | integer | 真实轮数上限，1–5，默认 3 |
| `human_checkpoints` | boolean | 是否每轮后等待人工复核，默认 true |
| `runtime_timeout_seconds` | integer | 每轮基础执行期限，60–7200，默认 3600 秒；默认编译器按输入分片和产物工作量延长实际期限 |

第一轮从原始知识源冻结快照。后续轮次使用原始快照、累计人工补证和上一轮修订上下文。任务返回 `rounds_mode: "orchestrated"`；`current_round` 表示真实轮次。

首轮从冻结的原始来源确定并保存主要语言，中文主导时使用简体中文。后续轮次沿用该语言，不由旧英文产物或补证语言重新决定。任务语言规则由运行时加入新的 Compile 请求，面向人的正文、说明、案例 Query、Checklist 自然语言、共享分析和复核问题按该语言生成；字段名、ID、文件路径、来源 URI、代码、固定枚举、原文直接引用及 `Checklist`、`Evidence`、`Materials` 等结构标签保持原样。当前接口与控制台均无独立的输出语言选择项。

保存的自定义业务编译指令保持原样，无需为语言更新而重置。已受理的请求继续使用其冻结版本；已完成产物不自动翻译或重新 Compile。

使用默认编译 Skill 的新任务采用 `candidate_contract_version: 2`：模型先规划可独立执行的任务方法，再为每个方法编译独立的 Skill Candidate。数量由触发场景、输入输出、任务边界以及独立执行与 True Replay 验证能力决定，不按文档篇数或章节拆分。每个有界规划任务最多输出 8 个候选，整轮通过更多任务处理更多有效方法，不要求模型填满数量；无法支持可执行方法时允许 0 个候选，并记录暂缓原因。纯事实及 Evidence 不足的内容保留不形成候选的原因；只有真实缺口才要求人工补证。

此前保存的自定义编译 Skill 若仍采用单候选契约，新任务保留 `candidate_contract_version: 1` 并提示兼容性警告。重置为当前默认编译 Skill 后创建的新任务才启用多候选；已有任务仍保留其冻结契约。

默认流程由 TE 分阶段提交现有公共 Compile。来源抽取及产物生成使用本轮原始 Evidence 的有界文本片段；规划阶段使用有逐项处置记录的可追溯方法账目，不将摘要当作原始事实。上轮候选集合的修订上下文通过 `instruction` 直接提供，不作为来源参与提取。默认分阶段流程保留本地候选修订账目，每次任务只提供与当前范围或候选名称匹配且能容纳于任务预算的上下文；节选或省略有明确标记。自定义单次 Compile 仍使用最多 30000 字符的集合修订上下文。完整上轮产物始终保留在轮次归档中。

暂缓提议在生成最终补证问题前回到相关原始文件分批复核；参考摘要遗漏或运行时输入尚未给定不能直接视为 Evidence 缺口。同一候选在后续批次或轮次中复用身份，语义相近的方法按有界任务复核并保留合并原因及完整来源谱系。共享来源、公式或流程中的单一步骤不自动合并；相似检索不保证穷尽所有重复。每个阶段编译器只携带当前任务的输出契约，公共业务规则与完整产物布局分别维护，已冻结请求不追溯改写。

新冻结的后续轮次区分身份复用与业务字段修订。普通复用保持原字段；显式修订必须提供准确的候选身份、旧字段哈希、当前原始方法范围内的提议及修订理由。TE 保留名称与已有 Case IDs，重新生成正文、评测说明和案例，并保存旧值、新值及来源账目；后续规划与中断恢复使用已接受的业务版本。不能隐式改写同名方法或用另一来源的方法替换原身份。原文范围校验不证明两段描述在语义上属于同一工作流，产物仍需业务复核；旧冻结请求继续按原协议处理。

自定义分阶段编译 Skill 使用唯一的 `te-mining-policy.v1` 类型标记的 JSON 代码块，字段为 `schema_version`、`common` 和 `stages`：`common` 保存完整公共业务规则，`stages` 按阶段名称保存额外规则。无法安全识别的自由文本不会被自动删改，而会报告迁移错误；既有非分阶段自定义编译方式保持原样。默认模板与已冻结的已知默认模板由 TE 明确识别。

新的原文复核使用最多 768 字符的连续原文片段。模型选择当前任务中准确的片段标识，TE 按原文范围和哈希还原引用；原文只出现一次，按完整 JSON 大小分批，不截短材料。已冻结的文本引用请求仍使用连续原文精确匹配；仅 Unicode 引号、空白和独立列表符号存在排版差异时，TE 必须在准确来源 URI 下唯一定位原文。数字、运算符、业务词变化或多个匹配位置仍拒绝。模型原始结果和旧请求保留；这个校验确认出处，不证明业务规则完整或推理正确。

Test Dataset 的案例生成使用现有公共 Compile 的文件级 Map 和 Finalize：`from` 提交包含最多四个独立任务文件的专属输入目录，符合公共接口只接收目录的约定；每个模型任务仍只生成一个有界案例。TE 核对目录成员，输入、来源范围和输出路径按文件隔离。即使 OV 返回完成状态，TE 仍逐案例验证准确标识、来源、完整性及重复 Query；部分成功时保留合格案例，只对缺失或不合格案例作有界修复。十二个案例全部校验通过后才组装该候选的 Dataset；已冻结的旧单案例请求继续按原请求恢复。

旧批处理若将文件 URI 用作 `from`，仅在准确的提交 Key 查询确认未受理后建立目录版请求并记录审计；原任务文件、Case IDs 与修复次数保留。旧请求不改写，已受理任务仍恢复原任务编号；网络故障或停止请求不会触发替换。

## 能力拆分与产物契约

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
| `skills` | object[] | 本轮候选集合的方法规划；默认分批流程不受单任务 8 个候选上限约束 |
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

默认分批流程另生成 `analysis/method-dispositions.md` 和 `analysis/dispositions/page-0001.md`、同名 JSON 等分页。它们逐项说明方法提议如何保留、合并、归为事实信息或暂缓，保留来源及原因。事实信息不自动触发人工补证。

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

附件保存到当前任务本地目录，等待继续时冻结到下一轮输入。响应包含 `ok`、`saved` 和更新后的 `job`；`saved` 列出保存的名称和引用。此步骤不会直接修改 OpenViking 原始知识源或已生成产物。

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

错误使用 HTTP 状态码和 `detail` 描述。典型情况如下：

| 状态码 | 情况 | 处理 |
| --- | --- | --- |
| 400 | 创建任务或附件请求格式无效 | 修正请求 |
| 401 / 403 | 未登录或无当前挖掘管理权限 | 使用有权操作的控制台身份 |
| 404 | 任务或来源不存在 | 重新查询有效引用 |
| 409 | 不在人工等待状态、复核标识过期、未知问题、必答问题未处理、轮数达到上限或请求冲突 | 刷新任务详情并修正回答，或明确标记未解决 |
| 413 | 附件超出允许大小 | 缩减上传内容 |
| 429 | 同时执行的挖掘任务已达到 8 个 | 等待已有任务结束后重试 |
| 502 | OpenViking 来源读取暂不可用 | 查询已有任务状态，恢复连接后重试 |

任务创建和继续请求会先保存本地状态，远端输入准备与 Compile 提交在后台执行。后台错误通过任务的 `error`、`submission_error` 或状态同步信息查看；收到成功响应不代表远端 Compile 已开始执行。

统一 Test Dataset 结构检查不等于业务事实校验。即使格式合规，Evidence 缺口仍需人工复核；最终产物仍须通过 True Replay 与 Candidate Review。

新默认契约在提交后保存 `round_work_units`（预计输入分片与产物生成工作量）和 `round_timeout_seconds`（本轮实际执行期限）。每个阶段任务按基础期限的八分之一、至少 180 秒预留时间，总期限至少为基础期限。能力规划确定后计入文件和案例生成任务；同一轮期限只增不减，起始时间不变，下一轮重新估算。重试已接受的相同请求不会重复计入工作量。已冻结的旧请求不追溯修改。原始来源逐文件持久化，最多 10000 个文件、每文件 20 MB，不受旧的 16 MB 总量上限限制。

来源通过现有 OV 文件列表接口逐目录分页遍历，不因固定深度或树节点上限遗漏文档。服务端返回的截断、权限错误、重复分页或范围外条目不会被当成完整来源。Skill 正文逐段校验、原子写入，最终身份校验与归档哈希分块读取；正文没有组装阶段的截短操作。
