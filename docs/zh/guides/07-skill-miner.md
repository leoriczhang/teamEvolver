# Skill 挖掘

Skill 挖掘把领域文档及人工补充的 Evidence 转成可审核的 Skill Candidate 和同源 Test Dataset。teamEvolver 管理任务、轮次和候选提交，OpenViking Compile 负责读取 Evidence 并生成产物。

## 准备与创建任务

启动 teamEvolver，在控制台配置可访问的 OpenViking Endpoint、Account 和服务端凭据。挖掘使用 OpenViking 上的模型及执行环境。

1. 在知识源页面注册或上传来源，确认目录带有 Skill 用途标签。知识库挖掘可以使用同一来源的 Knowledge 用途标签。
2. 创建任务并设置最大轮数（1–5，默认 3）、人工补证和每轮基础运行期限（60–7200 秒，默认 3600；大来源按输入分片和产物工作量自动延长）。
3. 查看任务进度、执行事件、远端任务 ID 和错误信息。最多同时执行 8 个任务；人工等待阶段不占执行名额。

来源在任务开始时冻结。后续修改原目录不会改变该任务的 Evidence。每轮使用独立输入与输出，并保存本地归档。

## 原生整轮交付

新任务默认每轮只提交一次公共 OpenViking Compile。编译 Skill 在这次 Compile 内完成来源阅读、方法归并、内容分工、生成与复核；内容单元增多不会增加 TE 的外层 Compile 调用，也不会直接增加最终 Skill 数量。

`te-native-components.v2` 按方法组织独立的内容单元，并保留旧 v1 产物兼容：

- `core` 单元生成 `METHOD.json`、可执行的 `SKILL.md`、`EVALUATION.md` 和组件清单，声明配套资源、Case ID、顺序及来源去向。主流程负责配置选择、资源读取、分支和完成条件。
- 原表、规则字典和已有提示词可由 Skill 在清单中声明为原文资源。TE 只从本任务冻结输入核对 SHA 并逐字装配，保留原始矛盾、格式和语言，不重写事实。生成的 `reference` 单元负责必要的配置导航、适用关系和覆盖对照，避免重复转写长篇原文。
- `case` 单元各生成一个完整的 Case JSON；每个方法有 12 个具备实质差异、来源明确的 Case。

中间草稿只传配置选择、具体状态与执行设计，不把整篇正文嵌入中间 JSON。新的 Agent 读取相关原文后独立复核设计，并直接写出该单元声明的完整文件。TE 校验组件清单、路径、唯一 ID、文件完整性、来源集合和 Test Dataset 契约，随后按清单顺序确定性装配原文资源、`benchmark.json` 并渲染 `BENCHMARK.md`。TE 不选择业务规则、不补写事实或新增 Case，也不为装配再次调用 Compile。

原文资源声明使用精确冻结源 URI，不能指向实时目录、未冻结文件或生成产物；SHA 由冻结清单校验，不能依赖模型自报。复制后的源文本保持可查用，但复制完整不等于规则覆盖。Skill 仍须给出配置匹配、资源查用、分支、冲突处置与完成条件；案例须验证具体状态下的可观察行为。单元复核和装配成功不代表已完成全包跨文件业务审查，更不代表 True Replay 通过。

来源逐文件冻结，目录最多 10000 个文件，单文件最多 20 MB；原始来源没有 16 MB 的总量限制。来源目录通过现有 OV 文件列表接口逐目录分页读取，不按固定目录深度或树节点数截断。任务详情显示当前轮次实际运行期限。

组件协议只用于采用该协议的新冻结请求。旧任务保留原编译器、请求、执行协议和历史产物，不自动迁移或重编译。上述流程使用现有 OpenViking 接口，不修改 OV 代码；来源冻结、断线恢复、停止和确定性装配由 TE 管理，知识库挖掘保持原有流程。

首轮按冻结的原始来源确定并保存主要语言，中文主导的材料使用简体中文。后续轮次沿用该语言，不因旧英文产物或补证语言改变。面向人的正文、说明、案例 Query、Checklist 自然语言、共享分析和复核问题使用该语言；字段名、ID、文件路径、来源 URI、代码、固定枚举和原文直接引用保持原样。`Checklist`、`Evidence`、`Materials` 等结构标签保留原样。控制台没有独立的输出语言选择器。

## 每轮产物

| 路径 | 用途 |
| --- | --- |
| `analysis/evidence-map.md` | Evidence 与可复用方法的对应关系、来源引用 |
| `analysis/gaps.md` | Evidence 缺口、冲突和适用边界 |
| `analysis/review.json` | 结构化人工复核问题，契约为 `skill-miner.review.v1` |
| `compiled_skill/<name>/SKILL.md` | Skill Candidate 正文 |
| `compiled_skill/<name>/EVALUATION.md` | 评估说明 |
| `compiled_skill/<name>/BENCHMARK.md` | Test Dataset 的可读说明 |
| `compiled_skill/<name>/benchmark.json` | 统一 `team-replay.dataset` 格式的 Test Dataset |

控制台支持产物预览、Markdown 滚动阅读与编辑。最终提交使用当前编辑后的产物。文件结构校验不证明业务事实正确；来源处理失败、未解决的问题或格式补齐仍显示为需复核。

## 多轮复核与补证

新任务启用人工补证时，仅在本轮问题清单非空时等待人工复核，选答问题也保留此等待。原生整轮任务的问题数为 0 时直接结束，不为凑满轮数再调用 Compile；本轮完整产物和跳过记录仍归档。非原生旧流程按所选轮数推进。自动跳过不表示人工批准、业务质量通过或原文未知事实已解决，也不添加补充 Evidence。已有任务沿用其冻结的检查点策略。管理员查看产物、回答结构化问题、补充可访问的 OpenViking 来源引用、上传附件或填写复核意见。无法确认的必答问题应明确标记为未解决。

点击继续后，原生整轮任务以紧邻上一轮的真实补证、问题涉及的必要冻结原文和修订上下文作为下一轮输入，向同一产物目标增量编译；自由文本复核意见无法界定影响范围时，保守带入全部冻结原文。非原生旧流程仍重新处理原始冻结来源和累计补证。上一轮 Skill Candidate 只作为修订上下文，不能代替已证实的 Evidence，也不能用于来源引用。达到轮数上限后可以完成最终复核或停止任务；最后复核新增的内容不会自动编入已有产物。

关闭人工补证时，原生整轮任务在当前轮产物完成结构校验后直接结束，不额外调用 Compile；非原生旧流程自动执行所选轮数。服务重启和网络重试会恢复已有任务及幂等提交状态。执行中的任务停止时请求远端取消；删除前应先停止任务。

详见 [多轮 Skill 挖掘与人工补证](./17-multi-round-skill-mining.md)。

## 候选验证与发布

完成挖掘后，管理员将 Skill Candidate 与 Test Dataset 提交到现有候选流程。True Replay 比较 Baseline 与 Candidate 的真实执行结果，Candidate Review 决定是否 Publish。挖掘完成和人工补证结束均不自动发布团队 Skill。

## 配置与接口

`mining.pipeline.max_rounds` 设置默认轮数，`mining.pipeline.runtime_timeout_seconds` 设置默认每轮基础期限。`mining.prompts.ov_compile` 可覆盖编译 Skill 正文；每轮冻结正文、输入和 Compile 请求，并绑定内容摘要对应的独立编译器 URI。

运行时将任务语言规则加入新的 Compile 请求，自定义业务编译指令原样保存，无需为语言更新而重置。已受理的请求与已完成的产物不追溯修改，不会自动翻译或重新 Compile。

接口参数、状态与错误见 [Skill 挖掘 API](../api/17-skill-mining.md)。知识库上传、Wiki 编译与浏览说明见 [控制台](./03-console.md)。
