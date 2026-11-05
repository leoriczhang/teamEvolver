# Managed Agents 接入

火山方舟 Managed Agents（MA）可在控制台「设置与接入 → MA 接入」配置。一个入口 Agent 可以是单 Agent 或任意角色的多 Agent 协调器。连接 Worker 发现固定版本的成员与自定义 Skill、导入团队库、采集 Session，并在管理员 Publish 后通过 pull 更新 MA。业务项目无需上报 Session SDK。

## 配置连接

填写 MA API 地址、入口 Agent ID、Environment ID、业务 Vault、默认用户以及当前租户 Key。MA Key 和租户 Key 一次输入后加密保存为 Secret Ref，接口只显示是否配置。生产 MA 地址使用 HTTPS。

连接会递归发现成员和全部自定义 Skill。预置 Skill 保留引用；同名同树哈希直接绑定，同名不同内容停止导入，使用独立租户解决冲突。全部导入完成后状态为 `ready`。名称、角色和模型不依赖 CodingVideo。

Session 标签传 `{"key":"te-user","value":"<user_id>"}`，归属优先于默认用户；读取时规范化为 `te-user:<user_id>`。MA 不接受字符串标签数组。缺少标签时记录默认归属来源。控制台同时展示业务事件时间与拉取时间。一个主 Session 的多轮范围或语义切分仍只计一个独立 Evidence 来源。

采集等待主会话和全部子线程结束，保留主 Agent idle 后才产生的子线程事件。每轮 `agent_threads` 保存子 Agent 身份、委派要求、回复、完整工具调用及结果、实际 Skill 使用与事件 ID；会话分析和独立 Trace 分析均读取这些证据。子线程分析记录带 `parent_trace_id` 和 `session_thread_id`，实际使用的 Skill 进入同一父 Session 的进化分组，不额外增加独立 Session 数。原生 `skill({name: ...})` 调用、`/skills/<id或名称>/`、`/workspace/skills/<名称>/` 的实际工具路径及 `runtime_binding.paths` 映射到的运行时文件构成使用证据，仅挂载或目录介绍不算使用。升级后旧采集投影会在下一次拉取时重新进入强制分析，保留原正式 Session ID。

长会话的模型输入按字节预算分批整理证据，保留全部工具条目和子线程身份；父轮次中的子工具只传引用，避免重复发送结果正文。所有批次的覆盖 ID 均通过校验后才进入最终会话评分；任何批次失败都阻止归档。原始事件与完整内容仍保存在 PostgreSQL。Trace 分析同样按字节预算拆批，必须返回每个主、子 Trace 的明确结论。数据集生成和进化 Agent 的过长上下文也使用同一证据整理协议。

证据摘要格式、覆盖 ID 或输出长度校验失败时，只重试该分块，最多三次，并增加推理模型的输出预算。Trace 裁判使用明确的 `expected_trace_identities` 身份清单；父轮次编号不能替代子线程的分析序号。无效裁判结果也只重试当前批次，三次仍失败则整段分析失败，不产生部分归档或正常结论。Trace 阶段读取当前租户的 Prompt Studio 设置。会话分析通过后，其完整结果作为不可变阶段输出保存；Trace 失败或 Worker 重启时复用该结果，仍需全部强制阶段成功才能归档。

Session 列表显示“实际使用 N / 可用 M”。可用数量来自运行快照中配置的技能目录；实际使用来自主、子 Agent 的明确调用证据。配置的 15 个 Skill 不意味着每个 Session 都实际使用了 15 个。

## 开启隔离回放

显式设置 `replay_enabled`、独立 `replay_environment_id` 和 `replay_vault_ids`，并确认 Vault、MCP、自定义工具宿主的隔离能力。云环境支持完整配置冻结；长期 Memory 无可复制快照、共享生产可写目录或未隔离工具会阻塞回放。能力不满足时仍可采集，不能据此宣称可比性验证成功。

Baseline 与 Candidate 各自创建完整临时 Agent 图和独立 Session，Candidate 只替换目标 Bundle。同分支多轮使用同一 Session，保留创意确认；工具确认要求人工介入时明确失败。真实 usage、工具事件、输入材料和产物保存到 PostgreSQL，媒体通过 ffprobe/ffmpeg 检查并给独立 judge 提供采样图。没有指标或关键产物时返回 unavailable / inconclusive。

临时资源先记外部效果意图，远端结果不明时先对账，完成后按账本精确清理。原业务 Session 与资源保留。

## 发布与运行时

管理员在 Candidate Review 查看每个受管连接的验证结果后 Publish。新增 Skill 要先选择目标成员；已有 Skill 使用原成员关联。连接读取完整 `GET /sync/skills` manifest，按文件树哈希复用或上传版本，只更新关联成员及其祖先，并回读验证。外部托管项漂移时停止写入，普通 System、model 和 tool 修改被保留。

`GET /sync/ma/{connection_id}?user_id=...` 使用租户 Key 返回最后完整应用的发布快照和 ETag。部分同步保持上一完整 release；首次尚未完成返回 503。专有运行时应用可用固定入口版本与对应 Environment 创建新 Session；现有 Session 保持自己的快照。

可选 `runtime_binding` 指定 runtime Skill、Bundle 到 workspace 的路径映射、工作目录、bootstrap 和 ready 文件。从同一 Bundle 构造确定性初始化脚本，创建新 Environment 并固定树哈希，限制 256 KiB。回放在发送业务任务前核验 ready 文件。参考 [MA API](../api/24-managed-agents.md)。

`archive_compression` 默认 `gzip`，可显式选择 `xz` 减少初始化脚本体积；两种方式解压后的 Bundle 字节和树哈希相同。`xz` 通过 Python 标准库 `lzma` 解压，无需安装额外的 xz 命令。部分 MA 环境在脚本尚未达到文档上限时也会返回 500，因此创建 Environment 后仍须回读验证。

`source: "skill_mount"` 从 `/mnt/skills/<skill_name>` 读取会话固定版本的文件，在全部文件的 SHA-256 校验通过后才复制并执行 bootstrap；运行时树哈希与默认内嵌方式相同。CodingVideo 示例使用此方式，避免完整 runtime 重复嵌入初始化脚本。适用前提是首次启动沙箱的 Agent 已挂载该 Skill；缺件或版本不匹配直接失败，不会生成 ready 证明。

`pre_setup` 在原初始化脚本前安装应用的固定依赖；`replace_setup_script:true` 显式改用 Bundle 初始化，避免重复嵌入旧 runtime。默认保留原脚本。CodingVideo 示例使用官方校验和安装 Node 22.23.3，因为当前 MA 默认 Node 20 不满足其 bootstrap；约 66 KiB 脚本真实返回 500，去重后约 33 KiB 可以创建。完整脚本仍受文档中的 256 KiB 上限约束。

真实 MA 的初始化阶段尚未提供配置的 Python 包，示例前置步骤显式安装固定依赖；输出文件在工具运行后才登记。可选 `ready_probe_message` 在同一隔离 Session 只读运行时文件，再下载实际 ready 证明，之后才开始业务任务。两分支使用相同探测，usage 计入真实指标。受理时固定当前连接与回放基础设施，来源 Agent 图、模型和 Environment 执行快照仍保持历史事实。

## CodingVideo 示例

示例在 `examples/managed-agents/codingvideo.json`，从部署文件读取 3 个 Agent、15 个自定义 Skill 的资源 ID，实际数量以云端发现结果为准。先准备独立回放 Environment/Vault ID 的环境变量，以及 team-evolver Root Key 或控制台登录环境变量。

```bash
python scripts/connect_managed_agent.py plan --config examples/managed-agents/codingvideo.json
python scripts/connect_managed_agent.py apply --config examples/managed-agents/codingvideo.json
python scripts/connect_managed_agent.py status --config examples/managed-agents/codingvideo.json
```

`apply` 幂等创建 `codingvideo-ma` 租户，保留已有 Key；新 Key 只写显式 `credential_output` 指定的 0600 文件。凭证由 `secret_env` 指定的环境变量读取，禁止放 CLI 参数或示例 JSON。已有连接使用 revision 更新并受理 discover。任务、模型测试与导入都在控制台 Jobs 中查看。

示例仅修改自身租户：两个真实 Session、一个用户即可形成团队 Evidence；自动 Evolution / Evaluate，Publish 仍手动；回放 1800 秒、20 次交互保护上限、并发 1。双方持续执行至全部 Checklist 满足后才比较效率；达到保护上限仍未完成时结论未定。已有租户须应用示例配置更新轮数，已受理任务保持原冻结配置。CodingVideo 媒体预批准和代码正式画面政策继续适用。

CodingVideo 配置 `CODINGVIDEO_TEAMEVOLVER_URL`、`CODINGVIDEO_TEAMEVOLVER_CONNECTION_ID`、`CODINGVIDEO_TEAMEVOLVER_USER_ID` 和私有 `CODINGVIDEO_TEAMEVOLVER_TENANT_KEY` 后，部署采用完整 release 的资源映射，新 Session 固定入口与 Environment。详情见 CodingVideo 的 `docs/team-evolver-integration.md`。

## 接入其他 MA

通用模板为 `examples/managed-agents/generic.json`，不依赖 CodingVideo 文件或专有运行时。在私有环境文件设置 `MA_TENANT_ID`、`MA_CONNECTION_ID`、`MA_ENTRY_AGENT_ID`、`MA_ENVIRONMENT_ID`、`MA_USER_ID`、`MA_MODEL_ID` 和 `MA_API_KEY`；已有租户再提供 `MA_TENANT_KEY`，新租户 Key 自动写入当前目录的 `.env.ma`（0600）。控制面鉴权沿用 Root Key 或控制台登录环境变量。

```bash
python scripts/connect_managed_agent.py plan --config examples/managed-agents/generic.json --env-file .env.ma
python scripts/connect_managed_agent.py apply --config examples/managed-agents/generic.json --env-file .env.ma
python scripts/connect_managed_agent.py status --config examples/managed-agents/generic.json --env-file .env.ma
```

模板默认先采集与生成 Candidate，隔离回放未配置时验证不会通过。开启完整链路需补齐 `replay_enabled`、独立 Environment/Vault 和隔离声明；不要把私有环境文件提交到版本库。也可复制 CodingVideo 示例，替换租户、连接、入口、Environment/Vault、默认用户及环境变量名称；没有专有运行时可删掉 `runtime_binding`。`$env` 从环境读取，`$json` 加 `pointer` 可读取现有部署文件，`$text` 读取无凭据的初始化脚本，普通字段可直接填写非敏感 ID。省略 `skills`、`replay`、`model` 时保留租户现有策略。

暂停连接停止新采集和同步，已经受理的任务固定 revision；远端当前版本保留。错误、漂移和清理待办可在连接状态与 release 历史中检查，再受理 reconcile。平台范围限定为同一火山方舟 MA API。
