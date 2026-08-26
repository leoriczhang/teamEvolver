# Web 控制台用户指南

teamEvolver 控制台与 API 共用一个 FastAPI 服务，默认地址为 `http://127.0.0.1:52010/`。前端源码位于 `web-ui/src/`，生产构建位于 `teamEvolver/web/dist/`。

## 登录与初始化

首次访问时，如果系统还没有用户，控制台会打开管理员初始化页。表单默认填入账号和密码 `admin`，生产环境应在提交前改用强密码。初始化完成后，其他成员可以在登录页注册普通用户；管理员可在「用户与权限」中调整角色和空间绑定。

控制台登录使用 HttpOnly Session Cookie。普通用户只能查看和编辑自己的个人资产；管理员可以切换用户并管理团队资产。

## 导航结构

| 一级区域 | 页面 | 用途 |
|----------|------|------|
| 技能挖掘 | 挖掘总览、知识源、挖掘任务 | 管理文档知识源，运行三阶段 SkillMiner，审核和提交产物 |
| 进化闭环 | 运行总览、Langfuse 接入、进化链路 | 查看 Session → Candidate → Replay → Publish 状态，配置 Skill 与 Memory 进化 |
| 资产中心 | Agent 工作空间、平台资产 | 管理 Agent 可引用资产，或只读检查平台内部存储 |
| 平台治理 | 全局模型、用户与权限、运行状态 | 管理模型、身份、OpenViking 部署和系统健康 |
| 文档 | 使用文档 | 阅读和搜索仓库内的中英文 Markdown 文档 |

页面可通过 `?view=<key>` 直接打开，例如 `/?view=workspace` 或 `/?view=health`。

## 技能挖掘

### 挖掘总览

汇总知识源、挖掘任务、产物和运行状态，并提供常用入口。

### 知识源

知识源页面支持：

- 上传文档并执行后处理
- 创建、重命名、合并和删除知识源目录
- 浏览源文件及处理状态
- 将选定目录直接带入新挖掘任务

### 挖掘任务

每个任务按以下阶段运行：

1. 样本包构建
2. 语义发现
3. Skill 与 `EVALUATION.md` 编译
4. 可选反思轮次与 Benchmark

任务支持并行执行、停止、删除、失败诊断复制、人工补证和继续运行。完成后可以预览或编辑 Markdown 产物，并将 Skill 提交到 Candidate 验证链路。

## 进化闭环

### 运行总览

「运行总览」包含四个标签页：

- **总览**：服务状态、Session 队列和历史、候选摘要、Skill 版本
- **候选评审**：Candidate 详情、Bundle Diff、True Replay 结果和发布决策
- **进化审计**：每个进化周期消费的 Session、生成的 Candidate 和发布结果
- **过滤审计**：Session 在入队前的价值分类和跳过原因

Candidate 必须先满足 Checklist 完成门禁，再按交互轮次、工具调用数、Token 用量依次比较效率。管理员可以按评估结果发布，也可以在明确知晓风险时强制发布。

### Langfuse 接入

Langfuse 页面把两条相互独立的链路放在一起管理：

- **入站 Session 拉取**：按 environment、user、tags、release、version、trace name 等条件预览和导入 Session
- **出站链路追踪**：记录 teamEvolver 内部模型与工具调用

管理员还可以编辑 `map_trace(trace, observations)`，用内置样例或粘贴的 Trace 试运行，并对比自定义映射与内置映射结果。映射失败时单条 Session 回退到内置转换，不中断整批导入。

### 进化链路

页面顶部提供两个标签：

- **Skills 自进化**：展示进化阶段图、可编辑 Prompt、阶段模型参数、过程参数和真实输入/输出测试
- **团队 Memory 自进化**：执行跨 User 记忆聚合

团队 Memory 聚合采用三步操作：

1. 输入或确认 OpenViking Account，选择增量或全量模式。
2. 拉取 Account 用户列表，使用全选、全不选或反选确定参与用户。
3. 明确确认后启动后台任务，并持续轮询分组进度。

页面刷新后会从服务端恢复最近任务；服务进程重启会清空这份运行列表。管理员可在同页编辑「团队记忆聚合 Skill」，并配置最终输出前缀。默认最终目录为 `viking://resources/shared-knowledge/`，中转数据位于同级 `viking://resources/shared-knowledge-staging/`。

## Agent 工作空间

Agent 工作空间只展示 Agent 可引用的资产，分为：

| Workspace | 内容 |
|-----------|------|
| 个人 Workspace | 个人 Skills、个人 Memory、个人 Resources |
| 团队 Workspace | 团队 Skills、团队 Memory、团队 Resources |

文件树支持搜索、Markdown/JSON/代码预览、源码查看和目录 L0/L1 摘要。自建 OpenViking 会提供 Studio 链接；系统检测到 `ov` CLI 时还会显示内置 CLI。

### 多文件编辑

1. 点击「编辑」进入编辑模式。
2. 依次修改多个 Memory 或 Skill 文件；切换文件或 Workspace 不会丢失草稿。
3. 点击「完成编辑」，逐条查看带编号的行级 Diff。
4. 点击「确认保存」批量提交。

服务端会比较编辑开始时的内容哈希。任一文件已被其他写入者修改时返回 409，所有草稿继续保留。单文件上限 2 MB，单次最多 100 个文件、总计 16 MB。Resources 与平台资产不允许通过该编辑器写入。

### Skill Lab

Skill Lab 使用已保存的 Skill 作为 Baseline、当前草稿作为 Candidate。可以管理或从历史 Session 合成测试集，运行真实 A/B Replay，并查看 Checklist、分支输出、轮次、工具调用和 Token 对比。实验结果不会自动覆盖正式 Skill。

### Memory Lab

Memory Lab 从个人或团队 Memory 中选择文本文件，编辑只用于实验的草稿，并比较改动前后的 Context 注入或 True Replay 结果。草稿不会自动写回 OpenViking；确认有效后再回到 Workspace 编辑流程保存。

## 平台资产

平台资产是只读视图，只展示 teamEvolver 自身运行所需的目录，例如：

- `sessions/`、`session_archive/`、`session_ledger/`
- `candidate_skills/`、`validation_*`
- `skill_lab/`、`skill_datasets/`、`evolution_datasets/`
- `skill_evidence/`、`memory-changes/`、`memory-replays/`
- `skill_mutation_commits/`、`skill_sync_outbox/`

这些内容不会作为 Agent Workspace 资产直接提供给 Agent。

## 平台治理

### 全局模型

管理员可配置 OpenAI-compatible Base URL、Model ID、API Key、Max Tokens 和 Temperature，并直接测试连接。保存后会热更新进化与挖掘使用的全局模型；阶段级覆盖仍在「进化链路」中管理。

### 用户与权限

该页面管理：

- 团队显示名称
- 用户账号、角色、显示名、邮箱和密码
- `integration_id + external_subject` 的 Agent 身份映射
- 个人与团队 OpenViking Workspace 绑定

Trusted 自建 OpenViking 会自动把个人空间绑定到同名用户，并复用服务 Key 完成服务端访问。云端部署可为用户配置独立个人凭据。普通用户只能读取自己的资料和密钥状态，管理员可以管理全部用户。

### 运行状态

运行状态页聚合服务、OpenViking、模型、用户注册表、团队 Skill 和 Agent Integration 的检查结果，并显示队列与 Candidate 数量。

「OpenViking 部署」面板支持：

- 火山云 OpenViking
- 本机自建 OpenViking
- 远程自建 OpenViking，通过 Endpoint 覆盖填写可达地址
- Account、默认个人用户、团队用户、资源根前缀和服务/个人 Key

保存后服务会热重载 OpenViking、DreamCycle 和嵌入式进化集成，无需重启主进程。

## 内置文档

「文档 → 使用文档」自动扫描 `docs/zh/`、`docs/en/` 和 `docs/design/`。阅读器支持目录树、全文搜索、中英文切换、GFM 表格、代码块和仓库图片。

## 相关文档

- [配置参考](./01-configuration.md)
- [Skill Miner 指南](./07-skill-miner.md)
- [Prompt Studio 指南](./08-prompt-studio.md)
- [存储空间与目录布局](../concepts/09-storage-layout.md)
- [团队记忆聚合 API](../api/11-team-memory-aggregation.md)
