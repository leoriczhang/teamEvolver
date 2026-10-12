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
| `runtime_timeout_seconds` | integer | 每轮执行期限，60–7200，默认 3600 秒 |

第一轮从原始知识源冻结快照。后续轮次使用原始快照、累计人工补证和上一轮修订上下文。任务返回 `rounds_mode: "orchestrated"`；`current_round` 表示真实轮次。

首轮从冻结的原始来源确定并保存主要语言，中文主导时使用简体中文。后续轮次沿用该语言，不由旧英文产物或补证语言重新决定。任务语言规则由运行时加入新的 Compile 请求，面向人的正文、说明、案例 Query、Checklist 自然语言、共享分析和复核问题按该语言生成；字段名、ID、文件路径、来源 URI、代码、固定枚举、原文直接引用及 `Checklist`、`Evidence`、`Materials` 等结构标签保持原样。当前接口与控制台均无独立的输出语言选择项。

保存的自定义业务编译指令保持原样，无需为语言更新而重置。已受理的请求继续使用其冻结版本；已完成产物不自动翻译或重新 Compile。

使用默认编译 Skill 的新任务采用 `candidate_contract_version: 2`：模型先规划可独立执行的任务方法，再为每个方法编译独立的 Skill Candidate。数量由触发场景、输入输出、任务边界以及独立执行与 True Replay 验证能力决定，不按文档篇数或章节拆分。每轮最多 8 个候选，这是执行上限，不是要求模型填满的数量；无法支持可执行方法时允许 0 个候选，并记录暂缓原因。纯事实及 Evidence 不足的内容进入 `deferred`。

此前保存的自定义编译 Skill 若仍采用单候选契约，新任务保留 `candidate_contract_version: 1` 并提示兼容性警告。重置为当前默认编译 Skill 后创建的新任务才启用多候选；已有任务仍保留其冻结契约。

远端 Compile 请求的 `from` 仅包含本轮 Evidence 来源目录。上轮候选集合的修订上下文通过 `instruction` 直接提供，不作为来源参与提取。修订上下文 JSON 上限为 30000 字符，包含上轮各 Skill 正文、案例标识和本轮复核信息；节选或省略有明确标记。完整上轮产物始终保留在轮次归档中。

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
| `skills` | object[] | 0–8 个候选的方法规划 |
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
