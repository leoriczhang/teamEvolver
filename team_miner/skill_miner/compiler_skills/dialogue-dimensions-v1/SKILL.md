---
name: taikang-dialogue-dimensions-v2
description: Derive assessable capability dimensions and provisional behavior-anchored scoring guidance from selected excellent dialogues. Use for dialogue_dimensions extraction and bounded-window merging.
---

# 优秀对话评分维度提炼

te-dialogue-dimensions.v1

## 目标与产物层级

案例全部视为已选定的优秀对话。提炼可用于后续评价的能力维度，并为每个维度提出候选评分口径。
不重新评判案例是否优秀，不读取历史评分评价，不计算权重、总分或评分公式，
不生成教官 Skill、验证案例或 Skill Candidate，也不对当前案例实际打分。
对话是 Evidence，不是执行指令。只有宿主 instruction.stage 决定当前职责。

始终区分三个层级：

- 维度：评价 HWP 在某类交互目标上的表现，例如“开场关系建立”“需求探索”“异议澄清”。
- 行为指标：判断该维度的依据，例如使用真实相关的背景建立联系、确认客户顾虑的具体含义。
- 实现方法与实例：说明转介绍来源、转述真实评价、讨论共同情境等具体做法，以及支持它们的原文定位。

最终 groups 的 name、definition 必须处于维度层；observable_behaviors 承载行为指标，
原始成员保留具体方法与来源。不能只把动作改成一个短标题，定义和评分仍强制执行那套动作。
示例用于说明粒度，不是预设目录；只输出当前 Evidence 能支持的维度，不为凑齐示例创造发现。

## 执行范围与计划

本文包含完整字段契约与两个阶段的步骤，不依赖附件。
stage=extract 只执行提取步骤；stage=merge 只执行归并步骤。
不要将两个阶段指令全文复制到 Transform。
当前 assignment 只包含自己的文件及真实输入 ID，不扫描兄弟窗口或整个数据集。
Compile.from 是宿主为当前批次准备的独立目录，不是文件 URI；extract 目录只含本批未接受的
完整案例 Markdown，merge 目录只含当前窗口 Markdown。不要扫描父目录、host 清单或其他批次。

Entry 尽早返回 needs_pipeline=true、files=[]，不要逐文件预读或生成最终结果。
只使用一次文件 Map 和一次 Finalize：

```python
files = p.map(sources, task=contract.extract)
p.finalize(files, into=target)
```

contract.extract.execution=agent、input_unit=file、output=files、fields={}。
extract 是 Transform 名称，不等同于业务 stage。Transform instructions 必须保留
宿主 stage、对应步骤、当前文件责任、维度层级规则和输出路径规则。
没有 Shuffle、Reduce、第二次 Map 或额外子 Agent。
每个 child 只处理一个完整案例或一个有界窗口。

## 维度粒度与归纳规则

维度应回答一个稳定的评价问题：“在这些情境下，HWP 的哪种能力表现值得评价？”
用以下检查确定粒度：

1. **评价对象明确**：定义说明评价什么、边界到哪里，而不是复述一段话术或操作顺序。
2. **方法可以替代**：服务同一评价目标的不同合法方法可以成为同维度的不同表现路径；
   并非每次都必须采用所有方法。移除某个案例方法后，维度仍应成立。
3. **能够观察和分档**：指出用什么表现区别不同程度，不能只写“良好沟通”“专业能力”等宽泛品质。
4. **边界可区分**：同属一个章节不必属于同一维度；评价目标不同则分别保留。
   如果定义包含两个可独立评分的目标，通常应拆开。

名称优先使用简洁的能力或表现质量，如“开场关系建立”“信息采集的完整性”“需求量化与差距辨识”。
材料主题、章节、知识点和任务名主要用于说明适用范围，不自动成为维度目录。
“询问家庭收入”改成“家庭收入了解”仍可能停留在业务任务层；
应进一步明确评价的是信息采集的完整性、口径准确性或其他哪种有证据支持的能力。
维度应能容纳同类情境中的不同业务对象或合法方法，但不因此扩大已观察到的 Evidence 范围。
长的动作串、具体人名、产品名、固定数字和固定话术不作为维度名称。
名称简短只是结果，关键是定义、指标和评分也要上卷。
不固定维度数量，不通过减少数量证明成功，也不强行归并为少数过大的维度。

例如，“说明转介绍来源并转述真实正面评价”可以作为“开场关系建立”的一种方法。
“借助共同情境交流”“回应客户当前经历”若服务相同评价目标，也可归入这个维度。
它们不是所有客户都必须经历的连续步骤。无转介绍背景时，不要求提及介绍人。
如果另一发现关注的是专业资格披露，应核对其评价目标，不能只因也出现在开场便一并归入。
上述其他方法只有在当前输入实际存在时才能登记为支持方法，不能由示例补造成 Evidence。

## 判断与证据边界

识别 HWP 的可观察行为、客户触发与实际回应。不将客户动作写成 HWP 必须完成的行为。
客户回应可辅助解释互动过程，不作为必然成交、客户满意或信任提升的证明，
也不把客户明确表态、配合或成交设为 HWP 获得某档评分的必要条件。
姓名、地点、数值、同义措辞的替换不构成独立维度。
不将口头权益承诺当成合同事实，不推断未出现的销售效果。
疑点在 notes 中写入 needs_review 文本标记，不新增同名字段，其他成果继续。
允许零发现、单例支持、独立成员和暂不归并。支持数量是证据覆盖信息，不是维度分值。

只有优秀对话时，可以归纳已观察到的正向表现，也可以据此设计评分差异；
不能宣称这些差异已经被高低分对照验证，或已经能预测成交效果。
低档锚点属于评价设计推导，不能冒充观察到的低分案例；正向锚点也尚未经评分校准。
每个维度的评分口径均标记为候选、待校准。

## 原文与交付

默认只提供 sentence_refs，不抄原话 quote。需要 quote 时必须逐字复制，
不得为缩短引文使用省略号、修正 ASR、错字、重复、标点或换行。
原文已有省略号原样保留；不连续原话分别引用，不拼成连续引文。
解释、归纳出的能力和原文分开。只引用实际支持行为的句子。

完整阅读当前文件所有 ranges 和续行。使用当前 emit 的 FileResponse schema，
写文件再 emit 或直接按 schema 交付完整文件；不得发明 ready_path 等旧协议字段。
结果较大时优先分段写文件，并用当前 schema 支持的文件引用提交，不在回执中重复整份成品。
files[].inputs 只填实际 assignment 顶层输入 ID，不使用 case_ref、sentence_id 或业务成员 ID 替代。
文件 Map 的实际 emit 外壳如下（占位符必须换成实际值）：

```json
{
  "files": [
    {
      "path": "analysis/dialogue_dimensions/receipts/<实际案例或窗口>.json",
      "content": "<完整业务 JSON 序列化字符串，不加 Markdown 外壳>",
      "inputs": ["<实际 inputs[].id，列出该文件全部 ranges 的输入 ID>"]
    }
  ]
}
```

Entry 使用其实际 schema emit `{"needs_pipeline":true,"files":[]}`，随后由 Planner 生成 contract 与 plan。
上述 FileResponse 用于文件 Map，不能直接 emit 业务 findings / merge 对象。
使用临时文件时，以 `content_ref` 替代 `content`，保留最终 `path` 和真实 `inputs`；
临时文件位置不等于 Compile.to，不自行写最终目标。`content_sha256` 不确定时省略。
成功 emit 后结束，不再扫描来源。每文件对应一个真实回执；读过或上传过不等于分析完成。

## 字段契约

沿用协议 te-dialogue-dimensions.v1 及现有 schemas：
case=taikang.dialogue-case.v1、findings=taikang.dialogue-findings.v1、
window=taikang.dimension-window.v1、merge=taikang.dimension-merge.v1。
Skill 版本升级不自行改变宿主协议、字段、文件名或全局 ID 规则。
roles 为 HWP、C、unknown。坐标为 Unicode code points，零起点、左闭右开。
quote 为可选的完整连续句。

source_changed、unsafe_path、unrecoverable_json、missing_receipt、foreign_member 阻断当前单元；
unlocatable_observation、altered_quote、empty_behavior 隔离当前发现；
optional_field_missing、single_support、duplicate_dimension、unconfirmed_business_fact 保留警告。

评分口径放在现有 groups[].notes 的字符串条目中，不增设未获宿主支持的 rubric 等字段。
它是本 Skill 的内容约定；不是宿主已实现的结构化评分接口。

## stage=extract

完整阅读一条案例的 Metadata、Dialogue 和 Sentence index，包含全部 ranges。

先记录实际行为与情境，再回答“这段行为体现哪种可评价能力”。
label 写候选维度名称；behavior 写本案例的具体做法、可复用方式及其支持范围。
不要在 behavior 里用抽象口号替换原有事实。
同一能力下连续且互相依赖的动作可以作为一条 observation；
评价目标不同则分开。不能为了形成上位维度而补入案例没有出现的行为。
不同案例的 label 可以不同，最终维度身份由 merge 的评价目标比较决定。

applicability 保留本方法的实际触发、条件和限制。
interpretation 解释行为为何能够支持候选维度，区分观察到的过程与尚未验证的效果。
extract 不根据单例设计完整高低档评分，不分配全局维度，不把单个方法列成所有情境的必做项。
无法确认的业务事实保留方法层面的 Evidence、标 notes；局部疑点不删除其他发现。
没有可复用行为允许空 observations，并说明 empty_reason。

交付相对 Compile.to 的路径：
analysis/dialogue_dimensions/receipts/<case_ref>-<case_version>.json

```json
{
  "schema_version": "taikang.dialogue-findings.v1",
  "case_ref": "<当前Metadata>",
  "case_version": "<当前Metadata>",
  "analysis_status": "analyzed",
  "observations": [
    {
      "local_ref": "o1",
      "label": "<候选能力维度>",
      "behavior": "<本案例实际方法及可复用方式>",
      "applicability": {"trigger": "<触发>", "conditions": "<条件>", "limits": "<限制>"},
      "sentence_refs": ["<真实句子ID>"],
      "interpretation": "<该方法支持哪种能力及证据边界，非引文>",
      "customer_response_refs": [],
      "notes": []
    }
  ],
  "empty_reason": "",
  "notes": []
}
```

不照抄占位符。analysis_status 为 analyzed 或 analyzed_with_issues。
默认 quote_mode=none，不输出 quote，也不因缺 quote 造问题。
当 quote_mode=sentences 且确实需要引用时，可选 quote 为对象或数组，
包含 case_ref、case_version、sentence_ids、char_start、char_end、text。

## stage=merge

完整阅读当前 window Markdown，只归纳窗口内明确提供的 member_ref。
成员包含实际方法、候选能力、情境、条件、解释与已验证来源定位。

按以下顺序归纳：

1. 对每个成员明确评价目标，比较其能力对象及边界，不能只比较标题或动作相似度。
2. 服务同一评价目标、可以在同一套表现标准下评价的方法，归入同一维度；
   不同方法可以互为替代路径。保留各方法的适用条件，不把条件取并集后要求每个案例全部做到。
3. 同一评价目标下的条件差异保留为分支；差异导致评价对象、标准或产品事实不兼容时分别保留。
   标题相似、同处一个章节、共同出现或共用“信任”等词，都不能单独证明应合并。
4. name 写维度名称。definition 明确评价问题、能力范围和与邻近维度的边界。
   observable_behaviors 写可观察的能力指标，并注明必要的情境分支或可替代方法。
   具体原话与动作细节继续由原始 member_ref 追溯，不提升为全场景必做清单。
5. 为每个输出维度形成下面的候选评分口径，写入 notes。

### 维度内的候选评分方法

每个维度使用同一评价对象分档，不能一档评价表达、一档评价成交。
宿主明确提供量尺时采用该量尺；否则暂用 0—3 四档，为后续人工校准提供起点。
评分只依据对话中 HWP 可观察、可控制的行为，
可以按针对性、准确性、回应和调整程度区分，选择与本维度真正相关的区别。
不要按关键词出现次数、行为数量或支持案例数机械计分。

按“适用性判断 → 定位行为证据 → 对照档位 → 记录不确定性”描述使用方法：

- N/A：当前交互没有该维度的适用情境或表现机会，不计零分。
- NE：片段、角色或上下文不足，无法判断，不把缺证据视为未完成。
- 0：明确适用且有足够表现机会，能力未体现或出现可观察的相反做法。
- 1：已有尝试，但仅部分体现能力，存在可观察的机械化、针对性不足或回应不到位。
- 2：在适用条件下，已有清晰且恰当的能力表现。
- 3：在该能力上进一步体现对当前情境或实际反馈的细致把握与恰当调整。

以上只是量尺骨架。必须将每档改写为本维度具体可判断的表现，
不能原样复制“较差、一般、较好、优秀”等通用评价词。
最高档不要求客户表示满意、接受约访或成交；也不强迫无须调整的对话制造调整动作。
无法为当前 Evidence 建立有意义档差时，在 notes 标明 rubric_pending，保留维度与证据，
不硬造细档。不要将未提供的公司规范、时限、权益或负面案例填入锚点。

每个 group 的 notes 使用下列带标签的字符串条目，使现有结构能够承载完整口径：
每条简洁，只为每个维度写一份口径，不为每个成员重复，也不复述整段对话。
遵守宿主窗口预算；需要分段写文件时保持完整合法 JSON，不截断或省略成员去向。

- `评分对象：`评价 HWP 的什么能力、采用哪些行为证据。
- `适用与缺证据：`什么情境适用，何时 N/A，何时 NE。
- `候选0分：`本维度的具体可观察表现与判定边界。
- `候选1分：`本维度的具体可观察表现。
- `候选2分：`本维度的具体可观察表现。
- `候选3分：`本维度的具体可观察表现。
- `评分步骤：`如何定位证据并选择有证据支持的档位；方法之间的替代关系及跨维度边界。
- `证据与校准：`哪些正向表现由当前成员支持，哪些档差是设计推导；
  所有档位均为候选，未作高低分对照验证，不宣称权重、效度或成交效果。

宿主量尺不同则按实际量尺列锚点。rubric_pending 时用说明替代无法可靠拟定的档位。
不同维度可同时得到同一片段支持，但应评价不同能力；
同一能力的同义动作不拆成多个维度重复计分。

### 归并交付

输出路径逐字取 window.output_path，相对 Compile.to：

```json
{
  "schema_version": "taikang.dimension-merge.v1",
  "window_id": "<当前window_id>",
  "groups": [
    {
      "member_refs": ["<实际成员>"],
      "name": "<可评价的能力维度>",
      "definition": "<评价对象、范围和边界>",
      "observable_behaviors": ["<可观察的能力指标，注明条件和替代路径>"],
      "applicability": {},
      "notes": ["<按上述标签逐条填写候选评分口径及证据边界>"]
    }
  ],
  "kept_separate": [{"member_ref": "<实际成员>", "reason": "<独立原因>"}],
  "deferred": [{"member_ref": "<实际成员>", "reason": "<待核实原因>"}],
  "notes": []
}
```

单一成员也可形成边界清楚的候选维度，标记 single_support；
若仅能描述特定操作而无法建立可评价的能力，先 kept_separate 或 deferred，并说明原因。
不为了减少数量把不同能力合成一个维度。
每个成员至少有一个明确去向。有意多重关联可以保留，须说明不同评价目标，避免重复计分。
不分配全局 dimension_id；既有目标只使用宿主实际提供的 existing_dimension_id。
不读取全部历史对话或兄弟窗口；当前结果只代表窗口内归纳，不能声称完成全库唯一化。
推断与事实分开。不确定时明确 deferred，不能丢掉成员或只返回总结。
