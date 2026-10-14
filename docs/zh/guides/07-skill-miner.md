# Skill 挖掘

Skill 挖掘把领域文档及人工补充的 Evidence 转成可审核的 Skill Candidate 和同源 Test Dataset。teamEvolver 管理任务、轮次和候选提交，OpenViking Compile 负责读取 Evidence 并生成产物。

## 准备与创建任务

启动 teamEvolver，在控制台配置可访问的 OpenViking Endpoint、Account 和服务端凭据。挖掘使用 OpenViking 上的模型及执行环境。

1. 在知识源页面注册或上传来源，确认目录带有 Skill 用途标签。知识库挖掘可以使用同一来源的 Knowledge 用途标签。
2. 创建任务并设置最大轮数（1–5，默认 3）、人工补证和每轮基础运行期限（60–7200 秒，默认 3600；大来源按输入分片和产物工作量自动延长）。
3. 查看任务进度、执行事件、远端任务 ID 和错误信息。最多同时执行 8 个任务；人工等待阶段不占执行名额。

来源在任务开始时冻结。后续修改原目录不会改变该任务的 Evidence。每轮使用独立输入与输出，并保存本地归档。

默认编译采用有界分批：长文件切片抽取，方法提议保留在本地账目中，规划逐批处理并核对每项去向，每个候选的文件分别生成。Test Dataset 每次模型调用生成一个案例，最多四个独立案例文件共用一次 Compile，校验后由程序组装完整文件。每个文件有独立的输入、输出路径和校验结果；批次部分成功时保留合格案例，仅修复缺失或不合格案例，不将 OV 的完成状态直接当作整批合格。输入增多会增加批次和耗时，不会放大单次模型输入或要求一次输出全部产物。来源逐文件冻结，目录最多 10000 个文件，单文件最多 20 MB；原始来源没有 16 MB 的总量限制。任务详情显示当前轮次实际运行期限。

来源目录通过现有 OV 文件列表接口逐目录分页读取，不按固定目录深度或树节点数截断。Skill 正文逐段校验并原子写入；最终身份校验和归档哈希分块读取，避免重复载入完整正文，不截短产物。

默认流程直接使用公共 OpenViking Compile 的现有接口，不修改 OV 代码。TE 按预算分批提交来源抽取、方法规划、文件和案例生成，校验每阶段的小产物后推进；编译 Skill 负责业务规则。来源映射、断线恢复、停止和 Test Dataset 组装由 TE 执行，知识库挖掘保持原有流程。

规划暂缓的方法会先回查相关原文，再决定是否需要人工补证；摘要没有写出的规则和执行时尚未提供的输入值不会直接变成证据缺口。不同批次中的同一方法复用候选身份，相似提议另作分批复核，并保留所有来源。共享公式或来源本身不会导致合并；有限的相似检索仍可能漏掉重复，管理员应结合正文复核。

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

启用人工补证时，每轮结束都进入人工复核，包括最后一轮。管理员查看产物、回答结构化问题、补充可访问的 OpenViking 来源引用、上传附件或填写复核意见。无法确认的必答问题应明确标记为未解决。

点击继续后，下一轮重新处理原始冻结来源和累计补证。上一轮 Skill Candidate 只作为修订上下文，不能代替已证实的 Evidence，也不能用于来源引用。达到轮数上限后可以完成最终复核或停止任务；最后复核新增的内容不会自动编入已有产物。

关闭人工补证时，系统自动执行所选轮数。服务重启和网络重试会恢复已有任务及幂等提交状态。执行中的任务停止时请求远端取消；删除前应先停止任务。

详见 [多轮 Skill 挖掘与人工补证](./17-multi-round-skill-mining.md)。

## 候选验证与发布

完成挖掘后，管理员将 Skill Candidate 与 Test Dataset 提交到现有候选流程。True Replay 比较 Baseline 与 Candidate 的真实执行结果，Candidate Review 决定是否 Publish。挖掘完成和人工补证结束均不自动发布团队 Skill。

## 配置与接口

`mining.pipeline.max_rounds` 设置默认轮数，`mining.pipeline.runtime_timeout_seconds` 设置默认每轮基础期限。`mining.prompts.ov_compile` 可覆盖编译 Skill 正文；每轮冻结正文、输入和 Compile 请求，并绑定内容摘要对应的独立编译器 URI。

运行时将任务语言规则加入新的 Compile 请求，自定义业务编译指令原样保存，无需为语言更新而重置。已受理的请求与已完成的产物不追溯修改，不会自动翻译或重新 Compile。

接口参数、状态与错误见 [Skill 挖掘 API](../api/17-skill-mining.md)。知识库上传、Wiki 编译与浏览说明见 [控制台](./03-console.md)。
