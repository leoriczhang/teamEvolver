# Prompt Studio 指南

Prompt Studio 是 teamEvolver 提供的白盒化工具，将技能进化流水线从黑盒变为可检查、可编辑、可测试的透明系统。通过 Prompt Studio，你可以查看每个进化阶段的系统 Prompt、修改它们、使用真实会话测试效果，并独立调整每个阶段的模型参数。

后端逻辑位于 `teamEvolver/evolve/prompt_studio.py`，前端视图位于 `web-ui/src/views/PromptStudioView.tsx`。

## 设计理念

历史上，进化流水线中各 LLM 阶段的系统 Prompt 是硬编码在 Python 模块中的常量，对控制台用户来说是不可见的黑盒。Prompt Studio 将这些 Prompt 提升为一等公民：

- **可检查**：查看每个阶段实际使用的 System Prompt（包括默认值和你已保存的覆盖）
- **可编辑**：直接在线编辑 Prompt，编辑后立即生效
- **可测试**：选择真实历史会话，用当前编辑的 Prompt 运行测试，看到 System Message、User Message 和模型输出
- **可调节**：每个阶段可独立配置 temperature、max_tokens，甚至指定不同的模型
- **可回滚**：一键重置回代码内置的默认 Prompt
- **版本持久化**：Prompt 覆盖存储在 `~/.teamEvolver/prompt_overrides.json`，阶段模型设置存储在 `~/.teamEvolver/stage_settings.json`

覆盖文件路径可通过环境变量自定义：
- `TEAMEVOLVER_PROMPT_OVERRIDES_PATH`
- `TEAMEVOLVER_STAGE_SETTINGS_PATH`

## 流水线阶段

进化流水线共定义 11 个阶段节点（其中 8 个是 LLM 调用阶段，拥有可编辑 Prompt）：

### 完整阶段图

```
ingest → session_filter → summarize → judge → group → ┬→ evolve_skill → ┬→ merge → dataset_synthesis → validate → replay_checklist → publish
                                                        └→ create_skill ─┘
```

| 阶段 ID | 类型 | 可编辑 Prompt | 说明 |
|---------|------|--------------|------|
| `ingest` | IO | 否 | 会话入队，无 LLM 调用 |
| `session_filter` | LLM | 是 | 价值分类，判断会话属于技能证据、用户记忆、普通任务还是闲聊 |
| `summarize` | LLM | 是 | 会话总结，构建无损轨迹并生成轨迹感知摘要 |
| `judge` | LLM | 是 | 会话评分，对缺少可靠分数的会话补打多维度分 |
| `group` | 逻辑 | 否 | 按技能分组，无 LLM 调用 |
| `evolve_skill` | LLM | 是 | 改进技能，对已有技能决定 improve/optimize_description/create/skip |
| `create_skill` | LLM | 是 | 新建技能，从 no-skill 桶识别可复用模式并生成新技能 |
| `merge` | LLM | 是 | 冲突合并，合并同名技能的两个进化版本 |
| `dataset_synthesis` | LLM | 是 | 测试集生成，生成带 Checklist 的渐进式测试数据集 |
| `validate` | 门禁 | 否 | 真回放校验，非 LLM 逻辑 |
| `replay_checklist` | LLM | 是 | Checklist 裁判，逐条核验回放结果是否满足 Checklist |
| `publish` | IO | 否 | 发布，写入技能库并同步云端 |

### 各 LLM 阶段详情

#### session_filter（价值分类）

- **模块**：`teamEvolver.session_filter`
- **符号**：`_SESSION_CLASSIFIER_SYSTEM`
- **默认 Temperature**：0.0
- **默认 Max Tokens**：512
- **输入变量**：session summary（requests, tools, interactions, metrics）
- **注入共享块**：否
- **说明**：决定会话是否进入进化队列，并区分团队 Skill 证据与用户 Memory 候选。这是流水线的第一道门，高温度可能导致分类不稳定，建议保持低 temperature。

#### summarize（会话总结）

- **模块**：`teamEvolver.evolve.stages.summarize`
- **符号**：`_SUMMARIZE_SESSION_SYSTEM`
- **默认 Temperature**：0.2
- **默认 Max Tokens**：100000
- **输入变量**：session JSON（interactions, tool calls, scores）
- **注入共享块**：否
- **说明**：对单个会话生成轨迹感知分析摘要，供后续评分与进化使用。需要大 token 上限以处理长会话。

#### judge（会话评分）

- **模块**：`teamEvolver.evolve.stages.judge`
- **符号**：`_JUDGE_SYSTEM`
- **默认 Temperature**：0.1
- **默认 Max Tokens**：32768
- **输入变量**：session payload（trajectory, summary, artifacts, prior scores）
- **注入共享块**：否
- **说明**：对缺少可靠分数的会话补打分，输出 JSON 维度分。低 temperature 保证评分一致性。

#### evolve_skill（改进技能）

- **模块**：`teamEvolver.evolve.stages.execute`
- **符号**：`_EVOLVE_FROM_SESSIONS_SYSTEM`
- **默认 Temperature**：0.4
- **默认 Max Tokens**：16384
- **输入变量**：`{skill_name}`、current skill block、cross-cycle evidence、evaluation cohort、session evidence、existing skill names
- **注入共享块**：是
- **说明**：这是核心进化阶段。基于会话证据对已有技能做出改进决策。注意：原始 Prompt 模板中包含三个 sentinel 占位符（`__GENERALIZATION_RULES__`、`__USER_OVERRIDE_RULE__`、`__EVIDENCE_ROUTING_RULES__`），保存覆盖时保留这些占位符即可，运行时会自动注入共享规则块。

#### create_skill（新建技能）

- **模块**：`teamEvolver.evolve.stages.execute`
- **符号**：`_CREATE_FROM_SESSIONS_SYSTEM`
- **默认 Temperature**：0.4
- **默认 Max Tokens**：16384
- **输入变量**：cross-cycle evidence、evaluation cohort、session evidence、existing skill names
- **注入共享块**：是
- **说明**：从无技能匹配的会话桶中识别可复用模式并生成全新技能。同样包含共享块占位符。

#### merge（冲突合并）

- **模块**：`teamEvolver.evolve.stages.execute`
- **符号**：`_MERGE_SKILL_SYSTEM`
- **默认 Temperature**：0.3
- **默认 Max Tokens**：8192
- **输入变量**：Version A（现有技能）、Version B（新进化版本）
- **注入共享块**：否
- **说明**：当同名技能产生两个冲突的进化版本时，将它们合并为一个更优版本。

#### dataset_synthesis（测试集生成）

- **模块**：`teamEvolver.dataset_synthesizer`
- **符号**：`_SYNTHESIZE_SYSTEM`
- **默认 Temperature**：0.3
- **默认 Max Tokens**：16384
- **输入变量**：`{case_count}`、`{min_requirements}`、`{max_requirements}`、candidate Skill、Session trajectories、team SOP evidence、replay seeds
- **注入共享块**：否
- **说明**：从会话证据和跨周期 SOP 证据同步生成带 Checklist 的渐进式测试数据集。模板变量 `{case_count}` 等会在运行时被实际值替换。

#### replay_checklist（Checklist 裁判）

- **模块**：`teamEvolver.true_replay`
- **符号**：`_CHECKLIST_JUDGE_SYSTEM`
- **默认 Temperature**：0.0
- **默认 Max Tokens**：8192
- **输入变量**：checklist、interactions、tool trajectory、workspace artifacts
- **注入共享块**：否
- **说明**：在真回放完成后，逐条核验 Checklist 项是否满足。裁判只允许依据可观察证据判定，temperature=0 保证裁决一致性。

## Prompt Studio Web 界面

在 Web 控制台左侧导航中选择 "Prompt Studio" 进入。界面分为三个主要区域：

### 左侧：流水线可视化

- 展示完整的有向图，节点按类型着色：
  - 灰色：IO 节点（ingest、publish）
  - 蓝色：LLM 阶段（可编辑 Prompt）
  - 紫色：逻辑节点（group）
  - 琥珀色：校验门禁（validate）
- LLM 节点上会显示角标：
  - "已覆盖"：该阶段有用户保存的 Prompt 覆盖
  - "参数已修改"：该阶段的模型参数与默认值不同
- 点击节点选中对应阶段
- 节点之间的箭头显示数据流向

### 中间：Prompt 列表 + 编辑器

- **阶段列表**：显示所有 8 个 LLM 阶段，带简短描述
- **提示词编辑器**：选中阶段后显示：
  - 阶段名称和描述
  - 当前生效的 Prompt 文本框（可编辑）
  - "显示默认值"开关：切换查看代码内置默认 Prompt vs 当前生效 Prompt
  - 如果该阶段注入共享块，会显示三个共享块内容：
    - `__GENERALIZATION_RULES__`：泛化规则
    - `__USER_OVERRIDE_RULE__`：用户覆盖规则
    - `__EVIDENCE_ROUTING_RULES__`：证据路由规则
  - 保存按钮（仅管理员可见）
  - 重置按钮（恢复默认值，仅管理员可见）

### 右侧：模型参数 + 测试面板

- **模型参数区域**：
  - Model：指定该阶段使用的模型（留空使用全局 `llm.model_id`）
  - Temperature：0.0-2.0 滑块
  - Max Tokens：数字输入
  - 显示默认值参考
  - 重置按钮恢复默认参数

- **测试面板**：
  - 会话选择器：从最近 50 条历史会话中选择一个作为测试输入
  - "运行测试"按钮
  - 测试结果分三个标签页展示：
    - **System Prompt**：本次测试实际发送的系统消息（编辑后的 Prompt + 展开的共享块）
    - **User Message**：测试运行时构建的真实用户消息（使用与会话阶段相同的构建逻辑）
    - **Model Output**：LLM 返回的输出内容

- **进化过程参数**（底部，可折叠）：
  - 可编辑 evolve 节和 validation 节的核心参数
  - 保存时一并更新

## 如何使用 Prompt Studio

### 工作流：编辑并测试 Prompt

推荐的迭代流程：

1. **选择阶段**：在流水线图或列表中点击要修改的阶段
2. **理解默认 Prompt**：打开"显示默认值"阅读默认 Prompt，理解其设计意图
3. **小步修改**：在编辑器中做增量修改，不要一次性大改
4. **选择测试会话**：从右侧选择一个有代表性的历史会话
5. **运行测试**：点击"运行测试"，查看三栏结果
   - System Prompt 是否符合预期（共享块是否正确展开）
   - User Message 是否是真实阶段会收到的格式
   - Model Output 质量是否达到要求
6. **对比**：可以记录默认 Prompt 的输出，与修改后对比
7. **保存**：测试满意后点击保存。保存后下一轮进化立即使用新 Prompt
8. **观察**：在 Dashboard 和 Evolution Pipeline 视图观察后续进化轮次的效果

### 测试面板的真实性

Prompt Studio 的测试不是"模拟"——它使用与真实进化流水线完全相同的消息构建逻辑：

- `session_filter`：使用 `_session_summary()` 构建与真实分类器完全一致的输入
- `summarize`：使用 `_build_session_payload()` 构建真实载荷
- `judge`：确保 `_trajectory` 和 `_summary` 元数据存在后调用 `_build_judge_payload()`
- `evolve_skill`/`create_skill`：调用 `_build_session_evidence()` 构建真实证据块
- `merge`：提供示例 A/B 版本（因为需要两个冲突版本）
- `dataset_synthesis`：使用 `render_synthesis_prompt()` 渲染
- `replay_checklist`：构建示例 checklist 和交互记录

这意味着你在测试面板看到的 User Message 就是真实进化时 LLM 收到的消息，测试结果可靠反映实际行为。

### 模型参数调优建议

不同阶段适合不同的参数配置：

| 阶段 | Temperature 建议 | Max Tokens 建议 | 理由 |
|------|-----------------|-----------------|------|
| session_filter | 0.0-0.1 | 512 | 分类任务需要确定性 |
| summarize | 0.1-0.3 | 100000+ | 摘要需要覆盖完整信息 |
| judge | 0.0-0.2 | 32768 | 评分需要一致性 |
| evolve_skill | 0.3-0.6 | 16384 | 创造性改进需要适度随机性 |
| create_skill | 0.3-0.6 | 16384 | 新技能创建需要创造性 |
| merge | 0.2-0.4 | 8192 | 合并以融合为主，不需要太高随机性 |
| dataset_synthesis | 0.2-0.4 | 16384 | 出题需要多样性但不能发散 |
| replay_checklist | 0.0 | 8192 | 严格的证据裁决必须确定 |

如果某个阶段需要更强的模型（如创建复杂技能），可以为该阶段单独指定更强的模型 ID，其他阶段使用默认模型以平衡成本和速度。

## 共享块说明

`evolve_skill` 和 `create_skill` 两个技能编写阶段的 Prompt 中包含三个特殊占位符，这些占位符在 Prompt 生效时会被自动替换为共享规则块。编辑这些阶段的 Prompt 时，请保留这些占位符：

- `__GENERALIZATION_RULES__`：泛化规则，控制技能从具体会话抽象为可复用 SOP 的程度
- `__USER_OVERRIDE_RULE__`：用户 override 规则，处理用户手动修改过的技能
- `__EVIDENCE_ROUTING_RULES__`：证据路由规则，控制如何引用和呈现会话证据

共享块的实际内容定义在 `teamEvolver/evolve/stages/execute.py` 中，通过 `_inject_shared_blocks()` 函数在运行时注入。在 Prompt Studio 界面查看阶段详情时，可以看到三个共享块的当前内容。

如果你删除了这些占位符，共享规则将不会被注入，可能导致进化行为异常。重置回默认 Prompt 可以恢复占位符。

## 变量占位符

部分 Prompt 模板包含运行时变量占位符，采用 `{variable_name}` 语法。编辑时请保留这些占位符：

| 阶段 | 占位符 | 运行时值 |
|------|--------|---------|
| evolve_skill | `{skill_name}` | 正在进化的技能名称 |
| dataset_synthesis | `{case_count}` | 生成测试用例数量 |
| dataset_synthesis | `{min_requirements}` | 最少检查项数量 |
| dataset_synthesis | `{max_requirements}` | 最多检查项数量 |

## 权限控制

- **管理员（admin）**：可以编辑 Prompt、修改模型参数、保存、重置
- **普通用户（user）**：可以查看流水线图、查看 Prompt 内容、运行测试，但不能保存修改

## Prompt 版本管理

当前版本的 Prompt Studio 采用简单的覆盖机制：

- 保存覆盖时写入 `prompt_overrides.json`
- 重置时删除对应条目，恢复默认
- 修改立即生效于下一次 LLM 调用
- 配置文件是文本格式（JSON），可以手动备份或纳入版本控制

建议在重大修改前手动备份覆盖文件：

```bash
cp ~/.teamEvolver/prompt_overrides.json ~/.teamEvolver/prompt_overrides.json.bak
cp ~/.teamEvolver/stage_settings.json ~/.teamEvolver/stage_settings.json.bak
```

## API 端点

Prompt Studio 的 HTTP API 位于 `/api/prompt-studio/`：

| 端点 | 方法 | 说明 |
|------|------|------|
| `/api/prompt-studio/pipeline` | GET | 获取带覆盖标记的流水线图 |
| `/api/prompt-studio/prompts` | GET | 获取所有 Prompt 摘要列表 |
| `/api/prompt-studio/prompts/<stage_id>` | GET | 获取单个阶段完整详情 |
| `/api/prompt-studio/prompts/<stage_id>` | POST | 保存 Prompt 覆盖和模型参数 |
| `/api/prompt-studio/prompts/<stage_id>/test` | POST | 运行测试 |
| `/api/prompt-studio/prompts/<stage_id>/reset` | POST | 重置为默认 Prompt |
| `/api/prompt-studio/sessions` | GET | 获取测试用会话列表 |

测试接口请求体：

```json
{
  "session_id": "abc-123",
  "system_prompt": "(可选) 临时测试用 Prompt，不保存"
}
```

如果 `system_prompt` 不为空，测试使用临时 Prompt（便于快速试错）；为空则使用当前保存的生效 Prompt。

## Python API

如需在脚本或 Notebook 中操作 Prompt Studio，可直接调用 `teamEvolver/evolve/prompt_studio.py` 中的函数：

```python
from teamEvolver.evolve.prompt_studio import (
    list_prompts,
    get_prompt,
    set_override,
    reset_override,
    effective_prompt,
    set_stage_settings,
    reset_stage_settings,
    stage_call_options,
    pipeline_graph,
    run_stage_test,
)

# 列出所有可编辑 Prompt
prompts = list_prompts()

# 获取某个阶段详情
detail = get_prompt("evolve_skill")
print(detail["default_prompt"])  # 默认 Prompt
print(detail["effective_prompt"])  # 当前生效 Prompt
print(detail["shared_blocks"])  # 共享块（仅注入阶段有）

# 设置覆盖
set_override("judge", "你的自定义 Prompt 文本...")

# 重置
reset_override("judge")

# 获取运行时应该使用的 Prompt（调用点使用）
prompt = effective_prompt("summarize", fallback=default_in_module)

# 设置模型参数
set_stage_settings("evolve_skill", {
    "temperature": 0.5,
    "max_tokens": 32768,
    "model": "doubao-seed-evolving-large",
})

# 获取阶段调用选项
options = stage_call_options("evolve_skill")
# {"temperature": 0.5, "max_tokens": 32768, "model": "doubao-seed-evolving-large"}

# 获取流水线图
graph = pipeline_graph()
```

## 与白盒配置的关系

Prompt Studio 与全局进化参数（evolve 节）是互补关系：

- **Prompt Studio** 控制每个阶段的 LLM Prompt 和模型采样参数，是"如何思考"层面的配置
- **evolve 节配置** 控制进化流程的宏观参数：
  - 进化间隔（`interval_seconds`）
  - 发布模式（`publish_mode`）
  - 证据库大小（`evidence_max_entries`）
  - 测试集参数（`dataset_test_cases`、`dataset_min_requirements` 等）
  - 人工审核设置（`human_review_enabled`）
  - 校验门槛（`validation_max_rejections`）
- **validation 节配置** 控制校验策略：
  - 校验模式（`mode`：true_replay/replay）
  - 并发度（`max_concurrency`）
  - 通过门槛（`required_results`、`required_approvals`）

在 Prompt Studio 右侧面板的"进化过程参数"区域可以直接编辑 evolve 和 validation 的核心参数，保存 Prompt 时一并更新。

## 最佳实践

1. **一次只改一个阶段**：修改后运行几轮进化观察效果，确认没问题再改下一个
2. **保留默认值作为参考**：经常对比默认 Prompt，理解每次修改的影响
3. **使用测试面板验证**：保存前务必用至少 2-3 个不同类型的会话测试
4. **谨慎修改 evolve_skill/create_skill**：这两个是核心阶段，直接影响技能质量；修改前备份
5. **不要删除共享块占位符**：`__GENERALIZATION_RULES__` 等占位符是关键
6. **裁判类阶段保持低 temperature**：judge 和 replay_checklist 建议 0.0-0.1
7. **监控 Token 用量**：如果为某个阶段设置了过大的 max_tokens 且模型频繁输出冗长内容，考虑收紧 Prompt 或降低 max_tokens
8. **审核技能产出**：修改 Prompt 后，重点关注下几轮进化产生的候选技能质量，在控制台的候选审核页面仔细评估
