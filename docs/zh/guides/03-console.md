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
| 资产中心 | 实验工作台、个人与团队资产、平台资产 | 编辑和验证 Skill，管理 Agent 可引用资产，或只读检查平台内部存储 |
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

管理员还可以在「映射注册表」面板为不同 Agent 配置各自的映射条目：每条可设置路由条件（trace 名称通配、tags、sessionId 模式，按列表顺序首个命中生效）与映射代码（可含 `map_session` 会话钩子）。支持逐条插入参考模板、试运行（显示路由是否命中）与面板级「路由预览」（粘贴样例 Trace 查看整张注册表的路由结果）。映射失败时该 trace 回退到内置转换，不中断整批导入。

### 进化链路

页面顶部提供两个标签：

- **Skills 自进化**：展示进化阶段图、可编辑 Prompt、阶段模型参数、过程参数和真实输入/输出测试
- **团队 Memory 自进化**：执行跨 User 记忆聚合

团队 Memory 聚合采用三步操作：

1. 输入或确认 OpenViking Account，选择增量或全量模式。
2. 拉取 Account 用户列表，使用全选、全不选或反选确定参与用户。
3. 明确确认后启动后台任务，并持续轮询分组进度。

页面刷新后会从服务端恢复最近任务；服务进程重启会清空这份运行列表。管理员可在同页编辑「团队记忆聚合 Skill」，并配置最终输出前缀。默认最终目录为 `viking://resources/shared-knowledge/`；Phase 1 原文快照和 merge 中间产物位于 merge 身份的私有 Resources，Skill 仅在 merge 阶段执行。

## 实验工作台

实验工作台在一个界面中完成 Skill Bundle 编辑和 True Replay 调试。左侧文件树可编辑 `SKILL.md`、脚本与引用文件；右侧选择数据集和运行参数；底部查看 Candidate Trace 与文件 Diff。运行时会把全部未保存文本文件作为 Candidate，不要求先覆盖正式 Skill。

## 个人与团队资产

资产页只展示两个 OpenViking 根空间：

| 空间 | 根 URI |
|------|--------|
| 个人记忆 | `viking://user` |
| 团队资源 | `viking://resources` |

文件树支持搜索、Markdown/JSON/代码预览、源码查看和目录 L0/L1 摘要。自建 OpenViking 会提供 Studio 链接；系统检测到 `ov` CLI 时还会显示内置 CLI。控制台不提供 Skill 导入或导出入口。

### 多文件编辑

1. 点击「编辑」进入编辑模式。
2. 依次修改多个文本文件；切换文件或资产空间不会丢失草稿。
3. 点击「完成编辑」，逐条查看带编号的行级 Diff。
4. 点击「确认保存」批量提交。

服务端会比较编辑开始时的内容哈希。任一文件已被其他写入者修改时返回 409，所有草稿继续保留。单文件上限 2 MB，单次最多 100 个文件、总计 16 MB。团队资源仅管理员可写；平台资产始终只读。

## 平台资产

平台资产是只读视图，只展示 teamEvolver 自身运行所需的目录，例如：

- `sessions/`、`session_archive/`、`session_ledger/`
- `candidate_skills/`、`validation_*`
- `skill_lab/`、`skill_datasets/`、`evolution_datasets/`
- `skill_evidence/`、`memory-changes/`、`memory-replays/`
- `skill_mutation_commits/`、`skill_sync_outbox/`

页面按存储来源分为 Session 流水与 Skill 进化产物：前者从 PostgreSQL（或本地/NAS）读取，后者从 NAS 读取。页面提供逻辑键树、内容预览、分类统计和存储后端标识，不依赖 OpenViking，也不显示 OV CLI。这些内容不会作为“个人与团队资产”直接提供给 Agent。

## 平台治理

### 全局模型

管理员可配置 OpenAI-compatible Base URL、Model ID、API Key、Max Tokens 和 Temperature，并直接测试连接。保存后会热更新进化与挖掘使用的全局模型；阶段级覆盖仍在「进化链路」中管理。

### 用户与权限

该页面管理：

- 团队显示名称
- 用户账号、角色、显示名、邮箱和密码
- 控制台用户资料与个人记忆/团队资源设置；Agent 接入使用租户 Key + user_id
- 个人记忆与团队资源的 Account + Name 绑定

Trusted 模式统一使用租户 Root Key，用户记录只保存 OpenViking Name；Account 来自当前租户配置。普通用户只能读取和修改自己的资料，管理员可以管理全部用户。

### 运行状态

运行状态页聚合服务、OpenViking、模型、用户注册表、团队 Skill 和 Agent Integration 的检查结果，并显示队列与 Candidate 数量。

「OpenViking 部署」面板支持：

- 火山云 OpenViking
- 本机自建 OpenViking
- 远程自建 OpenViking，通过 Endpoint 覆盖填写可达地址
- Account 与 OpenViking Root Key

保存后服务会热重载 OpenViking、DreamCycle 和嵌入式进化集成，无需重启主进程。

## 内置文档

「文档 → 使用文档」自动扫描 `docs/zh/`、`docs/en/` 和 `docs/design/`。阅读器支持目录树、全文搜索、中英文切换、GFM 表格、代码块和仓库图片。

## 相关文档

- [配置参考](./01-configuration.md)
- [Skill Miner 指南](./07-skill-miner.md)
- [Prompt Studio 指南](./08-prompt-studio.md)
- [存储空间与目录布局](../concepts/09-storage-layout.md)
- [团队记忆聚合 API](../api/11-team-memory-aggregation.md)
