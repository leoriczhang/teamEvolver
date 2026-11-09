# 业务 Prompt 与上下文进化

teamEvolver 可以在既有 Skill Candidate 链路中，管理业务 Agent 的 Prompt 与注入内容，并把它们与冻结的 Skill 版本作为一个执行组合验证、发布和回滚。候选生成、True Replay、发布和 Agent 实际加载是不同阶段；外部独立复核属于可选增强策略，登记配置或任务完成不等于业务效果改善。

## 范围与前提

| 允许修改的模块 | 当前行为 |
| --- | --- |
| `skill` | 沿用 Skill bundle 进化，包括允许编辑的包内资源 |
| `prompt` | 修改已登记的 `agent_prompt` 与 `context_fragment` 正文；关联 Skill 的 bundle 和版本保持不变 |
| `skill`、`prompt` | 联合修改，并对完整组合重新验证；只为实际改变的资产写版本 |

`prompt` 指业务 Agent 的 Prompt/注入组件。控制台下方的 TE 进化引擎 Prompt 编辑器仍用于控制 TE 自己的分析、候选生成与裁判行为。当前范围不包括模型权重、任意 Agent 引擎源码、工具实现、权限或部署配置的自动修改。

需要先有业务执行基线、可验证的来源 Evidence，以及支持目标组件替换和最终请求观测的 Replay Adapter。默认复用 Skill 的静态检查、运行检查和主 True Replay；独立验收 Test Dataset 只在增强策略中必需。使用 Skill 的 Agent 还需有相应团队 Skill；纯 Prompt Agent 可以有 0 个 Skill 依赖。真实业务 Agent 还需接入执行组合的拉取、渲染、加载与请求观测。

## 登记执行配置

租户管理员可在“本次业务 Agent 执行配置”区域登记，也可调用 `POST /api/skill-evolution/execution-profiles`。`affected_skill_ids` 中列出的 Skill 必须已经存在；纯 Prompt Agent 传入空列表 `[]`，不创建占位 Skill。

界面登记时须提供实际业务 Prompt，上下文片段依附于该 Prompt 进行匹配与进化。控制台会识别正文中的 `{{变量名}}`，逐项填写实际默认值，并生成共享模板的输入契约；确实需要空值时显式选择空值。模型字段为可选配置，其他运行参数仍可在高级 JSON 中设置。先预览组件、变量和关联 Skill，再确认登记。`POST /api/skill-evolution/execution-profiles/preview` 接收与登记相同的请求，只校验模板、变量、Skill 依赖及 ID 冲突，不写入版本或启动任务；最终登记仍在事务中重新检查，预览不占用 ID。业务用户提示词及对话历史属于本次请求输入，不填入 TE 引擎 Prompt。

```json
{
  "profile_id": "business-agent-01",
  "runtime_type": "observed_http",
  "agent_id": "agent-01",
  "user_id": "alice",
  "affected_skill_ids": ["demo"],
  "components": [
    {
      "component_id": "agent-01-main",
      "kind": "agent_prompt",
      "slot": "system",
      "order": 0,
      "content": "Follow the task requirements in the {{domain}} domain."
    },
    {
      "component_id": "agent-01-context",
      "kind": "context_fragment",
      "slot": "context",
      "order": 1,
      "content": "Check the user's explicit constraints before acting."
    }
  ],
  "environment": {
    "request": {"model": "configured-model-id", "temperature": 0, "tools": []},
    "template_variables": {"domain": "document processing"},
    "validation": {"strategy": "standard-v1"}
  }
}
```

示例的 `observed_http` 是模型 HTTP 传输参考运行时，不能据此认为已经接入真实业务 Agent。`environment.request` 是参考适配器最终发送的非 `messages` 请求字段；具体模型名与选项必须与配置的模型端点兼容。Key、密码和 Authorization 不存入执行配置，继续使用现有凭证管理方式。

注册限制：标识符不超过 200 字符；关联 0–50 个不同 Skill；包含 1–32 个组件；每个正文非空且不超过 256 KiB；`order` 是 0–1000 内全局唯一的整数。slot 支持 `system`、`developer`、`context`，其中 `context` 在当前参考装配器中映射为 developer 消息。

服务端计算组件正文哈希、初始 revision 和绑定，保存关联 Skill 的版本及 tree hash，并建立完整 release。重复登记同一 profile 或同租户已有组件 ID 返回冲突；注册不是覆盖已发布版本的更新入口。查询使用 `GET /api/skill-evolution/execution-profiles` 与 `GET /api/skill-evolution/execution-profiles/{profile_id}`。

模板只支持 `{{identifier}}` 标量替换，可包含占位符两侧空白。变量来自冻结的 `environment.template_variables`，支持字符串、布尔和数字；不执行表达式、条件或循环，也不递归渲染变量值。缺失变量和不支持的模板语法会阻断执行。Replay 同时核对模板哈希、变量集哈希、渲染正文哈希和实际请求正文。

共享同一模板、每次注入不同变量时，可在 `environment` 显式设置：

```json
"template_input_contract": {
  "schema_version": "teamevolver.template-input-contract.v1",
  "variable_names": ["domain"]
}
```

注册时 `template_variables` 必须恰好提供这些变量的默认值。服务端以组件 ID 作为稳定 `template_id`。源 Trace 的 `input.template_inputs` 保存 `teamevolver.execution-inputs.v1`：`template_refs` 按组件 ID 记录模板 ID、revision 和 content_hash，`template_variables` 保存本次实际变量，`input_messages` 保存只读的公开 user/assistant 输入消息。来源 Session／Trace 身份由服务端补齐，模型不能指定；同一来源还必须有可逐字核对的完整模型请求。离线重建的请求必须明确标注其来源，不能冒充历史真实请求。

Case 生成沿用现有 Checklist 和源锚点机制，由服务端从锚点物化变量和输入消息。每题的新旧版本使用相同变量、消息、模型及工具配置，只替换候选组件正文。验证冻结并核对逐题执行计划及输入哈希；历史题去重也区分不同变量和输入。原回答、评分和后续纠正只作为进化材料，不作为该题的模型输入。该能力不增加裁判或发布门槛，也不修改 Agent 的线上激活方式。未设置契约的固定 Prompt 和 Skill 保持原行为。

显式共享模板模式按完整 Session 选择有界材料窗口：本轮材料全部保留，历史材料按已有顺序在字节预算内加入。选中及省略的历史来源写入冻结快照，Worker 重试复用原快照。当前整批过大时明确失败且不消费材料，需要调用方重新分批，不截断原文或将未读材料算作已使用。材料覆盖数和最终 Replay 测试数分别统计。

共享模板的 Case 物化要求原始目标锚点，沿用 `recorded_anchor` 协议。离线独立记录保持原来的独立记录校验；连续会话只有在目标请求的模板引用、变量和实际 GENERATION 输入与冻结基线逐字核对通过后，才可作为请求锚点。它仍标为连续会话中的一轮，不改成独立记录，也不增加独立来源根。模型只选择服务端提供的合法锚点，服务端读取原始题目、变量和公开输入；缺失或冲突时拒绝，不让新题猜用另一条请求的变量。Checklist、Case 数量及跨 Session 的独立来源门槛保持原规则。

## 将变量文本登记为上下文组件

在「登记与进化设置」选择已有业务执行配置，打开「登记变量上下文基线」。填写新配置 ID、粘贴变量样本 JSON 数组、选择要管理的文本变量，先预览再登记。源配置与原候选保持不变；新配置继承源发布版本的 Runtime、模型、Skill 依赖与其他输入配置。界面默认只允许进化选中的上下文组件，也可选择同时允许修改相应 Prompt 模板。

以已声明 `policyText` 变量的源配置为例，两个接口使用同一个请求体：

```json
{
  "profile_id": "agent-with-managed-policy",
  "source_release_id": "the-selected-current-release",
  "managed_variables": ["policyText"],
  "variable_samples": [{"policyText": "Original policy A"}, {"policyText": "Original policy B"}, {"policyText": ""}],
  "evolution_scope": "context"
}
```

先调用 `POST /api/skill-evolution/execution-profiles/{profile_id}/variable-contexts/preview`；调用去掉 `/preview` 的接口登记新基线。服务端重新计算结果，并以 `source_release_id` 检查来源版本，变化时返回 409。最多 200 个样本对象、合计 1 MiB；选中变量必须是已声明并用于业务模板的文本变量。非空原值逐字去重，空或仅空白的文本继续作为原输入，不创建空资产；不修改样本，不做 Unicode 或空白归一化。仍遵守每个配置最多 32 个组件的限制。

也可在界面按变量分别录入多个原始文本值，或导入已有 JSON 文件。接口使用 `variable_values: {"policyText": ["Original policy A", "Original policy B"]}`，与 `variable_samples` 二选一。对象必须恰好覆盖所选变量，每组 1–200 个文本值，总输入不超过 1 MiB。各变量独立登记，不要求补齐无关变量，也不展开变量的组合。预览中的 `input_mode` 区分两种输入，集合模式的 `sample_count` 是所有组内提交的值条数，不是会话或独立证据数量。

每个新片段具有稳定组件 ID、初始 revision 1，以及服务端生成的 `binding.inline_variable`，包含宿主 Prompt 的 `component_id`、变量 `name` 和原值的 UTF-8 SHA256 `source_value_hash`。按每道 Case 的冻结原值选择片段，在原占位位置放入片段正文，不增加独立消息，也不递归解释片段正文里的 `{{...}}`。未登记的值继续原样渲染。编辑正文不改变来源选择器；相同原值跨多个样本复用同一宿主下的资产。

登记预览、版本和候选界面按「执行配置 + 变量名」展示变量组；一个变量组的多个取值是组内成员。Planner 按组归纳问题，再明确需要修改的成员，沿用原有精确编辑与候选事务。四个变量各有三种取值时，对应四个变量组及十二个取值成员，实际输入组合只属于 Case，不创建八十一个进化对象。成员仍保留宿主、原值选择器和各自版本；组版本指纹从冻结成员清单计算，不重写旧执行计划、组件 ID 或历史哈希。

`environment.evolution.editable_component_ids` 固定本组合的可编辑组件范围；未配置时沿用原有全部组件可编辑行为。片段、模板和只读输入仍沿用同一候选编辑、Checklist、True Replay、审阅、组合发布和回滚链路。登记本身不更新 Agent、改写变量表或发布候选。

Agent 必须使用支持该绑定的装配器。旧 AgentLab Runtime 的 Replay 由 TE Adapter 创建确定性的传输视图，分别验证逻辑资产计划和实际传输计划哈希及最终请求字节；无法精确表达的投影在调用前拒绝。这不代表旧 Runtime 已支持直接激活逻辑组合，普通 Agent 采用需要相应的装配接入。

## 接入真实业务 Agent

在所选执行配置中展开「接入真实 Agent 的会话」，复制当前版本的元数据结构。`GET /api/skill-evolution/execution-profiles/{profile_id}/integration` 是只读接入示例，包含稳定模板 ID、revision、正文哈希和已登记上下文的变量映射。它展示的是登记默认值，Agent 必须替换成本次真正使用的变量与公开输入，并在 Langfuse GENERATION 记录实际模型请求；不能把示例或最新发布信息回填为历史使用证据。

元数据放入 Trace 的 `metadata.teamevolver_execution`，协议详见 [Agent 接入协议](../agent-integrations/06-protocol-v2.md)。TE 拉取后核对 Agent／配置身份、模板版本、实际变量及模型输入字节；同一个模板使用不同取值仍进入同一配置，匹配到的上下文取值通过原值哈希选择成员。缺失或冲突的声明保留原始记录并给出不匹配原因，不猜测模板或伪造 Skill。

界面可输入已归档 Session ID，调用 `GET /api/skill-evolution/execution-profiles/{profile_id}/session-match?session_id=...` 检查是否匹配及命中了哪些组件。该接口不分析、不消费会话，也不发起进化；匹配不代表材料未被消费、纠正信号充足或已经满足成组门槛。实际进化仍选择既有 `prompt` 模块、执行配置和同一候选／Replay／审阅发布链路。

一个连续 Session 使用多个 Prompt 时，纯 Prompt 配置只选取与自身版本相符的目标 Trace，其他 Trace 保留为只读 Context，独立来源根的数量不变。选择材料时先核对匹配，再应用本轮 Session 数量上限；无关会话不占用该上限，也不被该配置消费。纯 Prompt 消费记录使用 `execution:<profile_id>` 范围，同一会话可分别支持两个配置，各自只消费一次；Skill 原消费范围保持不变。旧的纯 Prompt 消费记录根据已完成命令冻结的配置身份识别，不回写历史结果。升级需应用追加迁移 `023_execution_consumption_scope.sql`，再更新 API／Worker。

## 集中查看 Prompt 版本

租户管理员进入「运行总览 → Prompt 版本」，可按执行配置、Agent 或用户查找业务 Prompt。打开「查看版本与指标」后，在同一个窗口切换发布版本，并查看：

- 该发布版本的 Prompt／上下文正文、组件版本与哈希，以及冻结的 Skill 依赖；
- 相对当次进化冻结基线的正文差异，也可另选历史发布作正文对照；
- 该版本发布所依据的 Replay 完成情况、交互轮次、工具调用和 Token 对比；
- 发布信息和这个版本的 Agent 下载／激活回执。

在进化链路中选择业务执行配置后，也可点击「查看 Prompt 历史版本与指标」直接进入对应配置。查看历史版本只读取记录，不启动 Replay，不修改候选、当前发布或 Agent 配置。

`GET /api/skill-evolution/execution-profiles/{profile_id}/releases/{release_id}` 返回所选发布的不可变正文及可核对的冻结验证摘要，不使用 Candidate 后来更新的评估替代历史结果。初始登记版本没有新旧对比；回滚版本说明恢复来源，不冒充新一轮验证；历史证据缺失或绑定不匹配时，正文仍可查看，指标明确显示不可用。正文对照切换不改变指标所对应的冻结 Baseline。链接保留当前租户，并可使用 `execution_profile`、`execution_release` 定位版本。

“TE 当前发布”指平台当前提供拉取的组合，不代表 Agent 正在使用。下载／激活回执是 `agent_reported` 历史事件，不能代替最终模型请求证据，也不能证明 Agent 当前仍使用该版本。版本窗口展示的回执来自执行配置最近 20 条记录，较早事件可能不在其中；没有显示回执不等于从未采用。

## 选择模块与触发

“本次允许修改的模块”只影响这一次手动执行；“后续任务默认允许修改的模块”保存到计划。默认是 `skill`。选择 `prompt` 时同时选择已登记的业务执行配置。

```text
POST /api/skill-evolution/trigger
```

```json
{
  "allowed_modules": ["prompt"],
  "execution_profile_id": "business-agent-01"
}
```

可另带至多 200 个 `session_ids`。返回 `202` 与 `job_id`，从 `GET /api/jobs/{job_id}` 看执行状态。受理时冻结模块选择、执行基线和配置 revision；手动选择不会修改已保存的默认值。保存计划使用 `PUT /api/skill-evolution/settings` 的 `revision` 与 `evolve.evolution_allowed_modules`、`evolve.evolution_execution_profile_id`。

本次手动触发若需要提高或取消候选生成的 Token 上限，可另带 `generation_budget`：

```json
{
  "allowed_modules": ["prompt"],
  "execution_profile_id": "business-agent-01",
  "generation_budget": {"max_total_tokens": null, "reason": "本次实验已授权不限额，保留真实消耗"}
}
```

该授权仅在选择 `prompt` 时有效，必须有非空原因；数值只能高于冻结基线的原上限，`null` 表示无上限。服务器派生原上限，将完整授权固定到本次 Worker 命令，不修改已发布组合、模型参数或原始 Evidence。计量仍按既有候选分支范围累计，不冒充所有服务的总费用。没有授权时保留原有限预算。生成授权不自动改变后续 Replay 额度；后者沿用候选的验证预算接口。

允许范围与实际变化分别记录为 `allowed_modules`、`actual_modules`。选择联合模式不要求两类资产都发生变化；未选择的模块不能被候选生成器或编辑接口越界修改。

纯 Prompt 配置的 `affected_skill_ids` 与发布文档的 `skill_dependencies` 均为空。它沿用同一候选、验证与评审 API，候选通过 `candidate_kind="execution"` 和 `execution_profile_id` 标识；`skill_name`、`candidate_skill_name`、`current_skill`、`candidate_skill` 为 `null`，不生成 `SKILL.md` 或 Skill 文件。控制台展示业务组件真实文本差异、组件 revision、基线 release ID、组合 hash 和 0 Skill 依赖；Skill 版本不适用。审计分组的 `group_key` 为 `execution:<profile_id>`，不冒充未关联 Skill 的新建技能组。

纯 Prompt 配置即使同时勾选 `skill` 与 `prompt`，也不会凭空创建 Skill；实际变化只记录本次修改的业务组件。原 Skill 候选保持 `candidate_kind="skill"`，已有 Skill 与 Prompt 联合进化继续校验和冻结真实 Skill 版本。

升级环境需要先应用追加的 `022_prompt_only_candidates.sql` Schema，再运行新版本 API/Worker；应用启动不会自动建表。该迁移保留原 Skill Candidate 类型，纯 Prompt Candidate 使用独立的执行配置身份，不把 profile 名称写成 Skill 名称。

## 来源 Evidence 的边界

候选生成器必须先核对业务基线中已有的要求，再诊断缺陷。业务条件、首次／再次触发分支、列表消息的基本含义和优先级属于需要保留的任务约束，不能为了去重或提高通过率改成可选参考。重建请求不等于历史真实请求；独立离线记录的相邻顺序不构成“上一轮”证据。

生成器与 Test Dataset 合成器共享目标 Evidence 的边界描述和内容哈希。外部评分保留原文、声明来源及解析结果，发生分数与说明冲突时不重标标签，也不据未知权重猜测正确分数。出题仍使用现有两个当次 Case、6–8 条 Checklist 和历史回归机制；显式离线批次的请求锚点约束见 [True Replay](./06-true-replay.md)。这些约束不能保证模型修订必然改善，仍需实际 True Replay 与审阅。

有来源版本信息的 Session 使用稳定的 `provenance.source_session_key` 表示独立上游根会话，以 `target_trace_refs` 表示本次事实，以 `context_trace_refs` 保留解释上下文。重复拉取、增长快照和语义切分不会增加独立根会话数量；旧 Trace 只作为 Context，不能重复支持新 Evidence。相同 Trace 的有效修订替换旧事实，不增加独立来源。

来源身份未验证、Trace 身份不足、修订冲突、纯 Context 快照，以及显式 synthetic、Replay、Candidate audit 来源，不作为有效新 Evidence。旧的无 provenance Session 保留 Skill-only 兼容；这不补足业务 Prompt 的历史来源证明。进化门槛及每个跨 Session 合成 Case 的最少来源数，均按独立根会话计算。

业务 Prompt 诊断要求实际模型请求 Evidence。模板、当前配置、`metadata.systemPrompt` 或空 system 消息不能代替最终加载证明；不能用今天的 Prompt 回填历史。可验证上游 GENERATION 中与冻结基线匹配的非空 Prompt 可支持诊断，但它仍不等于完整可回放运行环境或真实加载验收。

## 候选生成中的多轮纠错

业务 Prompt 与注入组件复用 Skill 的 Plan → Act → Observation → Final 控制循环、精确锚点编辑内核和配置的轮次／工具调用限制。Prompt 适配器负责组件正文、模板变量和来源引用，不套用 Skill 名称、description、文件包或 `team_skill` 非空分类要求，也不创建占位 Skill。

生成器先提交带原始引用的诊断或精炼假设，读取当前组件，再做原子替换、插入或删除。编辑错误、引用越界、模板变量变化或无效模板作为 Observation 返回，允许在同一次生成中修正。待审候选存在时，以其冻结的工作组合为编辑起点，以发布基线为只读对照；合法计划保留已有引用和编辑依据，无效后续计划不清空已接受状态。

已发布基线、冻结模板变量及声明的运行契约用于识别业务事实、角色、权限、任务条件与例外。未发布工作组合和诊断只是待验证假设，不构成新增业务授权。机器评价、场景训练目标和另一对话者的愿望不能自动改变 Agent 的业务义务；历史缺失保持未知，不跨独立记录拼接事实。生成器可针对原始 Evidence 支持的问题修订策略、合并冗余指导，保留业务要求和有效能力。

`replace_component_content` 在先读取当前正文后，按 `expected_content_hash` 把完整替换展开为同一精确编辑操作，复用计划授权、引用、模板变量及原子性校验。组件正文只能来自成功暂存的工具修改，Final 不能直接提交正文或绕过可写范围。`inspect_changes` 展示发布基线、工作组合和本轮草稿的字符数、UTF-8 字节数及差值；这些不是 Token 估算或长度门槛。

`validate_changes` 只执行确定性的范围、原始 target 引用、模板变量、渲染和编辑来源检查，不额外调用模型。优先由 Final 明确提交；若轮次耗尽，已成功修改的草稿只有在计划、实际变化及全部机械校验通过时，才按 Skill 的容错方式形成候选。没有有效修改或检查未通过时仍跳过。兜底不自动发布，也不放宽来源或变量约束。`editor_audit` 保存计划、编辑次数、Evidence 查阅、编辑来源和暂存组合哈希。机械校验不证明业务语义等价或效果改善；实际业务结果仍由 True Replay、Candidate Review 与 Publish 判断。

生成输入无损携带已选 Evidence 的原始字段、来源边界、外部评分与 Context-only 对照。搜索只作索引；完整读取或显式分页保留长度和哈希。初始请求和累计编辑历史均检查输入预算，超限明确跳过，不静默截断。Context-only 不能增加有效来源数或充当原始纠正。

后续 Prompt 生成保留同一候选版本中已测得的 `interaction_turns`、`tool_call_count` 和 `total_tokens`，并通过 `active_candidate_replay_feedback` 提供该版本已完成原生主回放的公开交互、实际答复和逐项检查结果。正常多轮交互保留；这些只是下一轮改稿的开发诊断，不是新的业务事实、黄金答案或独立来源。锁定验收、A/A、重复及单模块探测不回灌；缺失保持不可用，不增加模型点评调用。旧规划快照不回填后来完成的结果。Test Dataset 合成仍只接收原始 Evidence 和原有基础效率反馈，不接收该详细反馈、候选正文或私有验收题。

历史材料保留必要原始 Session 正文与来源，Prompt 路径按需展开，Skill-only 输入保持原摘要方式。旧摘要记录仅在 Session 身份和内容哈希完全匹配时恢复原文；无法恢复时明确不可用，不凭摘要补造，也不把当前版本冒充历史内容。

确认网关支持 JSON Object 输出后，可启用租户 `skills.prompt_editor_json_mode`（默认 `false`），仅向 Prompt 编辑请求增加 `response_format={"type":"json_object"}`。`skills.prompt_editor_max_rounds` 默认 `null`，继承 Skill 编辑轮次；显式值为 2–64 的整数。两项均不改变普通 Skill 请求、Replay 标准或机械校验要求。

Test Dataset 与候选从同一冻结 Evidence 独立合成，保留原有两个当次 Case、每题 6–8 项 Checklist 和来源根要求。合成仅调用一次生成模型，再做确定性的 schema、来源和任务锚点校验。普通在线 Prompt 复用 Skill 对多余题目和检查项的规范化，以及基于真实 target 原文的确定性兜底；Context、候选声明或摘要不能补造业务题目。显式离线锚点协议仍严格失败，不用此兜底换题。Checks 应核验最终回复、公开消息、工具事件或产物，并包含裁判所需的定义；内部草稿和未输出的自检不可当作公开完成证据。此阶段不另设模型审核或定向改题循环。

Replay 裁判实际只接收本 Case 的完整 Checklist、公开交互、messages 和 artifacts，不会自动收到来源业务 Prompt、模板变量或内部 observed_requests。固定 query 或整套 Checks 须自足提供必要定义，不为补定义改写 query，也不向 Agent 泄露私有 Checks。

业务 Prompt 的显式离线独立记录支持 `query_mode="recorded_anchor"`：模型选择合法 `anchor_id` 并合成 Checks，不返回 `query` 字段。服务端核对冻结 target、Trace／Evidence 引用、Context 边界、完整正文哈希与字节长度，再逐字物化原请求。前后空白、换行和 Unicode 文本均保留。模型返回额外 query、锚点串配或来源不符均明确失败，不模糊匹配、换题或凭摘要补齐。普通 Skill 和非离线 Prompt 不启用此协议；原 wire 与物化 query 的来源收据仍分别保存。

离线协议在未注入阶段参数提供器时采用 32768 的合成输出预算，普通 Skill 和非离线 Prompt 默认仍为 16384；明确的宿主参数不被覆盖。当前独立合成入口尚未把租户存储的 `stage_settings.dataset_synthesis` 接到该提供器，存储值不等于此路径的有效请求参数。该缺口不由本次精简修复。预算不是实测消耗或通过门槛；截断或无效 JSON 明确失败，不补造缺失业务内容。

Replay 裁判中的 `satisfied=true` 表示满足该条原始标准，包括禁止式要求。公开观察范围完整时，未出现被禁止的行为可以作为满足的证据；缺失轨迹不能被当作行为未发生。条件性检查按其原条件判定，不额外要求未触发分支。该语义说明不自动翻转历史分数，也不为有效负面判定增加重抽机制；原检查项、技术不可用处理及采纳门槛保持独立。

检查项中的“尤其／例如”默认不是穷尽列表；裁判须核对原标准的完整范围及相关已提供历史，不能只排除举例就宣称符合“任一／所有”约束。原生报告的 `format_diagnostic.request_pin` 保存有效裁判 SYSTEM、动态 JSON Schema、实际构造的 system 消息及 Checklist 的哈希，区分不同裁判输入。`scope=intended_request` 只表示拟提交请求；预算、输入或 Provider 失败时，它不证明实际传输或执行。哈希不代替业务判断，也不保存凭证明文。

原标准明确禁止历史近义复述时，按实质陈述或被询问事项比较。仅换语气、词序、称呼、补充已知场景或另加一句，不能豁免其中实质重复的部分；共用个别词或话题本身也不直接等于复述。证据应短引最相关历史和当前片段作对照，不输出内部推理。此说明仍受原标准的条件、例外和判定粒度约束。

## 准备锁定验收集

本节仅适用于显式启用 `environment.validation.strategy="enhanced-v1"` 的增强策略。默认 `standard-v1` 不要求锁定集，也不把已配置锁定题加入主回放。增强策略先按 [Replay API](../api/05-replay-branch.md) 创建 Test Dataset，再把其 ID 放入 `environment.validation.holdout_dataset_ids`。锁定集使用标准数据集格式，并包含以下来源字段：

```json
{
  "schema_version": "team-replay.dataset",
  "dataset_id": "locked-agent-01",
  "name": "Independent acceptance tasks",
  "skills": [{"skill_id": "demo"}],
  "metadata": {"validation_role": "locked_acceptance"},
  "cases": [{
    "case_id": "acceptance-01",
    "query": "Complete an independent document transformation task.",
    "skill_ids": ["demo"],
    "checks": [{"id": "R01", "kind": "output", "text": "Satisfy the supplied task constraints."}],
    "materials": [],
    "provenance": {
      "source_runtime": {"type": "observed_http"},
      "source_session_keys": ["src_actual-independent-source-key"]
    }
  }]
}
```

`source_session_keys` 使用实际来源根 ID，不能用不同数据集 ID 掩盖同一来源。每个 Case 必须有来源根，且锁定集不能与诊断/规划 Evidence 或其他选中验收集共享根。验证受理时冻结数据集 revision、hash、材料与 Case 列表；来源不明、重叠或没有锁定集会阻断发布。

自动候选生成器只获得用于排除的保留来源 ID，不获得锁定 Case 的 query、答案或 Checklist。某个来源根首次用于一个 Candidate revision 后，不能再作为另一个 Candidate 或新 revision 的未曝光验收来源；同 revision 重试可复用。更换数据集名称或 ID 不会清除来源曝光记录。

## Replay Adapter 与真实加载

支持新计划的 Adapter 在 `REPLAY_ADAPTER.capabilities` 中声明：

```python
{
    "execution_plan": "teamevolver.execution-plan.v1",
    "component_kinds": ["agent_prompt", "context_fragment"],
    "request_observation": "local_dispatch",
    "skill_treatment": True,
}
```

`skill_treatment` 是联合 Skill/Prompt 观测能力。Adapter 源码仍只能由服务 Root Key 写入，运行固定源码 revision；旧 Skill-only Adapter 继续适用于原链路，不能自动获得 Prompt 覆盖能力。

`ReplayContext.execution_plan` 包含完整组件、稳定的 slot/order/binding、目标组件 ID 和非目标环境。共享组件不会随 Case 的 Skill 子集丢失。Adapter 必须实际替换对应内容。声明 `local_dispatch` 时，通过分支的 `request_observer` 在最终模型传输边界发送已捕获的不可变请求 bytes，返回 `AgentObservation.execution_receipt`；TE 对照本地派发记录检查完整请求、渲染正文、顺序、位置、目的地址、原材料及非目标模型/工具配置。既有 Agent Lab 接入使用 `remote_attested_dispatch`，验证独立 control 密钥签名、nonce 与真实远端最终请求，见 [Replay 接入契约](../api/05-replay-branch.md#纯-prompt-replay-与现有-agent-lab)。仅自报 hash 或 body 不足以通过。

缺失、忽略、重复注入、后续覆盖或错 hash 导致 unsupported/inconclusive，不会当作生效。观测依赖受信、固定版本的 Adapter 代码，不提供对任意上传 Python 或远端服务的密码学证明。

候选回放明细可展开每个分支的“最终模型请求加载证据”，核对 `execution_verification`、`execution_plan_hash` 以及 `observed_requests` 的派发 ID、请求 hash、目的地址和完整请求。没有请求记录时，状态字符串或自报哈希不能作为加载成功的证据。纯 Prompt 候选仍须完成真实回放和冻结策略要求的发布门禁；独立复核只在增强策略中必需。0 个候选或仅登记基线不等于闭环成功。

`team_replay/adapters/examples/observed_http.py` 是可运行的模型 HTTP 传输参考：每个分支维持独立多轮历史，捕获最终模型请求。它不执行业务工具循环、Skill 脚本或任意 Agent 编排，不能代替真实业务 Agent 接入。当前 AgentsHub 示例没有已验证的独立 Prompt/Context 覆盖 API，会明确拒绝这类计划。

参考适配器直接通过 HTTP 调用 Agent 模型，按真实派发记录与响应 usage 独立统计 Agent Replay 通道；这些请求不再计入 TE 内部的 LLM 收集器，避免同一请求重复计费。

## 评估与发布

1. 调用 `POST /api/skill-candidates/{candidate_id}/evaluate`，等待任务完成。
2. 读取 `GET /api/replays/skill/{run_id}/input` 的完整冻结输入与运行结果；`run_id` 使用该次验证返回的 `job_id`。检查全部主测与必要回归 Case 及 A/B 请求。
3. 默认 `standard-v1` 与 Skill 对齐：静态检查、运行检查和原生主回放须通过，同时核对 Prompt 实际加载、冻结组合和依赖版本。调用 `POST /api/skill-candidates/{candidate_id}/validate`，请求 `{"mode":"auto","candidate_revision":2}`；Worker 复用刚完成的不可变结果发布，不为了发布另跑一次随机回放。主回放 `reject` 或 `inconclusive` 不会自动变为通过，`force` 也不能绕过这些 Prompt 必要检查。

只有增强策略还要求锁定集、A/A、重复／单模块探测和绑定精确结果的独立复核。用独立程序或领域专家检查后，调用 `POST /api/skill-candidates/{candidate_id}/independent-validation` 登记外部复核；TE 不替代执行所声明的外部程序：

```json
{
  "candidate_revision": 2,
  "status": "passed",
  "method": "programmatic",
  "evidence_ref": "artifact:independent-review-result",
  "case_ids": ["all-frozen-case-ids-go-here"],
  "notes": "Independent checks and their evidence."
}
```

`status` 支持 `passed`、`failed`、`inconclusive`；`method` 支持 `programmatic`、`domain_review`。`case_ids` 必须覆盖刚完成验证中冻结的全部 Case。服务端从不可变验证记录生成 v2 回执，绑定 Candidate revision、具体 `validation_run_id`、验证命令 `validation_payload_hash`、完整结果 `validation_result_hash` 与验证输入 hash；客户端不能指定这些哈希。回执记录为 `reviewer_attested`，不是模型自动证明。缺少这些绑定的历史声明不能授权新的发布。

增强策略复核后使用相同 `/validate` 请求。原始机器质量判断仍须为 `accept`，增强策略要求的各项门禁也须通过；仅因当时缺独立声明而变为 `inconclusive` 的最终判定，可在附加复核后完成发布。两种策略均保留原验证记录，release 的 `validation_ref` 指向实际依据的原任务。原 Skill-only 候选继续使用既有验证发布路径。

策略在验证受理时冻结为 `validation_plan.validation_strategy`。新任务默认 `standard-v1`，显式配置可选 `enhanced-v1`；旧冻结计划未带此标记时，继续按原增强规则执行，不回写或降低旧任务门槛。采用新默认策略需要重新提交评估，旧结论不会自动改变。

受理与提交发布时都核对 Candidate 内容、预算、适用的复核与审阅状态、配置 revision、Replay Adapter 绑定、执行基线 head 及 Skill 依赖；受理后任一状态或审计序列改变均阻断旧命令。配置与依赖检查使用数据库事务锁，发布整套组合和 Candidate 状态在同一事务提交。新建验证任务清除旧复核在该次输入中的授权；增强策略须针对新任务结果重新复核。发布命令不创建新的验证成本记录，同一 Candidate 历史成本继续累计。

编辑业务正文使用 `PUT /api/skill-candidates/{candidate_id}/execution-content`，请求包含 `candidate_revision` 与 `component_changes`；每条变化只允许 `component_id`、完整 `content`、`reason`、`evidence_refs`。编辑会生成新 Candidate revision 并清除旧独立复核结果。Case、Adapter、配置、依赖版本或执行计划改变后需要重新评估；增强策略还需复核新的冻结结果。

## A/A、重复与单模块对照

仅增强策略在主验证之外执行 A/A、重复运行及联合变化的单类资产对照，默认策略不要求这些额外运行。A/A 使用相同基线资产；不要求随机模型逐字输出相同，也不把正常的“效率持平、inconclusive”当作提升。重复运行检查 verdict 与完成状态稳定性。`only-agent_prompt`、`only-context_fragment`、`only-skill` 对照用于观察单类变化，不保证改进可相加或证明所有因果关系。

| `environment.validation` 字段 | 默认 | 允许范围 / 含义 |
| --- | --- | --- |
| `strategy` | `standard-v1` | 默认复用 Skill 门槛；`enhanced-v1` 显式启用增强验证 |
| `holdout_dataset_ids` | 空列表 | 至多 8 个不同数据集；仅增强策略要求有效锁定集 |
| `max_probe_cases` | 4 | 1–8，探针选择的 Case 上限，仍须覆盖受影响 Skill |
| `max_extra_runs` | 5 | 2–8，A/A、额外重复与单类资产对照的总上限 |
| `repetitions` | 2 | 2–3，包含主运行在内的重复次数 |
| `max_total_tokens` | 默认策略未显式配置时无发布累计上限；增强策略为 1000000 | 显式值 1–10000000 仍约束累计预算；候选预算授权优先。生成阶段原默认预算不变 |

`total_model_cost_gate` 是整个候选的总模型成本门禁，当前验证响应字段为 `cost_gate`。它累计生成与 Case 合成、TE 裁判与反馈、主验证及所有 A/A、重复和单类资产对照的两分支 Agent Replay，以及实际 provider 重试。同一 Candidate 的历次验证和此前生成记录也纳入当前冻结预算；重复读取检查点按 TE `attempt_id`、Agent `dispatch_id` 去重，不作为新调用。

有限预算下，用量缺失、失败请求成本未知、历史记录不完整或 Worker 重试成本尚未对账时，成本门禁保持 inconclusive；`known_total_tokens` 只是已观测成本下界。无累计上限时，`cost_gate.status` 可为 `passed`，缺口由 `warnings` 展示，不单独阻断预算检查。新默认策略不从隐式历史默认值新增发布预算门槛，但不取消显式有限额度，也不修改生成预算。

预算判定与累计统计完整性分开：`accounting_status` 为 `complete` 或 `incomplete`；只有完整时 `total_tokens` 才是总量，否则为 `null`，已知下界保留在 `known_total_tokens`。缺失不能补为零，`te_llm_usage.complete` 如实反映完整性，控制台区分无上限预算通过和统计缺口。旧结果不回写。累计账本不替代 True Replay 的真实完成性、指标有效性、客观效率或实际请求观测；A/A、重复及独立复核是否必需，以任务冻结策略为准。

Replay 裁判与反馈保持原有请求参数，经平台统一调度和记录实际 provider 用量。新执行的 Case 带有 `replay_te_llm_accounting=platform-provider-attempts-v1` 覆盖标识；复用缺少该标识的旧检查点或历史验证时，`replay_te_llm_accounting_unverified` 在有限预算下作为阻断原因，无上限时作为统计提示。重新运行不能补齐此前遗漏的用量，旧报告也不会被回写为零成本或完整成本。

总成本门禁在验证结果聚合时检查累计支出。在有限预算模式下，TE LLM 收集器在自身已知用量达到预算后阻止后续模型调用，探针调度按已观测 Replay 用量停止新增探针。这些局部检查和已经派发的并发请求，不能保证最终累计支出始终低于固定预算；此前生成与历次验证的费用也已经发生。总成本超过预算时，即使其它质量检查通过，发布仍会被阻断。

## Agent 拉取与原子激活

Agent 使用租户 Key 与匹配 profile 的 `user_id` 拉取：

```text
GET /sync/skills?user_id=alice&execution_profile_id=business-agent-01&execution_capability=teamevolver.execution-plan.v1
Authorization: Bearer <tenant-key>
```

响应新增 `execution`：包含 `release_id`、`composition_hash`、`profile_id`、`document.execution_plan`、`document.skill_dependencies` 和精确版本的 `skills` 文件。顶层 `skills` 仍是当前团队 Skill 列表；执行组合必须使用 `execution.skills` 中冻结的依赖，不能任意混用当前 head。ETag 包含组合 hash，Prompt-only 发布也会改变拉取结果。缺少 capability 返回 409；profile 不属于当前用户或租户时不可读取。

纯 Prompt 发布的 `execution.skills=[]`、`execution.document.skill_dependencies=[]` 是合法的完整组合；客户端仍须校验并加载 `execution.document.execution_plan`。不能因 Skill 列表为空跳过 Prompt 安装，也不能改用外层团队 Skill 列表填补依赖。发布、回滚和 Agent 任务固定使用组合 release ID/hash，不增加无关 Skill 的版本。

`team_skills/execution/client.py:ExecutionReleaseCache` 提供 POSIX Agent 侧参考缓存：

```python
from team_skills.execution.client import ExecutionReleaseCache

cache = ExecutionReleaseCache("/path/to/agent-execution-cache")
installed = cache.install(response_payload["execution"])
task_pin = cache.active()  # 每个任务开始时读取一次，此后固定该目录与 manifest
```

客户端校验组合、组件、所有依赖 Skill 的版本和 hash，先写完整不可变目录，再原子切换 active 指针；多进程安装使用文件锁，旧目录保留供在途任务继续使用。它是 Agent 可重建缓存，服务端权威仍是 PostgreSQL。原子切换保证读者看到完整组合，不等于机器或文件系统故障后的持久恢复保证。业务 Agent 必须真正从固定组合渲染、加载 Prompt/Context 与 Skill，不能只调用缓存安装方法。

加载状态通过 `POST /sync/skills/execution-receipt?user_id=alice` 上报：

```json
{
  "profile_id": "business-agent-01",
  "release_id": "released-id",
  "composition_hash": "released-composition-hash",
  "request_id": "activation-event-001",
  "state": "activated"
}
```

`state` 为 `downloaded` 或 `activated`。同一 `request_id` 只可重试相同内容；下载与激活使用不同事件 ID。服务将此类回执标为 `agent_reported`，表示 Agent 自报暴露/激活状态，不等同于模型实际使用，也不能替代 Replay 的最终请求派发证据。

## 回滚与排查

调用 `POST /api/skill-evolution/execution-profiles/{profile_id}/rollback`，传入要恢复的 `release_id` 和当前 `expected_release_id`。Worker 在事务中恢复完整组合，通过新的 release 及必要的新资产版本记录回滚，保留既有历史；当前 head 改变则冲突。Agent 仍需下一次拉取并激活恢复后的组合，在途任务继续使用已固定的版本。

纯 Prompt Candidate 发布后发现问题时，先正常回滚到它原先的基线。确认该发布的直接回滚任务已完成，且没有同 profile 的其他待处理 Candidate 后，可调用 `POST /api/skill-candidates/{candidate_id}/resume-after-rollback`，提交 `candidate_revision`、回滚后 head 的 `expected_release_id`、修正后的 `component_changes` 和 `reason`。服务核对真实回滚命令、原发布和基线完整内容，以同一 Candidate 的新 revision 继续处理，清除旧验证、复核和采纳标记。旧 release、Replay、审计和累计成本保留；这不会自动切换 Agent，也不能直接发布修正版本。普通已发布或已拒绝 Candidate 仍不可编辑，此入口仅允许符合条件的纯 Prompt 发布恢复。

管理员调用 `POST /api/skill-candidates/{candidate_id}/validation-budget`，传入当前 `candidate_revision`、`max_total_tokens` 和记录授权来源的非空 `reason`。开放且包含执行计划的 Candidate 可将有限预算提高到更大的整数（最多 1,000 万），或显式传入 JSON `null` 取消该 Candidate 的累计 Token 上限；省略字段、字符串和大整数都不表示无限。显式有限预算和旧冻结策略不变。修改通过 revision CAS 产生新 revision 和审计事件，清除旧验证与复核授权，要求重新评估，并在增强策略下重新复核；既不改任一执行计划或模型参数，也不清除已发生费用。当前已无上限时，此接口拒绝再次修改，包括旧客户端写回有限值；它不提供重新设限功能。接口支持的额度不代表某项任务已得到相应支出授权。

显式取消后，下一次冻结的 `validation_budget.max_total_tokens`、`validation_plan.probe_policy.max_total_tokens`、验收 policy、结果 `cost_gate.budget_tokens` 和探针 `token_budget` 为 `null`。累计 Token 阈值停止比较；累计账本缺失只形成非阻断 `warnings`，仍保留已知下界、未知总量及历史去重。超时、Checklist、真实加载、当前 Replay 指标有效性与效率比较不变；增强策略的探针数量、稳定性及独立复核也不因取消预算而改变。两套执行计划中的 `environment.validation.max_total_tokens` 保持原值；`null` 不表示零成本，也不修正或追认旧验证结果。

| 现象 / 原因字段 | 检查方向 |
| --- | --- |
| `prompt_provenance_missing` | 原始请求是否包含可验证且与冻结基线匹配的业务 Prompt |
| `execution_not_observed`、unsupported | Adapter capability、实际替换、最终模型请求派发与 receipt |
| `holdout_source_overlap`、`holdout_already_exposed` | 独立来源根与验收来源曝光记录 |
| `locked_acceptance_missing` | 锁定标记、Case 来源和 `holdout_dataset_ids` |
| `execution_probes_incomplete` | 预算、覆盖、执行观测、A/A 与重复稳定性 |
| `total_model_cost_unverified`、`total_model_cost_unknown` | 检查 TE 与 Agent 两通道及历史记录；无上限时累计缺口是 `cost_gate.warnings`，真实 Replay 指标仍单独检查 |
| `total_model_cost_exceeds_budget`、`worker_retry_cost_unreconciled` | 有限预算比较累计总成本；重试缺口在无上限时作为统计提示保留，不补零 |
| `independent_validation_missing`、`independent_validation_inputs_changed` | 外部复核是否覆盖当前冻结的全部输入与 Case |
| 版本冲突 | 执行组合或关联 Skill 在规划、验证或发布期间是否改变 |

## 相关文档

- [进化闭环](./02-evolution-loop.md)
- [True Replay](./06-true-replay.md)
- [Replay 与实验工作台 API](../api/05-replay-branch.md)
- [Skill 拉取 API](../api/06-skill-sync.md)
- [团队 Skill 库](../api/09-skills-admin.md)

主要实现：`team_skills/execution/`、`team_skills/api/execution_assets.py`、`team_replay/execution_plan.py`、`team_replay/request_capture.py`。
