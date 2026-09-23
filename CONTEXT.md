# teamEvolver

teamEvolver 是 Agent 团队能力进化控制面。它把真实工作中产生的经验转化为可复用、可验证、可治理的团队 Skill 与团队 Memory。

## Ontology 扩展术语

**Ontology**：通过批准的 Schema、实体、带证据和时间限定的断言、受限规则与 IssuePack 表达领域语义的资产。
_Avoid_: 团队 Memory、Skill、普通知识图谱文件

**Ontology Candidate**：OV Compile 草稿经 TE 确认 Schema、校验证据和后处理，再由 OV 校验并等待审核的私有本体候选，尚未改变正式 generation。
_Avoid_: 已发布本体、Memory Change、Skill Candidate

**Ontology Publication**：OV 验证 trusted Root Key、当前证据、主体资格和人工审批记录后，以原子提交激活 generation 并返回回执。
_Avoid_: 保存候选、Compile 完成、Skill Publish

**IssuePack**：固定 Schema、证据槽、规则和只读 ToolContract 版本的领域诊断配置。
_Avoid_: 提示词、业务执行授权

## Language

**Agent 团队能力**:
团队可持续复用的任务方法与长期上下文，由团队 Skill 和团队 Memory 共同构成。
_Avoid_: 知识库、提示词集合、文件仓库

**Session**:
一次 Agent 与用户围绕任务产生的完整交互记录，包含对话、工具调用、产物、上下文使用情况和效率数据。
_Avoid_: 聊天记录、日志

**Evidence**:
从 Session、领域文档或历史团队资产中提取，并能支持某项资产变化的可追溯事实。
_Avoid_: 数据、样本、观察

**Evidence Classification**:
对 Evidence 的资产去向判断，区分团队 Skill、用户 Memory、任务要求、Agent 运行时问题和证据不足。
_Avoid_: 标签、打分

**Skill**:
可执行、可复用的任务方法，明确适用场景、操作步骤、约束与配套资源。
_Avoid_: Prompt、脚本、SOP 文件

**Memory**:
可检索的长期事实、背景、偏好或团队共识，不直接规定完整任务执行流程。
_Avoid_: Skill、Session、文档

**个人资产**:
只归属于单个用户、不能默认对团队共享的 Skill 或 Memory。
_Avoid_: 私有数据

**团队资产**:
经过共享性判断并由团队治理的 Skill 或 Memory，可供授权成员和 Agent 使用。
_Avoid_: 公共资产、全局资产

**Skill Candidate**:
基于 Evidence 提议的新建或修订版本，在验证和发布前不影响已发布团队 Skill。
_Avoid_: 草稿、实验 Skill

**Memory Change**:
对团队 Memory 的新增、合并、修订、去重或归档提议，可按风险自动应用或转管理员处理。
_Avoid_: Memory Candidate、编辑

**Test Dataset**:
与 Skill Candidate 同源生成的任务集合，用于比较 Baseline 与 Candidate 的真实执行结果。
_Avoid_: Benchmark、测试题

**Checklist**:
一个 Replay Case 必须满足的扁平完成条件集合，只作为完成门禁，不参与效果评分。
_Avoid_: 评分项、质量分

**Baseline**:
不加载 Skill Candidate 时执行同一 Replay Case 的对照分支。
_Avoid_: 旧版本、原始答案

**True Replay**:
在隔离的真实 Agent 会话中执行 Baseline 与 Candidate，通过独立裁判确认完成性，并在自然用户反馈中渐进披露要求、比较客观效率。
_Avoid_: 模拟测试、文本评审、A/B 打分

**Evolution**:
由 Evidence 驱动，经过候选生成、效果验证、审计记录且可回滚的团队资产变化。
_Avoid_: 编辑、上传、同步

**Skill Evolution**:
把可复用的团队级任务经验转化为经过 True Replay 和管理员门禁的团队 Skill 版本。
_Avoid_: Skill 生成、Prompt 优化

**Memory Evolution**:
把个人经验和既有团队 Memory 聚合、去个人化、去重并维护为长期有效的团队 Memory。
_Avoid_: Memory 同步、知识整理

**DreamCycle**:
团队 Memory 进化中的维护阶段，对已有团队 Memory 去重、修订、标记过时内容并维护概况与可发现性；与跨用户聚合阶段分工协作。
_Avoid_: 定时任务、Memory Agent

**Candidate Review**:
管理员基于 Evidence、静态检查和 True Replay 结果决定 Skill Candidate 是否发布的门禁。
_Avoid_: 审批流、代码审查

**Publish**:
把通过门禁的 Skill Candidate 固化为新的团队 Skill 版本，供租户内 Agent 拉取使用。
_Avoid_: 保存、上传、同步

**Rollback**:
以新版本恢复某个历史团队 Skill 的完整内容，同时保留原有版本和审计链。
_Avoid_: 降级、撤销发布

**AgentPrincipal**:
由租户及其声明的用户组成的 Agent 数据面主体，个人资产归属于该租户内的用户。
_Avoid_: Agent Integration、Agent 注册身份、外部主体映射

**ReplayAdapterFactory**:
租户选择的 Agent 执行适配器，为每个 True Replay 分支创建独立且连续的 Agent 会话。
_Avoid_: Agent 注册、裁判 Agent、发布决策器
