"""
Session-level execution helpers for the current team_skills.evolution pipeline.

The three skill-writing stages (evolve / create / merge) run as a controlled
agent loop (plan → act → submit) implemented in ``team_skills.evolution.agent``:

- merge same-name conflicts when two evolved versions collide
- evolve an existing skill from aggregated session evidence
- create a brand-new skill from no-skill session groups

This module keeps the stage prompts (Prompt Studio resolves its symbols here,
so overrides keep working) and thin wrappers that drive the loop runner with
per-stage call options and tracing.
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any, Optional

from team_skills.evolution.kernel.llm import AsyncLLMClient

logger = logging.getLogger(__name__)

# Shared prompt blocks. These are injected into the create/evolve system
# prompts via sentinel replacement at module load (see the bottom of the file).
# They are the two levers that keep evolved skills reusable:
#   1. force task-instance values to be parameterized (not hardcoded), and
#   2. force the skill body to state that explicit user requests win over the
#      skill's default conventions.

_GENERALIZATION_RULES = """\
## 强制要求：让 Skill 保持通用性 —— 这是最高优先级的规则

首先理解这里"通用"的含义，因为它决定了这个 Skill 是否有价值。

Skill 不是对你刚观察到的那个任务的记录。它是你在为"另一个人"写的建议——\
这个人未来的某个时刻会遇到同类任务，但具体细节完全不同：不同的输入文件、\
不同的目录、不同的日期、不同的公司或人物、不同的数字，甚至子任务数量也可能不同。\
如果你写下的任何内容对那个未来的人是错误的、令人困惑的或无用的，它就不该出现在 Skill 里。

因此写作过程中要不断自问："这对每一份同类任务都成立，还是只是我观察到的这一次恰好如此？"\
只有前者才配写进 Skill。

### 对你要写下的每一个具体细节做这项检验

对每个具体取值、名称、路径、数字、日期或固定结构，在心里运行这个检验：

  "如果下一个用户提出同类请求但输入不同，这个确切的取值仍然正确吗？"

- 如果对每一个这样的用户都是 YES（它是环境、工具或领域的稳定属性）→ 保留具体写法。\
这些稳定事实正是 Skill 的价值所在，不要含糊其辞地泛化掉。
- 如果是 NO——它会随请求而变化——那它只是本次任务的细节。不要把它写成答案本身，而是：
    1. 用一个命名占位符替代（例如一个含义清晰的 `{...}` 槽位），并且
    2. 用一句话说明如何从用户的实际请求或现有文件推导出真实取值。

### 几乎总是无法通过检验的内容类别（要泛化处理）

按类别思考，而不是记住单个例子：
- 东西在哪里：你读取的具体输入路径/文件名，以及你写出的具体输出路径/文件名。\
命名*规则*可以保留；字面实例不能保留。
- 是哪个实例：任何仅仅用于标记本次任务、数据集、运行或子问题的标识符。\
绝不能让这种标识进入 Skill 名称，也不能用它组织正文结构。
- 发生在何时：具体日期、时间戳、参考/基准日期。
- 关于谁/什么：具体的人、角色、层级、公司、产品、路线、地区——本次任务的主体。
- 数量多少：作为本次任务输入出现的数量、预算和阈值。（作为领域固定规则的数字可以保留；\
随本次请求而来的数字不能保留。）

### 一个隐蔽的陷阱：也不要固化所观察运行的"形状"

过拟合不只是字面取值的问题。如果你观察到的会话恰好包含若干子任务或若干报告类型，\
千万不要把 Skill 写成与你所见完全一致的固定清单"类型 1 做 X，类型 2 做 Y，类型 3 做 Z"。\
这份清单本身也是这一次运行的产物——未来的请求可能有不同的数量、不同的类型，\
或你从未见过的组合。正确做法是提炼底层的公共结构，以及"由输入决定形状"的规则\
（例如"每个分组维度一节，按主指标排序，末尾带合计行"），这样无论下一个用户带来\
多少种、哪些变体，同一个 Skill 都能产出正确结果。

### 名称与描述

- NAME 必须描述许多未来任务共享的"能力"，绝不能是单个实例或某个数据集/运行标签。
- DESCRIPTION 不得把 Skill 绑定到某个文件、某个日期、某个主体或某个固定枚举。\
危险信号：如果你的"NOT for"排除条款必须排除"其他文件 / 其他日期 / 非标准变体"，\
说明这个 Skill 已经过拟合——应当泛化它，而不是排除那些情况。

### 最终检查

- 对你引入的每一个占位符，必须同时说明如何从任务输入推导其真实取值。
- 完成泛化后，如果没有任何可复用的内容剩下——Skill 只是在复述某个任务的特定输入及其答案——\
那么这里不存在通用 Skill：选择 skip / 不要创建。"""

_USER_OVERRIDE_RULE = """\
## 强制要求：Skill 正文中必须包含用户优先级声明

Skill 编码的是从历史运行中提炼的默认约定——不是不可更改的法律。你产出的 Skill 正文\
必须包含一个简短、明确的声明段（2-4 行），说明：
- 这些默认约定仅在用户未另行指定时生效；
- 用户的任何明确要求——不同的输出路径、文件名、章节结构、格式、范围或内容——\
都优先于（OVERRIDES）Skill 的默认约定；
- 当用户请求与这里的默认约定冲突时，以用户为准。

该声明必须用与 Skill 正文其余部分相同的语言书写（中文正文配中文声明，英文正文配英文声明）。\
保持简短，只声明一次；不要把这个提示散落在文档各处。"""

_EVIDENCE_ROUTING_RULES = """\
## 强制要求：证据路由 —— 团队 Skill / 用户记忆 / 运行时问题

在选择动作之前，必须把每一条候选经验归入且只归入一个桶：

- `team_skill`：可复用的 SOP、稳定的环境事实、工具/领域操作规程，\
或对不同用户和任务实例都有用的护栏规则。
- `user_memory`：可归因于某个用户的偏好或习惯，例如视觉品味、偏好的格式、语气、\
排版、工作流，或重复出现的个人选择。
- `task_requirement`：仅针对当前交付物的明确要求或纠正。
- `agent_runtime`：中断、上下文丢失、重试行为、工具故障、编排失败，\
或 agent 未能遵循本身正确的指引。
- `insufficient_evidence`：与 Skill 没有可论证因果联系的观察。

只有 `team_skill` 证据才能修改共享 Skill。同一用户反复出现的偏好是更强的用户记忆证据，\
而不是团队 Skill 证据。用户的纠正并不自动意味着缺少一条 SOP：先判断它是一次性的任务要求、\
稳定的个人偏好，还是通用规程。

具有相同非空 `evaluation_profile` 的会话构成一个受控评估群组（cohort），其目的是学习\
共享的团队方法。当群组中两个或更多独立用户收敛到同一条基于产出物的纠正或验收规则时，\
若该规则符合 Skill 的定位，应将其归为 `team_skill`。不要仅仅因为这些规则是用户在纠正\
某个具体交付物时提出的，就降级为 `task_requirement`。

团队证据的最低要求：每条 `team_skill` 论断必须得到至少两个不同用户（`user_alias` 不同）\
的会话支持。只有一个用户观察支撑的经验——无论它看起来多合理——都应归入 `user_memory` \
或 `task_requirement`，而不是共享 Skill。例外：稳定的环境事实（端口、端点、schema 等）\
可以被单次会话确证，因为它们属于环境而非任何个人。

对群组公共规则要保留其操作层面的具体性。确切的叙事顺序、章节比例、元数据词汇表、\
design-token 约定、必需的说明/审计项、校验阈值等，只要在群组内被独立复现，\
就是可复用的 Skill 内容。不要把它们替换成"遵循要求的结构""检查相关 token"这类空洞建议——\
那会丢掉群组已经示范过的方法。主体、受众、文件名、单人视觉品味以及其他非复现细节\
保留在 `user_memory` 或 `task_requirement` 中。

用户明确要求的输出技术或格式，并不构成"覆盖该技术的 Skill 不相关"的证据。能力边界可以重叠；\
例如基于 HTML 的演示文稿完全可能用到前端视觉设计指引。只有当证据表明当前 Skill 的指引\
确实因果性地损害了结果、且替代路由持续改善了结果时，才添加 `NOT for` 排除条件。\
其他 Skill 的共现、注入或可用性本身不是因果证据。

把"PPT"、"slides"、"演示文稿"这类宽泛的用户表述理解为演示目标，而不是自动等同于\
原生 `.pptx` 文件契约。除非用户明确要求 PowerPoint/PPTX、原生幻灯片对象或可编辑的\
演示文件交付，或事后拒绝了 HTML，否则 HTML 幻灯片是有效实现。不要仅仅因为存在 PPT 专用\
Skill 就把 HTML 交付称为路由失败。

如果所有有用的观察都落在 `user_memory`、`task_requirement`、`agent_runtime` 或\
`insufficient_evidence` 中，选择 `skip`。不要把这些观察编码进共享 Skill。

每份输出都必须包含 `evidence_classification`，其中的数组名为 `team_skill`、\
`user_memory`、`task_requirement`、`agent_runtime`、`insufficient_evidence`。\
每个 `team_skill` 条目必须写明可复用的论断、支持的会话 ID，以及与拟议修改之间的因果联系。

保持这份分类紧凑：`team_skill` 最多 8 条，其他每个桶最多 4 条。每条论断、因果联系或\
字符串条目不超过 240 字符。不要在输出中复述会话摘要。rationale 不超过 600 字符，\
Skill 正文不超过 8000 字符。"""

_OUTPUT_LANGUAGE_RULE = """\
## 语言要求

- 所有分析性字段（`rationale`、`edit_summary`、`evidence_classification`，以及\
file_changes 的 `reason`）必须用中文书写。工具名、命令、路径、报错信息等专有名词\
可保留英文原文，但叙述语言必须是中文——不要在同一段落中英文混杂。
- Skill 的 description 与正文内部必须保持单一语言：改进既有 Skill 时沿用原 Skill \
的语言；新建 Skill 时默认使用中文（与团队既有 Skill 一致），除非会话证据明确表明\
应使用其他语言。"""

_MERGE_SKILL_SYSTEM = """\
你是 teamEvolver 的 Skill 工程师，工作在一个多轮循环中。

同一个 Skill 存在两个版本：因为两次独立的演化动作在相同名称下产生了不同内容。

你的任务：把两个版本合并成一个更优的版本，融合两者的精华。两个版本的全文都在\
第一条消息中给出；对照下面的合并原则完成自检后，一次性输出 final。

## 合并原则

- 保留两个版本中所有可操作的指引——不要丢弃有用内容。
- 消除冗余——对重叠章节去重。
- 如果两个版本相互矛盾，优先采用更具体、更明确的指引。
- 当一个版本硬编码了任务实例的取值（具体的输入/输出路径、文件名、数据集 ID、日期、\
实体或任务输入阈值），而另一个版本把它表达为参数/模式时，优先采用参数化形式——\
绝不能让合并结果重新引入硬编码的实例取值。
- 如果任一版本包含用户优先级声明段（明确用户要求覆盖 Skill 默认约定），合并后的 Skill\
必须保留一个这样的段落，并用合并后正文的语言书写。
- 保留更强的既有结构，除非重组明显更有利。
- 不要仅仅为了让版本看起来更"标准化"而重写。
- 保持名称不变。
- 合并后的 description 应涵盖两个版本的触发条件。
- 只保留对合并后 Skill 仍有帮助的 metadata 或额外 frontmatter。
- 合并后的内容应保持精炼，但不要强套僵硬的章节模板。

## 提交前自检

输出 final 之前逐项确认：
- 每个版本中可操作的指引要么被保留、要么被更优表达取代——没有静默丢失。
- 重叠章节已去重，不存在互相矛盾的并行规则。
- 未重新引入任务实例字面量（路径/文件名/数据集 ID/日期/实体）。
- 用户优先级声明段恰好保留一份，语言与正文一致。
- name 与输入一致；description 覆盖两个版本的触发条件。
- 输出的是"仅正文 content"，不是带 frontmatter 的完整 SKILL.md。
"""

_EVOLVE_FROM_SESSIONS_SYSTEM = """\
你是 teamEvolver Skill 演化系统的 Skill 工程师，工作在一个多轮循环中。

你会得到来自多个 agent 会话的证据，这些会话都涉及技能 ``{skill_name}``。\
每个会话包含程序化轨迹（逐步的工具调用及其结果）和一份 LLM 生成的分析。\
第 1 轮你会收到一份紧凑的会话索引卡（每会话一行）和当前 Skill 的大纲；\
需要完整内容时用工具按需加载（read_session / search_sessions / read_skill）。

你的任务：修改原始 Skill，使其能更好地为未来的运行压缩环境信息。\
把会话证据当作环境反馈，用于随时间持续打磨、验证和扩展该 Skill。

结合当前 Skill 内容分析会话证据，然后决定最佳行动：

如果存在 `active_candidate_feedback`，说明上一个候选版本已在 True Replay 中失败\
或结果不明确。只把它的轮次、工具调用和 token 变化作为反馈：保留既有行为，\
做出实质不同的针对性修复，并且不要返回与之前相同措辞的候选内容或编辑摘要。

1. **improve_skill** —— 需要根据会话证据对 Skill 正文做针对性修改（例如缺少指引、\
信息过时或指令不清晰）。用 propose_edits 工具提交一个有序的编辑列表，系统会把它\
机械地应用到当前正文；未涉及的正文保持逐字节不变。

2. **optimize_description** —— Skill 正文没问题，但 description 导致它被匹配到错误任务。\
只重写 description 使触发更精准。不要改动正文内容。

3. **create_skill** —— 会话证据揭示了一个反复出现的模式、能力缺口或可复用策略，\
但它不属于当前技能 ``{skill_name}``。需要另建一个全新、独立的 Skill。当前技能保持不变。\
只有当该模式与当前技能的定位明显不同、且无法通过改进当前技能解决时才选此项。

4. **skip** —— Skill 运转良好，或证据太弱/太模糊，不足以支持修改。无需任何动作。

## 编辑原则（针对 improve_skill）

- 把当前 Skill 当作事实基准，而不是等待重写的草稿。
- 先用 read_skill 工具读取当前正文，再看会话证据。
- 通过 propose_edits 工具提交精确锚定的 replace / insert_after 编辑——系统按字节应用编辑，\
任何未涉及的部分保持零字节差异。
- 如果多个会话都指向同一章节有误或不完整，就修改该章节。
- 如果失败只出现在边角情况，补上缺失的检查或澄清约束即可，不要改动无关章节。
- 保留原有的结构、标题顺序、术语和有效指引，尤其是被成功会话验证过的部分。
- 只有当证据表明某章节存在实质性错误时才重写整节（一个覆盖整节的大 replace 编辑）。
- 如果 Skill 中包含事实正确的具体 API 细节（端点、端口、payload schema、工具名称），\
即使 agent 用得不好也要保留。这些细节是 Skill 的核心价值。

## 硬性约束

- 不要随意更改任务 API 契约、端口、端点、输出路径、payload 格式或要求的文件名。\
这些是环境特定事实，默认应予保留。例外：如果会话证据清楚表明某个 API 端点、端口或契约\
已经变更，则更新 Skill 以反映修正后的值。
- 不要删除与所观测失败无关的核心能力、API 引用、命令模式或工具使用示例。
- 不要把 Skill 变成另一个用途不同的 Skill。
- 不要从头重写整个 Skill。
- 不要强加新模板、新的强制章节结构或不同的写作风格，除非证据要求这样做。
- 不要添加 agent 本应自行处理的通用最佳实践指引（例如限流处理、重试逻辑、状态管理或缓存）。\
只有当 Skill 所处的环境确有 agent 无法自行发现的特殊问题时才添加此类指引。

## 保守编辑模式

- 优先保留既有章节标题和顺序。
- 如果某个章节得到了成功会话的支持，除非失败证据明确与之矛盾，否则保持原样。
- 优先收紧或澄清既有章节，而不是新增全新章节。
- 除非失败证据很强且既有结构无法表达修复，否则不要引入新的大章节。
- 新增的过程性检查要简短，并与观测到的失败直接相关。

## 区分 Skill 问题与 agent 问题

并非每次失败都是 Skill 的缺陷。编辑之前，先判断失败由谁造成：
- **Skill**（错误、缺失或误导性的指引）→ 修改 Skill。
- **Agent**（子 agent 误用、不必要的重启、上下文溢出，或没有正确读取 Skill）→\
这些是 agent 层面的问题；不要把 agent 运行时建议塞进 Skill 使其膨胀。
- **环境**（mock API 不稳定、网络抖动、docker 怪癖）→ 如果多个会话显示反复的 API 失败\
或超时，可以简要注明这种不稳定性，让 agent 有所预期。保持简短；不要把 Skill 写成重试教程。

必须避免的关键反模式：如果 Skill 已经包含正确的环境信息（API 端点、端口、payload 格式、\
工具名称），而 agent 失败是因为没有使用这些信息，那是 AGENT 问题，不是 Skill 问题。\
不要从 Skill 中删除正确的 API 信息并替换成"去读 utils.py""检查 mock 服务代码"之类的指令。\
Skill 的全部意义就在于让 agent 免于自行发现这些细节。

拿不准时，宁可 **skip** 也不要做投机性修改。

__EVIDENCE_ROUTING_RULES__

## Bundle 文件变更

- 当前 Skill 可能包含 "Editable bundle files"（可编辑捆绑文件）章节；\
用 read_bundle_file 读取其全文。
- 只有当会话证据证明某个捆绑文件需要变更时，才在 final 的 `skill.file_changes` 中\
给出变更。不要为了风格而重写脚本。
- 支持的操作是 `upsert` 和 `delete`。
- `upsert` 必须包含完整的替换用 UTF-8 `content`。
- `delete` 必须指向可编辑章节中列出的既有文件。
- 每个操作都必须附上简明的、基于证据的 `reason`。提交时系统会做契约预校验，\
失败会作为观测反馈给你修正。
- 绝不要通过 file changes 指向 `SKILL.md`；应更新 `content` 字段。
- 未列在可编辑章节中的既有文件由服务保留，不得出现在 file changes 中。
- 当脚本接口变化时，同步更新 Skill 正文，让未来的 agent 正确调用新接口。

## Skill 写作原则（针对 create_skill）

- 新 Skill 的定位必须不同于 ``{skill_name}``。
- 名称优先选择简短、面向动作的 slug（小写连字符）。
- 名称必须与下方列出的所有既有技能名称不同。
- Skill 应压缩环境信息（API 端点、端口、payload 格式、工具特有的怪癖或领域操作规程），\
而不是 agent 已经掌握的通用最佳实践。
- description 应说明该 Skill 做什么及触发场景，包括"NOT for: ..."排除条件。2-4 句。
- 内容应领域特定、实际有用且非显而易见。
- 保持精炼、可复用、以证据为依据。
- 写可复用的指引，而不是失败总结或复盘报告。

以下两段适用于任何会写入 Skill 正文或 description 的动作（improve_skill 和 create_skill）。\
在改进既有 Skill 时，若正文中存在已固化的任务实例字面量（输入/输出路径、文件名、\
数据集 ID、日期、实体、任务输入阈值），用 replace 编辑把它们改写为下述占位符，\
这算作针对性修复，不属于被禁止的整体重写。同样，用 insert_after 添加所要求的\
用户优先级声明段也是允许且预期的编辑，即使在保守模式下也是如此。

__GENERALIZATION_RULES__

__USER_OVERRIDE_RULE__

__OUTPUT_LANGUAGE_RULE__
"""

_CREATE_FROM_SESSIONS_SYSTEM = """\
你是 teamEvolver 的 Skill 工程师，工作在一个多轮循环中。

你会得到若干 agent 会话的摘要，这些会话没有引用任何既有技能。\
这些会话可能揭示出可以沉淀为可复用 Skill 的模式，供未来会话使用。\
第 1 轮你会收到一份紧凑的会话索引卡（每会话一行）；需要完整内容时用工具按需加载\
（read_session / search_sessions）。可用 read_library_skill 查看既有技能全文以做\
差异化定位，用 check_name 校验新名称是否冲突。

请分析这些会话是否揭示了某种共同模式、反复出现的挑战或可复用策略，\
将其捕获为 Skill 后会让未来的 agent 会话受益。

1. **create_skill** —— 存在一个清晰的、可传授的模式，它压缩了 agent 无法可靠自行发现的\
环境特定知识。产出新 Skill。
2. **skip** —— 没有可操作或可泛化的模式。会话之间差异太大、过于领域特定，\
或问题本身不适合用 Skill 解决。

__EVIDENCE_ROUTING_RULES__

## Skill 写作原则（针对 create_skill）

- Skill 应压缩环境信息（API 端点、端口、payload 格式、工具特有的怪癖或领域操作规程），\
而不是 agent 已经掌握的通用最佳实践。
- 名称优先选择简短、面向动作的 slug（小写连字符）。
- description 应说明该 Skill 做什么及触发场景，包括"NOT for: ..."排除条件。2-4 句。
- 内容应领域特定、实际有用且非显而易见。
- 当 API 端点、端口、命令模式或 payload 示例是任务核心时，应包含它们的具体内容。
- 保持精炼、可复用、以证据为依据。
- 写可复用的指引，而不是失败总结或复盘报告。
- 使用祈使句式指令。按任务需要自然组织结构。
- 不要添加通用 agent 运行时建议（限流处理、重试逻辑、缓存策略或状态管理），\
除非环境确有需要这样做的特殊问题。

__GENERALIZATION_RULES__

__USER_OVERRIDE_RULE__

__OUTPUT_LANGUAGE_RULE__

## 何时选择 skip

出现以下情况时优先 skip：
- 失败由 agent 层面的问题造成（重试、上下文溢出或子 agent 误用），而不是缺少知识。
- 会话之间差异太大，无法提炼出一个连贯的 Skill。
- 该模式属于 agent 应凭通用智能处理的事项。

## 可选的 bundle 文件

只有当可复用能力确实需要可执行支撑时才创建捆绑文本文件。\
把操作放入 `skill.file_changes`，使用 `upsert`，返回完整的 UTF-8 内容，\
并附上基于证据的 reason。绝不要通过 file changes 创建或修改 `SKILL.md`。\
提交时系统会做契约预校验，失败会作为观测反馈给你修正。
"""


def _inject_shared_blocks(template: str) -> str:
    return (
        template.replace("__GENERALIZATION_RULES__", _GENERALIZATION_RULES)
        .replace("__USER_OVERRIDE_RULE__", _USER_OVERRIDE_RULE)
        .replace("__EVIDENCE_ROUTING_RULES__", _EVIDENCE_ROUTING_RULES)
        .replace("__OUTPUT_LANGUAGE_RULE__", _OUTPUT_LANGUAGE_RULE)
    )


# Expand the shared generalization / user-precedence blocks into the prompts
# that write skill content. Done once at import time.
_EVOLVE_FROM_SESSIONS_SYSTEM = _inject_shared_blocks(_EVOLVE_FROM_SESSIONS_SYSTEM)
_CREATE_FROM_SESSIONS_SYSTEM = _inject_shared_blocks(_CREATE_FROM_SESSIONS_SYSTEM)

from contextvars import ContextVar

_EVOLVE_DEBUG_DIR: ContextVar[str] = ContextVar("evolve_debug_dir", default="")


def _effective_system(stage_id: str, fallback: str) -> str:
    """Return a stage's system prompt, honoring any Prompt Studio override.

    Lazy import avoids a circular import (prompt_studio imports this module for
    its default resolvers). Falls back to the in-module default so the pipeline
    is byte-identical when no override is stored.
    """
    try:
        from team_skills.evolution.prompt_studio import effective_prompt

        return effective_prompt(stage_id, fallback)
    except Exception:  # noqa: BLE001 - never let studio wiring break the pipeline
        return fallback


def _stage_call_options(
    stage_id: str,
    *,
    max_tokens: int,
    temperature: float,
) -> dict[str, Any]:
    try:
        from team_skills.evolution.prompt_studio import stage_call_options

        return stage_call_options(stage_id)
    except Exception:  # noqa: BLE001 - retain stable stage defaults
        return {"max_tokens": max_tokens, "temperature": temperature}


def set_evolve_debug_dir(path: str) -> None:
    """Set the debug dump directory used by session-level evolution calls."""
    _EVOLVE_DEBUG_DIR.set(str(path or "").strip())


def _get_evolve_debug_dir() -> str:
    return _EVOLVE_DEBUG_DIR.get()


# Loop budgets for the agent stages. Overridable by the orchestrator (which
# owns the engine config) via set_evolve_agent_limits; stage functions read
# the current values at call time so tests can monkeypatch the dict.
_AGENT_LIMITS: dict[str, int] = {
    "max_rounds": 12,
    "max_tool_calls_per_round": 8,
}
_TASK_AGENT_LIMITS: ContextVar[dict | None] = ContextVar("evolve_agent_limits", default=None)


def set_evolve_agent_limits(max_rounds: int, max_tool_calls_per_round: int) -> None:
    """Configure the agent-loop budgets used by the three skill stages."""
    _TASK_AGENT_LIMITS.set({
        "max_rounds": max(2, int(max_rounds or 12)),
        "max_tool_calls_per_round": max(1, int(max_tool_calls_per_round or 8)),
    })


def _agent_limits() -> dict:
    return _TASK_AGENT_LIMITS.get() or _AGENT_LIMITS


# Bundle contract used for submit-time file_changes pre-validation in the
# evolve stage. Defaults mirror EvolveServerConfig; the orchestrator pushes
# the live engine config here at startup (same pattern as the debug dir).
_BUNDLE_CONTRACT: dict[str, Any] = {
    "extensions": [".py", ".sh"],
    "max_file_bytes": 262144,
    "allow_delete": True,
}
_TASK_BUNDLE_CONTRACT: ContextVar[dict | None] = ContextVar("evolve_bundle_contract", default=None)


def set_evolve_bundle_contract(
    extensions: list[str], max_file_bytes: int, allow_delete: bool
) -> None:
    normalized = [
        f".{str(ext or '').strip().lower().lstrip('.')}"
        for ext in (extensions or [])
        if str(ext or '').strip().lstrip('.')
    ]
    _TASK_BUNDLE_CONTRACT.set({
        "extensions": normalized or [".py", ".sh"],
        "max_file_bytes": max(1, int(max_file_bytes or 262144)),
        "allow_delete": bool(allow_delete),
    })


def get_evolve_bundle_contract() -> dict[str, Any]:
    return dict(_TASK_BUNDLE_CONTRACT.get() or _BUNDLE_CONTRACT)


async def execute_merge(
    llm: AsyncLLMClient,
    existing_skill: dict,
    incoming_skill: dict,
) -> Optional[dict]:
    """Merge two versions of the same skill into one superior version."""
    from team_skills.evolution.agent.runner import run_skill_agent

    skill_name = str(existing_skill.get("name") or "")
    return await run_skill_agent(
        llm,
        stage="merge",
        system_prompt=_effective_system("merge", _MERGE_SKILL_SYSTEM),
        merge_versions=(existing_skill, incoming_skill),
        max_rounds=_agent_limits()["max_rounds"],
        max_tool_calls_per_round=_agent_limits()["max_tool_calls_per_round"],
        call_options=_stage_call_options("merge", max_tokens=8192, temperature=0.3),
        trace_kwargs=_llm_trace_kwargs("merge_skill", skill_name=skill_name),
        debug_stem=(skill_name or "merge_skill").replace("/", "_"),
    )


def _build_session_evidence(sessions: list[dict], max_sessions: int = 60) -> str:
    """Format session evidence (trajectory + summary) for LLM prompts."""
    blocks: list[str] = []
    for session in sessions[:max_sessions]:
        session_id = session.get("session_id", "?")
        avg_prm = session.get("_avg_prm")
        prm_str = f", avg PRM: {avg_prm}" if avg_prm is not None else ""
        has_errors = session.get("_has_tool_errors", False)
        err_str = ", has tool errors" if has_errors else ""
        skills = session.get("_skills_referenced") or set()
        skill_str = f", skills: {sorted(skills)}" if skills else ""
        runtime_context = (
            session.get("runtime_context")
            if isinstance(session.get("runtime_context"), dict)
            else {}
        )
        evaluation_profile = str(
            runtime_context.get("evaluation_profile")
            or session.get("_evaluation_profile")
            or ""
        ).strip()
        profile_str = (
            f", evaluation_profile: {evaluation_profile}"
            if evaluation_profile
            else ""
        )

        aggregate = session.get("aggregate") or {}
        aggregate_str = ""
        if aggregate:
            parts: list[str] = []
            rollout_count = aggregate.get("rollout_count", 0)
            mean_score = aggregate.get("mean_score")
            stability = aggregate.get("stability", "")
            success_count = aggregate.get("success_count", 0)
            fail_count = aggregate.get("fail_count", 0)
            if rollout_count:
                parts.append(f"{rollout_count} rollouts")
            if mean_score is not None:
                parts.append(f"mean ORM={mean_score:.3f}")
            if success_count or fail_count:
                parts.append(f"success={success_count} fail={fail_count}")
            if stability:
                parts.append(f"stability={stability}")
            if parts:
                aggregate_str = f", {', '.join(parts)}"

        trajectory = session.get("_trajectory", "")
        summary = session.get("_summary", "")

        # Evolution evidence flag: tells the model whether this session is a
        # badcase to fix (defect) or a goodcase to reinforce (exemplary).
        judge_scores = (
            session.get("_judge_scores")
            if isinstance(session.get("_judge_scores"), dict)
            else {}
        )
        evidence_kind = str(judge_scores.get("evolution_evidence") or "").strip().lower()
        evidence_str = ""
        if evidence_kind in {"defect", "exemplary"}:
            evidence_reason = str(judge_scores.get("evidence_reason") or "").strip()
            evidence_str = (
                f", evolution evidence: {evidence_kind}"
                + (f" — {evidence_reason}" if evidence_reason else "")
            )

        parts = [
            f"### Session {session_id}{prm_str}{aggregate_str}{err_str}"
            f"{skill_str}{profile_str}{evidence_str}"
        ]
        if trajectory:
            parts.append(f"**Trajectory**:\n{trajectory}")
        if summary:
            parts.append(f"**Analysis**:\n{summary}")
        if not trajectory and not summary:
            parts.append("(no data)")
        blocks.append("\n\n".join(parts))

    if len(sessions) > max_sessions:
        blocks.append(f"\n... and {len(sessions) - max_sessions} more sessions")

    return "\n\n---\n\n".join(blocks)


def _build_cross_cycle_context(context: Optional[dict]) -> str:
    if not isinstance(context, dict) or not context:
        return ""
    debt = context.get("change_debt") if isinstance(context.get("change_debt"), dict) else {}
    reconsider = bool(debt.get("reconsideration_ready"))
    guidance = (
        "Repeated independent evidence has crossed the reconsideration threshold. "
        "Do not skip merely because any single session is weak; resolve the repeated "
        "signal with a targeted edit or explain why the accumulated evidence conflicts."
        if reconsider
        else (
            "Use recent evidence for responsiveness and historical evidence as a "
            "regression guard. A skip keeps the unresolved evidence for later cycles."
        )
    )
    payload = {
        "total_evidence_sessions": context.get("total_evidence_sessions"),
        "current_session_count": context.get("current_session_count"),
        "recent_session_ids": context.get("recent_session_ids") or [],
        "historical_session_ids": context.get("historical_session_ids") or [],
        "tool_error_sessions": context.get("tool_error_sessions"),
        "defect_evidence_sessions": context.get("defect_evidence_sessions"),
        "exemplary_evidence_sessions": context.get("exemplary_evidence_sessions"),
        "mean_judge_score": context.get("mean_judge_score"),
        "change_debt": debt,
        "active_candidate_job_id": context.get("active_candidate_job_id") or "",
        "active_candidate_feedback": (
            context.get("active_candidate_feedback")
            if isinstance(context.get("active_candidate_feedback"), dict)
            else {}
        ),
    }
    return (
        "## Cross-cycle evidence state\n\n"
        f"{json.dumps(payload, ensure_ascii=False, indent=2)}\n\n"
        f"Evolution guidance: {guidance}\n\n"
    )


def _build_evaluation_cohort_context(sessions: list[dict]) -> str:
    cohorts: dict[str, list[str]] = {}
    for session in sessions:
        runtime_context = (
            session.get("runtime_context")
            if isinstance(session.get("runtime_context"), dict)
            else {}
        )
        profile = str(
            runtime_context.get("evaluation_profile")
            or session.get("_evaluation_profile")
            or ""
        ).strip()
        session_id = str(session.get("session_id") or "").strip()
        if profile and session_id and session_id not in cohorts.setdefault(profile, []):
            cohorts[profile].append(session_id)
    controlled = {
        profile: session_ids
        for profile, session_ids in cohorts.items()
        if len(session_ids) >= 2
    }
    if not controlled:
        return ""
    return (
        "## Controlled evaluation cohorts\n\n"
        f"{json.dumps(controlled, ensure_ascii=False, indent=2)}\n\n"
        "Rules independently repeated inside one listed cohort are the intended "
        "team method. Preserve those rules concretely in the candidate Skill; "
        "only per-user and per-task details should be generalized away.\n\n"
    )


def _write_debug_dump(stem: str, system: str, user_msg: str, raw: str | None = None) -> None:
    debug_dir = _get_evolve_debug_dir()
    if not debug_dir:
        return

    dump_dir = Path(debug_dir)
    dump_dir.mkdir(parents=True, exist_ok=True)
    (dump_dir / f"{stem}_system.txt").write_text(system, encoding="utf-8")
    (dump_dir / f"{stem}_user.txt").write_text(user_msg, encoding="utf-8")
    if raw is not None:
        (dump_dir / f"{stem}_raw_output.txt").write_text(raw, encoding="utf-8")
    logger.info("[DebugDump] wrote %s prompt artifacts to %s", stem, dump_dir)


def _llm_trace_kwargs(operation: str, *, skill_name: str = "", sessions: list[dict] | None = None) -> dict:
    run_id = os.environ.get("EVOBENCH_RUN_ID", "").strip()
    session_ids = [
        str(session.get("session_id") or "").strip()
        for session in (sessions or [])
        if str(session.get("session_id") or "").strip()
    ]
    tags = ["team-skill-evolver", "team_skills.evolution", operation]
    if run_id:
        tags.append(run_id)
    if skill_name:
        tags.append(f"skill:{skill_name}")
    tags.extend(session_ids[:20])
    metadata = {
        "source": "team-skill-evolver",
        "component": "team_skills.evolution",
        "operation": operation,
        "skill_name": skill_name or None,
        "evobench_run_id": run_id or None,
        "session_count": len(sessions or []),
        "session_ids": session_ids[:50],
    }
    return {
        "trace_name": f"team-skill-evolver:{operation}" + (f":{skill_name}" if skill_name else ""),
        "trace_tags": tags,
        "trace_metadata": metadata,
        "trace_session_id": f"team-skill-evolver:{run_id}:{operation}:{skill_name or 'no-skill'}" if run_id else "",
        "trace_user_id": run_id,
    }


async def evolve_skill_from_sessions(
    llm: AsyncLLMClient,
    skill_name: str,
    sessions: list[dict],
    current_skill: Optional[dict],
    existing_skill_names: list[str],
    *,
    evolution_context: Optional[dict] = None,
) -> Optional[dict]:
    """Agent-loop decision + execution for one existing-skill session group."""
    from team_skills.evolution.agent.runner import run_skill_agent

    return await run_skill_agent(
        llm,
        stage="evolve_skill",
        system_prompt=_effective_system("evolve_skill", _EVOLVE_FROM_SESSIONS_SYSTEM),
        skill_name=skill_name,
        sessions=sessions,
        current_skill=current_skill,
        existing_skill_names=existing_skill_names,
        evolution_context=evolution_context,
        max_rounds=_agent_limits()["max_rounds"],
        max_tool_calls_per_round=_agent_limits()["max_tool_calls_per_round"],
        call_options=_stage_call_options(
            "evolve_skill",
            max_tokens=16384,
            temperature=0.4,
        ),
        trace_kwargs=_llm_trace_kwargs(
            "evolve_skill", skill_name=skill_name, sessions=sessions
        ),
        bundle_contract=get_evolve_bundle_contract(),
        debug_stem=skill_name.replace("/", "_"),
    )


async def create_skill_from_sessions(
    llm: AsyncLLMClient,
    sessions: list[dict],
    existing_skill_names: list[str],
    *,
    evolution_context: Optional[dict] = None,
    library_reader: Optional[Any] = None,
) -> Optional[dict]:
    """Agent-loop decision + execution for the no-skill session bucket.

    ``library_reader`` is an optional async ``name -> SKILL.md text``
    callable the orchestrator wires to shared storage so the loop's
    read_library_skill tool can differentiate against existing skills.
    """
    from team_skills.evolution.agent.runner import run_skill_agent

    return await run_skill_agent(
        llm,
        stage="create_skill",
        system_prompt=_effective_system("create_skill", _CREATE_FROM_SESSIONS_SYSTEM),
        sessions=sessions,
        existing_skill_names=existing_skill_names,
        evolution_context=evolution_context,
        library_reader=library_reader,
        max_rounds=_agent_limits()["max_rounds"],
        max_tool_calls_per_round=_agent_limits()["max_tool_calls_per_round"],
        call_options=_stage_call_options(
            "create_skill",
            max_tokens=16384,
            temperature=0.4,
        ),
        trace_kwargs=_llm_trace_kwargs("create_skill", sessions=sessions),
        debug_stem="no_skill",
    )


def _parse_evolve_result(raw: str, skill_name: str) -> Optional[dict]:
    """Back-compat shim: one-shot parse + legacy suppression via the contract."""
    from team_skills.evolution.agent.contract import legacy_suppress, parse_turn

    turn = parse_turn(raw, allow_repair=True)
    if not turn.get("ok"):
        logger.warning("[SessionExec] failed to parse evolve result for '%s'", skill_name)
        return None
    decision = turn.get("decision") if turn.get("type") == "final" else None
    return legacy_suppress(decision, skill_name)
