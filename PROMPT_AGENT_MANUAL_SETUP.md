# 业务执行 Agent 手动接入细则

这份细则对应 [Prompt 进化操作 README](PROMPT_EVOLUTION_README.md) 第 5、7、8 节。从一条真实业务请求开始，依次登记执行基线、核对 Agent 上报、拉取并检查 Session 匹配。

同一个 Langfuse 项目接入多个业务 Agent 时，每个 Agent 分别准备一条真实请求、登记各自的执行配置。数据源可以共用，同窗拉取后再逐一核对会话归属。[双 Agent 接入与验收示例](PROMPT_EVOLUTION_README.md#14-同一-langfuse-项目接入多个业务-agent)

## 0. 先找齐一条真实请求的资料

请 Agent 开发者在测试会话中发一条业务问题，记下这条请求的 **Trace ID**。同时打开业务 Agent 的部署配置、对应的 Langfuse Trace 和 TE 的目标租户。

| 向谁取 | 在哪里看 | 需要拿到什么 |
| --- | --- | --- |
| Agent 开发者 | 部署配置、对话路由、Prompt 仓库，或运行时的模板加载记录 | 真实 Agent ID、执行主体 ID、模板原文、消息角色、变量默认值和本次渲染前的实际取值 |
| Agent 开发者 | Langfuse 中这条 Trace 的模型 GENERATION。字段不全时查调用模型前最后一层 SDK、网关或传输代码 | 完整有序的模型消息、实际 model、采样参数、输出限制和工具定义，以及 Trace 名称、上游 Session ID、Trace ID、userId、时间戳 |
| TE 运维或管理员 | TE 的「数据源接入」和「运行状态 → Replay 运行环境」 | 两个已绑定的 Adapter 文件及版本、Worker 可达的服务地址、Replay Adapter 支持的 Runtime 和 Agent 身份，以及所需的凭证引用 |

如果 Langfuse 只记录了渲染后的 System 文本，回到 Agent 的模板配置取含占位符的原文，在模板渲染处取实参。原始请求缺字段时先补采一条新请求。

「执行用户 ID」取自 Agent 加载执行组合的身份。Langfuse 的 `userId` 是业务会话的用户标识，用于筛选和来源归属。两者是否相同取决于业务的身份映射。TE 控制台登录名用于控制台操作。

## 5. 手填业务执行基线

以目标租户管理员身份打开「进化链路 → Skills 自进化 → 登记现有业务 Agent 执行基线」。

| 界面字段 | 值从哪里获取 | 填法与核对 |
| --- | --- | --- |
| 执行配置 ID | TE 管理员为此基线命名 | 自定唯一稳定 ID，例如 `support-refund-base`。先在下拉框检查是否重名。界面最多 160 字符，以字母或数字开头，后面可用字母、数字、点、冒号、下划线或短横线。派生上下文后，后续选用新的配置 ID。 |
| Runtime 类型 | 「运行状态 → Replay 运行环境」当前绑定的 Adapter 版本，从它的配置、`open` 方法或维护人处确认 | 填 Adapter 实际识别的标识。界面预填 `agentshub`。仓库的 Agent Lab 示例要求 `agentscope-java`。 |
| 业务 Agent ID | Agent 服务的部署清单、注册记录或对话路由 | 填 Adapter 识别的机器 ID，区分大小写。例如 Agent Lab 示例把 `agent-one` 写入会话地址，另一个允许值为 `agent-two`。自定义 Adapter 按当前绑定版本的映射填写。 |
| 执行用户 ID | Agent 配置的执行主体，或 Agent 客户端调用 `GET /sync/skills` 时使用的 `user_id` | 填同一个稳定标识。在 Agent 侧还未设置时，请开发者与管理员先确认主体并写入客户端配置。 |
| 全部受影响的 Skill 名称 | Agent 当前加载的团队 Skill 依赖、TE 目标租户的 Skill 库 | 填真实名称，逗号或换行分隔。纯 Prompt／上下文场景留空。预览核对依赖数量，登记时冻结同租户的版本。 |
| 业务 Prompt 槽位 | 模型 GENERATION 中对应消息的 `role` 和 Agent 装配代码 | 从 `system`、`developer` 中选择。此表单登记一条主 Prompt。多条独立模板按[执行组合协议](docs/zh/concepts/12-execution-evolution.md)逐组件登记，保持角色和顺序。 |
| 当前业务 Agent Prompt | Agent 实际加载的模板版本，通常在部署配置、Prompt 仓库或 Prompt 管理服务中 | 粘贴渲染前原文，保留换行和 `{{变量名}}`。核对该模板能还原第 0 节请求里相应的 System／Developer 消息。 |
| 每个「原始默认值」 | 模板渲染器的默认值、配置回退值或调用代码的默认分支 | 每个识别出的变量都要有值。实际为空串时点击「为……明确使用空值」。有数值或布尔类型的默认值可在 JSON 的 `template_variables` 中提供，界面同名文本输入优先。单次请求实际值另随 Trace 上报。 |
| 当前受管理的业务上下文注入 | Agent 装配时独立附加的稳定规则或示例消息 | 这个框会登记独立的 `developer` 消息，按组件顺序注入。对照实际角色、位置、正文填写。若规则已由 `{{policyText}}` 等变量注入，此处留空，取值按[原 README 第 6 节](PROMPT_EVOLUTION_README.md#6-界面第三步登记可进化的上下文变量)登记。 |
| 业务 Agent 模型 | 最终模型请求的 `model`，或实际映射到它的运行配置 | 填服务实际收到的模型 ID。界面有值时会覆盖 JSON 中的 `request.model`。仅登记基线时可空，True Replay 时须满足所选 Adapter 的运行要求。 |
| 冻结执行环境 JSON | 实际模型请求、Replay Adapter 契约和业务验收方案 | 按下表取值。界面「查看执行环境 JSON 示例」提供结构。 |

| JSON 字段 | 值从哪里获取 | 填法与核对 |
| --- | --- | --- |
| `request` | 最后一层发给模型的 HTTP JSON 请求体。Adapter 有专用投影时，按其契约核对 | 填去掉 `messages` 后的非消息字段，包括实际发送的 `temperature`、`top_p`、`max_tokens`、`response_format` 等。保留数字、布尔和数组类型。True Replay 会核对冻结字段与实际传输是否一致。 |
| `request.tools` | 最终模型请求中实际发送的工具定义 | 请求有工具时保留实际定义和顺序。请求显式发送空数组时填 `[]`，原请求省略时保持省略。业务 Runtime 与 Adapter 负责工具执行。 |
| `message_prefixes` | Runtime 在传输前追加说话人前缀的代码及实际消息 | Adapter 需要时填真实的 `user`、`assistant` 前缀，保留全角冒号和空格。它位于 `request` 之外。 |
| `template_variables` | 模板实际默认值的定义 | 文本框会组装默认值和模板输入契约，有类型要求的默认值在此对象填写。同名文本框值优先。每题实参仍由 Session 提供。 |
| `validation` | 验收负责人提供的测试计划和预算 | 常规验证的 `strategy` 默认为 `standard-v1`。按 `enhanced-v1` 做独立验收时，从同租户锁定的 Test Dataset 取实际 ID，填 `holdout_dataset_ids`。额外验证的默认值为 `max_probe_cases=4`、`max_extra_runs=5`、`repetitions=2`、`max_total_tokens=1000000`。这些是 TE 的策略参数。 |

完整 `messages` 保存在原始 GENERATION 输入。冻结环境、Prompt 和 Trace metadata 都只填业务字段，凭证由 Worker 进程环境或运维发放的 Secret Ref 提供。

**按真实业务 Runtime 确认 Adapter 的地址和凭证。**

- **完整业务 Agent**使用能创建独立 Replay Session、加载冻结执行计划、观察最终请求并清理会话的 Adapter。从服务部署配置取 Worker 可达的业务服务地址，从运维取该用途的 Secret Ref。仓库 [Agent Lab 示例](team_replay/adapters/examples/agent_lab.py) 读取 `replay.agent_lab.endpoint` 和 `replay.agent_lab.secret_ref`。地址是 Agent Lab 服务的 base URL。当前仓库示例仅放行 `agent-one`、`agent-two` 的纯 Prompt 执行计划，接 `task-judge` 时选择实际支持该 ID 的 Adapter 版本。Agent Lab 模拟客户选择 `system`，按普通会话配置取模型和输出限制，使用 `stream: true`、`stream_options.include_usage: true` 及实际 `HWP：`／`C：` 前缀，省略 `tools`。控制台「Replay 运行环境」用来选择已登记的文件和版本。
- **直接模型请求**可以使用 [Observed HTTP 参考 Adapter](team_replay/adapters/examples/observed_http.py)。`endpoint` 是模型网关的完整请求 URL，从模型网关部署配置取得。Runtime 应与 Adapter 配置一致，默认是 `observed_http`。由模型网关运维在 Worker 配置鉴权，例如 `auth_profile` 为 `BUSINESS` 时读取 `TEAMEVOLVER_AGENT_BUSINESS_REPLAY_API_KEY`。参考实现需要 `environment.request` 并解析非流式 JSON。带工具、检索或业务插件的完整 Agent 使用前一种方式。

对只有一条系统模板、没有工具的售后 Agent，假设模型网关实际收到的非消息字段只有 `model`、`temperature`，可按以下结构填写。把模型 ID 和温度换成实际请求里的值。如果实际请求还发送了输出上限或其他字段，一并填入 `request`。

```json
{
  "request": {"model": "replace-with-actual-model-id", "temperature": 0},
  "validation": {"strategy": "standard-v1"}
}
```

点击「预览业务执行基线」，核对预览卡中的 ID、组件数和 Skill 依赖数，在上方表单逐项核对变量。再点击「登记业务执行基线」。需要受管理变量时按原 README 第 6 节建立派生配置，再选中最终配置复制接入信息。

实现参考见[控制台表单](web-ui/src/features/skills/ExecutionProfilePanel.tsx)、[登记校验](team_skills/execution/contracts.py)、[验证策略](team_skills/execution/holdout.py)和 [Replay 接入契约](docs/zh/api/05-replay-branch.md)。

## 7. 逐项核对 Agent 上报到 Langfuse 的字段

打开「进化链路 → 选中最终执行配置 → 接入真实 Agent 的会话 → 复制接入元数据」。操作者可用下表整理字段和值，实际写入由业务 Agent 在产生该次请求时完成。先在测试环境产生一个新请求，再回到 Langfuse 核对。

| 写入位置或字段 | 值从哪里获取 | 填法与核对 |
| --- | --- | --- |
| Trace 的 `name` | Langfuse 目标项目中真实业务 Trace 的名称 | 与数据源 Adapter 传给 `build_langfuse_adapter` 的名称一致。Trace 名和 Agent ID 分别取值。 |
| Trace 的 `sessionId`、`id`、`userId` | Agent 会话路由的对话 ID、Langfuse Trace ID 和业务用户 ID | 同一对话沿用 `sessionId`，每次新交互使用新 Trace ID。筛选用户时复制实际业务用户 ID。 |
| `metadata.teamevolver_execution.schema_version` | 「复制接入元数据」 | 固定为 `teamevolver.execution-observation.v1`。把复制对象合入 Trace 的 `metadata` 顶层，保留一层 `teamevolver_execution`。 |
| `execution_profile_id`、`agent_id`、`runtime_type` | 该请求实际使用的配置、Agent 和 Runtime，与第 5 节对应 | 三项都保留。派生上下文配置投用后填写实际配置。如果 metadata 还保留旧版同名字段，两处值须一致。 |
| `release_id`、`composition_hash` | Agent 当时加载的发布记录。初始基线可在「查看发布与 Agent 回执」核对；拉取后从响应的 `execution` 读取 | 上报实际加载版本。组合哈希为 64 位小写十六进制。尚未取得时，协议允许省略这两个字段。 |
| `template_inputs.schema_version`、`template_refs` | 共享模板配置的接入元数据，或实际加载的 `execution.document.execution_plan.components` | schema 为 `teamevolver.execution-inputs.v1`。以每个业务 Prompt 的组件 ID 为键，引用包含 `template_id`、`revision`、`content_hash`。换到派生配置时重新取引用。固定 Prompt 未声明模板契约时，复制信息可省略整个 `template_inputs`。 |
| `template_inputs.template_variables` | 本次模板渲染实参及实际应用的默认值 | 提供声明的全部变量，保留精确取值和类型。支持字符串、有限数字与布尔。若渲染器拿到序列化 JSON 字符串，就上报真正注入的字符串。托管变量这里填用来选中取值成员的原值。 |
| `template_inputs.input_messages` | 最终请求中独立传入的公开历史 | 按实际顺序填写，每条只含 `role`、`content`，支持 `user`／`assistant` 文本。历史已并入模板变量或完整用户提示词时，独立历史可填 `[]`。本轮输入由 Trace input 提供，来源绑定 `source_ref` 由 TE 生成。 |
| Trace 的 `input`、`output` | 本轮用户输入与业务 Agent 真实回复 | 记录当前问题和实际回复。业务 Agent 已拼装完整用户提示词时，保留实际使用的那条用户输入。 |
| 对应 GENERATION 的 `input` | 调用模型时的最终请求 | 保存完整有序的 `messages`。TE 用登记组件、实际变量、独立历史及当前输入还原消息，逐条核对观察到的请求。保留角色、顺序、换行和前缀。 |

**例子。** 基线是 `你是售后助手。规则：{{policyText}}`，本次实际 `policyText` 为「先核验订单状态，再说明退款路径。」，本次无独立历史。保留 TE 复制的模板引用，Agent 将实际值写入 `template_inputs.template_variables.policyText`，将 `input_messages` 写为 `[]`。在 Langfuse 分两处核对。

Trace 的 **Input** 可以记录以下结构。

```json
{"messages": [{"role": "user", "content": "我想申请退货"}]}
```

同一 Trace 的**模型 GENERATION Input**中应能找到下面的有序消息。

```json
{
  "messages": [
    {"role": "system", "content": "你是售后助手。规则：先核验订单状态，再说明退款路径。"},
    {"role": "user", "content": "我想申请退货"}
  ]
}
```

有说话人前缀时，用提取后的本轮输入和 Runtime 的前缀核对 GENERATION 中的文本。README 示例的 Langfuse 映射可能清理当前输入的首尾空白。遇到这类不匹配，先对照 Adapter 转换后的输入与实际 GENERATION，再让开发者按业务传输规则调整映射。历史 Trace 的输入缺失时，从新请求验证真实上报。

**读取实际加载的组合。** Agent 使用目标租户 Key，并带第 5 节确认的执行用户和配置 ID。下面是查询当前组合的命令，变量由操作者从获授权的部署设置取得。`TE_URL` 来自 TE API 地址，租户 Key 由运维提供，其他两项来自第 5 节。

```bash
curl --fail-with-body --silent --show-error --get "$TE_URL/sync/skills" \
  -H "Authorization: Bearer $TE_TENANT_KEY" \
  --data-urlencode "user_id=$TE_EXECUTION_USER_ID" \
  --data-urlencode "execution_profile_id=$TE_EXECUTION_PROFILE_ID" \
  --data-urlencode 'execution_capability=teamevolver.execution-plan.v1'
```

用响应中的 `execution` 核对 Agent 的加载记录。已经开始的会话仍按当时固定的组合上报。模板组件 ID、版本与哈希均由 TE 生成。

实现参考见[接入信息界面](web-ui/src/features/skills/execution/AgentExecutionIntegration.tsx)、[模板元数据](team_skills/execution/registration.py)、[输入契约](team_replay/execution_inputs.py)和[实际消息核对](team_replay/source_inputs.py)。

## 8. 手填拉取条件，验收一条真实 Session

打开「数据源接入 → Session 拉取」。先核对「适配器文件」「适配器版本」已经保存，点击「测试已保存连接」。Worker 的 Langfuse Host 和 Key 由运维从同一个业务项目取得并设置，见[原 README 第 3.1 节](PROMPT_EVOLUTION_README.md#31-数据源文件怎么写)。确认 Langfuse 中看到的项目就是 Worker 实际连接的项目。

数据源维护人首次设置时，`LANGFUSE_PULL_HOST` 从 Langfuse 服务部署地址取得，按 Worker 可访问的地址填写。`LANGFUSE_PULL_PUBLIC_KEY` 和 `LANGFUSE_PULL_SECRET_KEY` 从业务 Trace 所在项目的现有集成凭证取得，交由运维写入 Worker 的密钥配置。同一项目的公钥与密钥配对使用。若没有权限读取现有凭证，由该项目管理员提供或按其流程签发。预览条件的「用户」和「Session ID」都从 Trace 取，无需使用这些密钥。

| 字段 | 值从哪里获取 | 填法与核对 |
| --- | --- | --- |
| 起始时间、结束时间 | Langfuse 目标 Trace 的发生时间 | 控制台用浏览器本地时区输入，再转换为 UTC。例如 Trace 的 `2026-11-17T02:01:00Z`，用北京时间的浏览器可选当日 `10:00` 到 `10:05`。字段是否必填由 Adapter 声明，README 示例要求两项都填写。 |
| Session ID | Langfuse 会话详情或该 Trace 的 `sessionId` | 可选。首次验收只拉一条对话时复制完整上游 ID。指定后，Langfuse Adapter 按该 ID 获取会话，同时按 Trace 名称筛选。 |
| 用户 | 目标 Trace、Session 或 Adapter 识别的上游用户字段 | 可选，填实际业务用户 ID。用户不确定时先留空，用上游 Session ID 定位。 |
| 预览条数 | 根据本次查询范围设置 | 默认 100，允许 1–1000。首次核对一条可填 1。它控制预览条数，正式拉取按提交的筛选条件执行。 |
| 已归档的 Session ID | 拉取、分析完成后，「运行总览 → 会话」列表中标题下方的完整标识 | 常见形如 `session_<入库 ID>`。复制完整内容到执行配置的「检查会话匹配」。可用列表的 Trace 列核对对应的上游记录。 |

1. 点击「预览已保存」，核对标题、用户、时间等返回信息。显式填写上游 Session ID 时，预览可能只回显此 ID，此时以上游 Trace、正式拉取和归档记录继续确认。
2. 点击「拉取入库」，查看状态。`fetched` 是取到的数量，`enqueued` 表示已入队，`duplicate` 表示相同来源快照已受理。强制分析及归档后，从「运行总览 → 会话」复制完整归档 Session ID。
3. 返回「进化链路 → 最终执行配置 → 接入真实 Agent 的会话」，填「已归档的 Session ID」并点「检查会话匹配」。核对显示的发布 ID、哈希、匹配结果和证据准入状态。共享模板会列出匹配请求数，固定 Prompt 分支看匹配结论，计数可能显示 0。
4. 需要验收 True Replay 时，用一个 Case 检查 Baseline 和 Candidate 是否在独立 Session 中执行，是否保留相同的本题输入，以及加载证据是否来自最终模型请求。进化选材还会检查价值、消费状态、独立来源、用户数量和成组条件。

| 出现的现象 | 回到哪里核对 |
| --- | --- |
| 连接正常，找不到 Trace | 核对 Worker Host、业务项目 Key、Adapter 固定的 Trace 名和浏览器时区，在 Langfuse 确认上游 Session ID。 |
| 拉取正常，检查找不到 Session | 等分析与归档，再从同租户的会话列表复制完整 TE Session ID。 |
| 提示来源身份未验证 | 请运维核对稳定的来源实例和项目命名空间。实现可使用上游 Host 或显式 `SOURCE.source_instance_id`，项目从原始 `projectId` 或 Adapter 声明的 `upstream_namespace`／`project_id` 取得，核对是否冲突。 |
| 提示 Agent、Runtime 或模板不匹配 | 查业务加载记录与 Trace metadata，同部署版本核对配置 ID、组件 ID、模板版本、哈希和真实运行时。 |
| 提示原始请求或变量不一致 | 对应 GENERATION 逐条核对角色、顺序、正文、变量、空白、前缀和历史，检查本轮用户输入是否重复拼接。 |
| 显示已匹配，仍未准入或没有 Candidate | 先确认来源根和 `valuable` 判定，再核对材料消费与成组门槛。 |
| 匹配成功，True Replay 失败 | 从「运行状态 → Replay 运行环境」核对已保存的 Adapter 版本、Worker 网络、凭证、执行环境和最终请求加载证据。 |

实现参考见[Session 拉取界面](web-ui/src/features/sessions/DataSourcesView.tsx)、[来源身份校验](session_ingestion/source_revisions.py)和[匹配检查接口](team_skills/api/execution_assets.py)。
