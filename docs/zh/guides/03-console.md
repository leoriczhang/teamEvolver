# Web 控制台用户指南

控制台与 API 由同一个 `api` 角色进程提供，默认地址为 `http://127.0.0.1:52010/`。前端源码位于 `web-ui/src/`，导航定义在 `web-ui/src/App.tsx`，构建产物位于 `teamEvolver/web/dist/`；未构建时 `GET /` 返回 503。

## 登录与初始化

系统没有任何用户时，控制台显示管理员初始化页，表单调用 `POST /api/auth/bootstrap`。表单预填账号与密码 `admin`，生产环境提交前必须改为强密码。

初始化后，其他人可在登录页注册账号（`POST /api/auth/register`）。`self_registration_enabled=false` 时注册入口隐藏。新注册账号没有租户成员关系，需要租户管理员在「成员与接入」中添加后才能进入租户。

控制台登录使用 HttpOnly Session Cookie，Root Key 不会下发到浏览器。角色与权限见 [控制台 RBAC](../api/20-console-rbac.md)。

## 租户切换与可见性

侧栏顶部的租户选择器列出当前用户可进入的租户，切换后页面整体重新加载，避免跨租户缓存。菜单可见性只由角色决定：

| 标记 | 可见用户 |
|------|----------|
| 无标记 | 有租户成员关系的用户 |
| 租户管理员 | 当前租户的 `tenant_admin` |
| 平台管理员 | `platform_role=platform_admin` |

页面可通过 `?view=<key>` 直接打开，例如 `/?view=workspace`；运行总览的标签页通过 `&tab=<key>` 指定。

## 导航结构

| 分组 | 页面 | `view` | 可见性 |
|------|------|--------|--------|
| 技能挖掘 | 知识库挖掘 | `mine-knowledge-base` | 租户管理员，需配置 OpenViking |
| 工作台 | 运行总览 | `dashboard` | 成员 |
| 工作台 | 实验工作台 | `workspace` | 成员 |
| 工作台 | 运行状态 | `health` | 租户管理员 |
| 智能体与能力 | 个人与团队资产 | `agent-assets` | 成员 |
| 智能体与能力 | 平台资产 | `platform` | 租户管理员 |
| 智能体与能力 | 成员与接入 | `members` | 租户管理员 |
| 知识与方法 | 经验库 | `experiences` | 成员 |
| 进化与实验 | 数据集 | `datasets` | 成员 |
| 进化与实验 | 进化链路 | `prompt-studio` | 租户管理员 |
| 审查与发布 | 审查与发布入口 | `review-center` | 成员 |
| 审查与发布 | 权限审计 | `identity-audit` | 平台管理员 |
| 审查与发布 | 链路观测 | `observability` | 平台管理员 |
| 设置与接入 | 平台用户 | `users` | 平台管理员 |
| 设置与接入 | 租户管理 | `tenants` | 平台管理员 |
| 设置与接入 | 模型配置 | `model` | 租户管理员 |
| 设置与接入 | 数据源接入 | `datasource` | 租户管理员 |

## 技能挖掘

### 知识库挖掘

知识库挖掘把 OpenViking 中的原始知识目录编译为结构化 Wiki。页面分为「知识源」与「知识库」两个文件树：

- 上传文件或文件夹到知识源，服务端转换为 Markdown
- 浏览、编辑、删除知识源与 Wiki 文件，查看反向引用
- 填写本次挖掘要求，选择增量编译起点，启动 OpenViking Compile 并查看阶段进度
- 生成知识库 HTML 图谱

未配置 OpenViking 时页面显示配置提示并跳转到「运行状态」。完整流程见 [知识库挖掘指南](./07-skill-miner.md)。

## 工作台

### 运行总览

运行总览包含四个标签页：

| 标签 | `tab` | 内容 |
|------|-------|------|
| 总览 | `overview` | 服务状态、Session 队列与历史、候选摘要、Skill 版本 |
| 候选评审 | `candidates` | Candidate 详情、Bundle Diff、A/B 回放明细与发布决策 |
| 进化审计 | `audit` | 每个进化周期消费的 Session、生成的 Candidate 和发布结果 |
| 过滤审计 | `filter` | Session 入队前的 valuable / chitchat 判别结果 |

Candidate 先经过 Checklist 完成门禁，再按交互轮次、工具调用数、Token 用量比较效率，规则见 [Checklist](../concepts/07-checklist.md)。候选评审支持用户标记 `reviewed`、`rejected`；发布（包括强制发布）会自动标记 `adopted`。

### 实验工作台

实验工作台在一个界面完成 Skill Bundle 编辑和 True Replay 调试：

- 左侧文件树编辑 `SKILL.md`、脚本与引用文件，支持快速打开与新建文件
- 选择 Test Dataset 和运行参数，在调试控制台运行 True Replay
- 底部查看 Candidate Trace 与文件 Diff

运行时把未保存的文本修改作为 Candidate 参与调试，不要求先覆盖正式 Skill。只有租户管理员可以保存，保存经 SkillHub 生成新版本。

### 运行状态

运行状态页聚合服务、PostgreSQL 存储池、对象存储、OpenViking、模型、Replay 适配器、服务日志与 Agent 拉取等检查项，每项给出建议动作。

「OpenViking 部署」面板可选择火山云 OpenViking、本机自建或远程自建（填写 Endpoint 覆盖），并配置 Account 与 Root Key。保存后服务热重载 OpenViking 集成，不需要重启进程。

## 智能体与能力

### 个人与团队资产

资产页浏览 OpenViking 中两个根空间：

| 空间 | 根 URI | 写权限 |
|------|--------|--------|
| 个人记忆 | `viking://user` | 本人 |
| 团队资源 | `viking://resources` | 租户管理员 |

文件树支持搜索、Markdown/JSON/代码预览和目录 L0/L1 摘要。服务器安装 OpenViking CLI 时页面显示内置终端，普通用户只能执行只读命令。

多文件编辑流程：

1. 点击「编辑」进入编辑模式。
2. 依次修改多个文本文件，切换文件不会丢失草稿。
3. 点击「完成编辑」查看逐文件行级 Diff。
4. 点击「确认保存」批量提交。

服务端比较每个文件编辑开始时的内容；任一文件已被他人修改时整批返回 409，草稿保留。单次最多 100 个文件，单文件 ≤2 MiB，合计 ≤16 MiB。接口见 [Workspace 与平台资产 API](../api/23-workspace-assets.md)。

### 平台资产

平台资产只读展示 PostgreSQL ObjectStore 中当前租户的任务 payload，按类别分为接入原文、Session 分阶段结果和 Memory Compile 结果。页面提供逻辑键树、内容预览与分类统计，不依赖 OpenViking。这些对象不会提供给 Agent。

### 成员与接入

租户管理员在此页：

- 按用户 ID 添加成员，分配 `tenant_admin` 或 `member` 角色
- 轮换租户 Key，新 Key 仅展示一次
- 设置租户运行配额

Agent 使用租户 Key 加声明的 `user_id` 接入，控制台成员关系与 Agent 身份相互独立。default 租户的接入凭证由平台管理员配置。

## 知识与方法

### 经验库

经验库展示 Session 分析生成的 Skill 错误经验和优秀实践，支持按 Skill 与关键词筛选。经验来自 Session 中的 `used_skills`，不要求对应 Skill 已上传。租户管理员可在同页管理成功经验同步，见 [成功经验同步](./16-successful-experience-sync.md)。

## 进化与实验

### 数据集

数据集页从 Session 创建 Test Dataset，编辑初始 Query、输出 Checklist 与轨迹 Checklist，导出数据集，并发起批量重回放验证。批量重回放的并发数为 1–2，单条超时 30–1800 秒。

### 进化链路

进化链路页有两个标签：

- **Skills 自进化**：编辑各阶段 Prompt、模型与过程参数，用真实输入测试阶段输出，详见 [Prompt Studio 指南](./08-prompt-studio.md)
- **团队 Memory 自进化**：选择 OpenViking Account 与成员，按「仅聚合」「仅维护」「聚合 + 维护」模式手动运行团队 Memory 聚合，配置聚合 Skill 与维护 Skill，见 [团队记忆聚合 API](../api/11-team-memory-aggregation.md)

## 审查与发布

「审查与发布入口」链接到运行总览的 Skill Candidate 候选评审。已发布 Skill 与团队 Memory 各自保留维护入口。

平台管理员可在「权限审计」查看平台访问、成员和身份变更记录；在「链路观测」配置全局 Langfuse 出站追踪，包括采样率和是否采集模型输入输出。

## 设置与接入

### 平台用户与租户管理

平台管理员在「平台用户」管理登录账号与平台身份，在「租户管理」创建或禁用租户。创建租户时，系统以 `tenant_id` 在 OpenViking 中查找同名 Account，已存在则绑定，不存在则用 Root Key 创建。

### 模型配置

租户管理员配置当前租户的默认模型、Base URL、API Key、Max Tokens 与 Temperature，可直接测试连接。阶段级模型覆盖在「进化链路」中管理。

### 数据源接入

数据源接入页管理当前租户的 Session 数据源：

- 编辑数据源连接与过滤条件
- 上传、编辑适配器源码并保存为不可变版本，用草稿测试验证转换结果
- 预览并手动拉取 Session，查看业务时间窗口与系统完成时间
- 启用每日定时拉取，设置触发时间与时区

适配器以服务进程权限执行，只能上传可信代码。操作步骤见 [数据源接入指南](./10-datasource-onboarding.md)。

## 相关文档

- [配置参考](./01-configuration.md)
- [控制台 RBAC](../api/20-console-rbac.md)
- [存储空间与目录布局](../concepts/09-storage-layout.md)
