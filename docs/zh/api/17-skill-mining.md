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

远端 Compile 请求的 `from` 仅包含本轮 Evidence 来源目录。上轮候选的修订上下文通过 `instruction` 直接提供，不作为来源参与提取。修订上下文 JSON 上限为 30000 字符，包含上轮 Skill 正文、案例标识和本轮复核信息；节选或省略有明确标记。完整上轮产物始终保留在轮次归档中。

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

任务详情包含当前状态、真实轮次 `current_round`、轮次列表 `rounds`、当前 `pending_checkpoint`、产物以及质量信息。`pending_checkpoint` 包含 `id`、`round`、`questions` 和 `attachments`。每个问题包含 `qid`、`question`、`reason`、`source_refs` 和 `required`。

`remote_warnings` 保留 OpenViking 的部分产物和来源处理失败警告，并合入 `artifact_quality.warnings`；存在警告时质量为 `caution`（需复核）。各轮记录保留自身的警告和 `compiler_sha256`。远端 `completed` 及七个产物格式通过，不代表 Evidence 覆盖完整。

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
