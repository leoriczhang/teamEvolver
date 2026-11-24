# Managed Agents 接入

火山方舟 Managed Agents（MA）可在控制台「设置与接入 → MA 接入」配置。一个入口 Agent 可以是单 Agent 或任意角色的多 Agent 协调器。TE 直接使用 MA 云端 Skills 管理接口查看、编辑、发布与回滚，连接 Worker 发现成员、记录云端原生版本并采集 Session。业务项目无需上报 Session SDK。

## 配置连接

填写 MA API 地址、入口 Agent ID、Environment ID、业务 Vault；默认归属用户可留空。表单自动读取并明文填入当前租户 Key，接入时直接复用。MA Key 和租户 Key 加密保存为 Secret Ref；MA 配置接口只显示是否配置，租户管理员可在 MA 页面点击「查看租户 Key」直接查看、复制，也可在「成员与接入」查看。刷新或重新打开页面后仍读取同一个已保存的 Key。旧 Key 若未留存，可填入原 Key 后保存，无需重新签发。生产 MA 地址使用 HTTPS。

连接会递归发现成员和全部自定义 Skill。预置 Skill 保留引用；自定义 Skill 的最新内容取云端 `latest_version`，成员实际使用的 `bound_version` / `bound_versions` 单独展示。发现不导入 TE 团队库，不上传旧版本，完成回读后状态为 `ready`。跨连接同名 Skill 需指定 `connection_id`。名称、角色和模型不依赖 CodingVideo。

Session 标签传 `{"key":"te-user","value":"<user_id>"}`，归属优先于渠道身份和默认用户；读取时规范化为 `te-user:<user_id>`。MA 不接受字符串标签数组。缺少标签时，从首条主用户消息的 `ChannelContext` 提取实际发起人，后续发言者保留在各轮次中。OpenID 按 `provider.app_id.sender_open_id` 保存；姓名只使用同一身份的渠道头信息，未提供时显示身份 ID。渠道消息身份缺失时标记未知，不归给连接配置人；普通非渠道消息仍记录默认归属来源。标题提取实际请求正文，去掉渠道协议头、读取策略和开头的 Bot 路由提及，原始消息保留。已有会话的身份和标题在下次采集时仅修正元数据，保留分析、产物和时间，不重放。控制台同时展示业务事件时间与拉取时间。一个主 Session 的多轮范围或语义切分仍只计一个独立 Evidence 来源。

采集等待主会话和全部子线程结束，保留主 Agent idle 后才产生的子线程事件。每轮 `agent_threads` 保存子 Agent 身份、委派要求、回复、完整工具调用及结果、实际 Skill 使用与事件 ID；会话分析和独立 Trace 分析均读取这些证据。子线程分析记录带 `parent_trace_id` 和 `session_thread_id`，实际使用的 Skill 进入同一父 Session 的进化分组，不额外增加独立 Session 数。原生 `skill({name: ...})` 调用、`/skills/<id或名称>/`、`/workspace/skills/<名称>/` 的实际工具路径及 `runtime_binding.paths` 映射到的运行时文件构成使用证据，仅挂载或目录介绍不算使用。升级后旧采集投影会在下一次拉取时重新进入强制分析，保留原正式 Session ID。

长会话的模型输入按字节预算分批整理证据，保留全部工具条目和子线程身份；父轮次中的子工具只传引用，避免重复发送结果正文。所有批次的覆盖 ID 均通过校验后才进入最终会话评分；任何批次失败都阻止归档。原始事件与完整内容仍保存在 PostgreSQL。Trace 分析同样按字节预算拆批，必须返回每个主、子 Trace 的明确结论。数据集生成和进化 Agent 的过长上下文也使用同一证据整理协议。

证据摘要格式、覆盖 ID 或输出长度校验失败时，只重试该分块，最多三次，并增加推理模型的输出预算。Trace 裁判使用明确的 `expected_trace_identities` 身份清单；父轮次编号不能替代子线程的分析序号。无效裁判结果也只重试当前批次，三次仍失败则整段分析失败，不产生部分归档或正常结论。Trace 阶段读取当前租户的 Prompt Studio 设置。会话分析通过后，其完整结果作为不可变阶段输出保存；Trace 失败或 Worker 重启时复用该结果，仍需全部强制阶段成功才能归档。

Session 列表显示“实际使用 N / 可用 M”。可用数量来自运行快照中配置的技能目录；实际使用来自主、子 Agent 的明确调用证据。配置的 15 个 Skill 不意味着每个 Session 都实际使用了 15 个。

MA 的模型、Agent、工具或 Environment 配置变化不阻止 Skill Evolution，也不因执行指纹不同归档历史 Evidence / Case。每次进化和候选保存来源 Session 的实际 Agent 版本、模型及接口地址、Skill 引用版本、Environment 身份与配置哈希；候选详情同时展示业务时间和入库时间。连接暂停或来源快照缺失仍限制自动使用。技能基线更新继续归档旧候选和旧 Evidence，新候选从最新技能包生成。True Replay 保持来源执行快照固定，只切换目标 Skill；当前连接提供 MA API 访问凭证。

Worker 为 MA 发现、采集与同步保留独立执行槽，避免长时间分析或回放占满接入能力；各槽按租户轮转，健康状态合并判断。同一连接仍通过数据库租约串行执行，等锁使用延后调度，不消耗执行重试次数。单个 Session 回读返回 404 时保留已有范围、记录 `unavailable` 并继续其余采集；认证失败和服务错误仍按连接错误处理。

## 开启隔离回放

`default_user_id` 可留空：原 Session 没有 `te-user` 标签、渠道身份或明确默认归属时，保存稳定的 `unknown.*` 身份并显示“未知提交人”。不能从对话正文猜测姓名，也不能把接入操作人当成提交人。明确填写默认归属的连接保持该配置，界面标注“默认归属”。修改默认归属后补采，已有 Session 和采集记录会修正归属，原始事件、分析阶段与时间保持不变；这项元数据修正不需要重新导入 Skill。

Skills 管理通过 MA Secret Ref 调用云端接口；连接器身份不作为任何业务会话的提交人。

候选验证默认选择经验审阅或一个相关 Candidate 场景。经验审阅不需要配置隔离回放，
也不宣称真实 Runtime 执行；候选执行与完整 A/B 才消费以下隔离要求。候选执行只运行
Candidate，明确阶段或完整交付范围；完整 A/B 保留双分支和效率比较。详见 [候选 API](../api/10-validation.md)。

新连接默认开启「启用隔离 MA True Replay」，已有连接保留显式开关。Baseline 与 Candidate 创建两个原生 Session，复用来源 Environment 和 Vault 引用；Environment 是配置身份，每个 Session 仍有独立沙箱。无需另建 Environment / Vault 或复制凭证。旧 API 字段 `replay_environment_id` / `replay_vault_ids` 保留读取兼容，但新任务始终固定来源 Session 的引用。

Candidate Validation、Skill / Memory Replay 与 Dataset 执行共用 Worker 输入准备。缺失的 URL、File、Base64 或文本附件从原 Session 归档范围补采，按大小与 SHA-256 固定到租户存储；补采结果单独保存，重试不重新选择输入。缺失 Agent / Environment / Vault 快照只从原生 Session 恢复，不用当前配置冒充历史事实。新 Skill 尚无目标绑定时，在来源入口 Agent 上隔离验证；发布目标仍走发布门禁。

合成 Case 如果明确排除原附件并提供完整替代输入，由模型判定其输入契约并保存原文引用；保留该 Case 的缺素材测试前提，不把不相关视频附入任务。真实附件任务保留原视频、图片或 PDF 模态及视频 fps。禁用的 MCP 工具集无需外部隔离；已核验只读服务用 `replay_readonly_mcp` 固定服务快照及闭合工具名单，新增写工具或服务变化仍须重新核验。

两分支固定相同的来源依赖、初始化脚本、网络配置和 Vault 引用，通过显式 `tos: null` 清除共享输出配置以使用 Session 独立输出位置。仅省略该字段会被 MA 的合并覆写语义重新继承；回读仅兼容已清空字段的省略，继承共享目录仍拒绝执行。凭证由 MA 原生 Vault 注入，TE 不读取或复制凭证值；同一引用不能证明外部服务状态或凭证值从未变化。采集快照保存 `vault_ids`，旧快照缺失时须补采并刷新 Case，不从当前连接推测。长期 Memory 无快照及未隔离 MCP 仍阻塞回放；真实自定义工具调用需要执行器。能力不满足时仍可采集，不能据此宣称验证成功。

Agent 仅声明自定义工具不阻止回放，原始工具定义在两分支中完整保留；只使用 MA 内置工具的 Case 可正常执行。当前适配器尚未接入自定义工具执行器，主线程或子线程实际发出 `agent.custom_tool_use` 时立即返回 unsupported，记录工具名称、事件与线程 ID，并归档最终事件；不伪造工具结果。`replay_isolation.custom_tools` 不能代替执行器，也不能绕过实际调用检查。

连接的 `replay_capability.supported` 仅表示配置前置检查通过，`assessment=configuration_only` 明确此范围；`custom_tool_names` 列出主、子 Agent 尚无执行器的自定义工具。控制台显示「配置检查通过」，不能据此视为真实回放成功。Session 回读兼容 Skill 显示名称省略、空列表省略，以及原生子 Agent 引用展开；展开的执行字段仍与固定成员配置比对，模型、系统指令、Skill ID 和版本变化继续阻塞。

Baseline 与 Candidate 使用同一固定版本入口 Agent、相同 Environment 配置与 Runtime、相同任务材料，创建两份独立 Session/沙箱。入口 Agent 通过 `agent_with_overrides` 固定来源 Session 的 system、model、tools、context_management 等执行字段，两分支之间只切换目标 Skill；同一 Agent 版本上的来源模型覆写也会保留。多 Agent 仅克隆受目标 Skill 影响的子成员及祖先，其余成员保留原固定引用。同分支多轮使用同一 Session，保留创意确认；工具确认要求人工介入时明确失败。创建后回读实际 Session，核验入口执行字段及 Environment 配置，记录 Agent、Environment、覆写字段及配置/运行时哈希，在回放明细查看。真实 usage、工具事件、输入材料和产物保存到 PostgreSQL，媒体通过 ffprobe/ffmpeg 检查并给独立 judge 提供采样图。没有指标或关键产物时返回 unavailable / inconclusive。

替换目标 Skill 时覆盖历史图中的全部原引用，并加入当前显式关联成员，避免主 Agent 与子 Agent 同名 Skill 的版本冲突。Skill 回放的 Runtime 固定为来源连接版本及历史图中的运行时 Skill 字节；Candidate 中的 Runtime 文件不参与安装。Skill 挂载运行时转换为 xz 内嵌相同字节，两分支保持相同树哈希。独立 OpenViking 运行时直接使用来源版本的固定制品，不要求下载历史 Skill。验证期间自动进化不得改写 Candidate，防止长回放结果失效；管理员主动编辑仍递增版本并使旧结果失效。

MA 主线程回复也携带 `session_thread_id`。回放按实际主线程身份提取主回复并构造评估消息，子线程仍单独保留；原始协议事件归档不改写线程身份。

输入素材校验大小与 SHA-256 后，以原始字节和原始路径挂载到隔离 Session；原生视频、图片与 PDF 在首轮消息恢复相应内容块，视频保留 fps，其他文件提供挂载路径。图片、音频等不强制转成模型 `document` 内容块，避免固定模型仅支持 PDF 的格式限制。模型 `session.error` 保留上游错误码，便于在回放明细定位原因。

原始 Session 的素材若仍有可用的原生 File，回放先从同租户、同连接的归档中查找引用，再下载核对其字节与固定输入完全一致后复用。两个 Session 各自挂载，快照记录 File ID、SHA-256 和传输方式；复用文件不计入回放清理账本，避免删除业务文件。原文件缺失、过期或字节不匹配时上传归档副本；MA API 鉴权错误仍明确失败。这样可避免每次回放重复占用输入文件配额。

若 MA 创建 Session 时仍因资源复制返回 `OperationDenied.FileQuotaExceeded`，且全部输入均已核实为原文件、来源 Vault 中已有 `ARK_API_KEY`，回放会改用沙箱初始化下载：通过 Vault 获取文件元数据，向下载地址发送不含 MA 凭证的新请求，再次核对大小和 SHA-256 后写入 `/tmp/te-replay-inputs/` 下的对应相对路径（原生 uploads 目录只读）。首轮消息和快照记录原路径与实际路径的映射。两分支使用相同初始化代码，保留原环境设置；不会生成新的原生 File，也不会把密钥或签名 URL 写入配置。其他 API 错误不触发此兜底。

MA 文件登记可能晚于工具完成。回放从原生 Write/Edit 路径和最终回复的 `<display>` 引用识别声明产物，最多等待 60 秒回读实际字节后才进入裁判与清理；仍不可下载时明确失败，不能只用工具的写入成功消息作为交付证据。

临时资源先记外部效果意图，远端结果不明时先对账，完成后按账本精确清理。原业务 Session 与资源保留。

只修改显示名称或采集/同步周期时可复用原发现记录；凭证、运行环境及隔离配置变化仍需重新发现。人工确认按主、子线程事件识别，`session.error` 无需等待 idle 才上报。最终历史归档失败仍执行资源清理，并保留归档失败；已清理的历史账目不占用待清理查询窗口。

业务 `environment_id` 可省略。Worker 在发现、采集和同步任务开始时，按连接的入口 Agent ID 查询创建时间倒序的 Session，再读取最新业务 Session 的实际 `environment.id`；排除 TE 回放账本中的 Session 及 `te-replay:` / `te-test:` 标签会话。环境 ID 或已观测配置哈希变化时，自动保存新连接 revision 并受理重新发现，刷新回放资源，无需手动维护环境 ID。`detail.environment_observation` 保留来源 Session、创建时间、环境 ID 和配置哈希；旧 Session 恢复活动或重新拉取不会覆盖更晚的观测。尚无可用 Session 时保留显式填写的初始环境；未填写则等待并定期重试。已受理任务及历史 Session 保留其固定配置，不改写历史快照。

运行时 ready 探测的最后一个工具明确失败时直接报告探测错误，不再继续空等证明文件。即使首轮 `send` 尚未成功，分支仍保存实际 Session 快照及 `runtime_diagnostics` 中的已观测 usage、子线程状态和最终事件归档引用。这些诊断数据不替代完整回放指标，也不把初始化失败算作任务完成。

`runtime_tree_sha256` 是本次固定的目标树哈希；只有实际下载 ready 文件并核对通过后，`runtime_ready_verified` 才为 true，并附上 `runtime_ready_proof` 归档引用。仅存在配置哈希不代表运行时已经挂载成功。

## 发布与运行时

管理员在 Candidate Review 查看验证结果后 Publish。版本列表、文件编辑、导入、新建、发布、回滚和删除走 MA 原生接口。新增 Skill 可先创建为未挂载资源，再选择目标成员；已有 Skill 保留成员关联。写入返回 `202` 和 `ma.skill` Job，Worker 上传新版本、更新关联成员及祖先并回读，成功后才标记 published。回滚将历史完整包上传为新的云端版本；删除先解绑、缓存经校验的历史包，再逐个删除原生版本和 Skill 资源，每一步保存效果回执。历史业务 Session 和 TE 验证快照保留。

新 Candidate 发布未指定成员时默认挂载到所选连接的入口 Agent；明确保存空成员列表时须补选成员才能发布。手工创建资源可保持未挂载，控制台区分“已挂载”“已发布，未挂载”和“已挂载旧版本”。在“成员与 Skill”编辑关联会异步更新方舟引用，并等待回读成功；取消全部成员会卸载。仅调整挂载不增加 Skill 版本；重复提交相同内容通常复用版本，仍须核对实际 Agent 引用。

新建 Skill 及上传版本使用包中的 Skill 名称作为方舟显示名称，并回读校验。已有 `te-<32位摘要>` 内部名称在再次提交内容时通过同内容的新版本修正，随后更新成员引用；仅调整挂载不会改名。新建请求在发送前保存已有资源 ID，网络结果未知时排除这些资源，再按名称与完整文件树对账；没有唯一匹配时暂停，不重复创建。升级前已受理的上传保留原效果账本用于恢复。

候选固定生成时的云端资源身份和基线文件；云端版本变化时拒绝过期发布。MA 上传接口没有 CAS，若上传期间发生竞争，该版本保留在云端历史，发布暂停并要求重新核对；未知网络结果先对账，不重复上传。TE 仅保存不可变验证快照、候选、任务与审计，不维护第二套 MA 当前版本链。普通 System、model 和 tool 修改在引用更新时保留，历史业务 Session 不改变。

`GET /sync/ma/{connection_id}?user_id=...` 使用租户 Key 返回最后完整应用的发布快照和 ETag。部分同步保持上一完整 release；首次尚未完成返回 503。专有运行时应用可用固定入口版本与对应 Environment 创建新 Session；现有 Session 保持自己的快照。

可选 `runtime_binding` 指定 runtime Skill、Bundle 到 workspace 的路径映射、工作目录、bootstrap 和 ready 文件，供隔离回放构造固定运行时并在发送业务任务前核验 ready 文件。生产 Environment 使用 MA 已配置的版本；Skill 发现不会覆写其配置。参考 [MA API](../api/24-managed-agents.md)。

发布记录以 `environment_alias` 提供固定业务名称，同时保留 `environment_id` 和 MA 实际名称。新建 release Environment 使用 `<业务别名>-<12位摘要>` 的实际名称；配置未变化时复用当前 Environment。已有 Environment 不改名、不删除，历史 Session 与 Replay 继续使用原快照。

`archive_compression` 默认 `gzip`，可显式选择 `xz` 减少初始化脚本体积；两种方式解压后的 Bundle 字节和树哈希相同。`xz` 通过 Python 标准库 `lzma` 解压，无需安装额外的 xz 命令。部分 MA 环境在脚本尚未达到文档上限时也会返回 500，因此创建 Environment 后仍须回读验证。

`source: "skill_mount"` 从 `/mnt/skills/<skill_name>` 读取会话固定版本的文件，在全部文件的 SHA-256 校验通过后才复制并执行 bootstrap；运行时树哈希与默认内嵌方式相同。适用前提是首次启动沙箱的 Agent 已挂载该 Skill；缺件或版本不匹配直接失败，不会生成 ready 证明。

`source: "openviking"` 使用独立的固定运行时制品，让 Skill 仅携带提示词与契约。`artifact` 包含 HTTPS `base_url`、原始下载 `uri`、压缩包 `sha256`、`bytes` 和 `files` 清单（每项 `path/sha256/bytes`）。`paths` 映射该制品的原始路径，树哈希从固定清单计算。MA 使用 Vault 注入的 `OPENVIKING_API_KEY` 获取 tar.xz，禁止鉴权重定向，限制下载字节与时间，并在暂存区核验全部普通文件的路径、大小、哈希后才安装和 bootstrap。缓存按压缩包哈希保存到沙箱工作目录，每次校验后复用；有效缓存支持离线启动，损坏缓存重新下载。ready 证明记录制品 URI、压缩包哈希及缓存命中情况。控制面不在配置受理时下载，正式同步和 Replay 使用同一安装器。

`pre_setup` 在原初始化脚本前安装应用的固定依赖；`replace_setup_script:true` 显式改用 Bundle 初始化，避免重复嵌入旧 runtime。默认保留原脚本。CodingVideo 示例使用官方校验和安装 Node 22.23.3，因为当前 MA 默认 Node 20 不满足其 bootstrap；约 66 KiB 脚本真实返回 500，去重后约 33 KiB 可以创建。完整脚本仍受文档中的 256 KiB 上限约束。

可选 `runtime_binding.packages` 将运行时依赖合并到 MA 的 `packages.apt/pip`，保留来源环境的包并按完整声明去重。MA 在执行初始化脚本前准备这些依赖；CodingVideo 示例显式声明 NumPy、SciPy、librosa 与 imageio-ffmpeg 的固定版本，避免每次启动脚本经公网重新解析和下载大包。前置脚本保留有上限的兼容安装及实际版本、二进制哈希验证。声明变化进入新的连接 revision 与 Environment，历史回放仍使用原固定声明。

输出文件在工具运行后登记，已登记的追加日志不保证实时更新；排查启动进度须通过实际工具回读沙箱文件。可选 `ready_probe_message` 在同一隔离 Session 只读运行时文件，再下载实际 ready 证明，之后才开始业务任务。两分支使用相同探测，usage 计入真实指标。受理时固定当前连接与回放基础设施，来源 Agent 图、模型和 Environment 执行快照仍保持历史事实。

## CodingVideo 示例

示例在 `examples/managed-agents/codingvideo.json`，从部署文件读取单入口 Agent 与核心 Skill 的资源 ID，实际数量以云端发现结果为准。先在 CodingVideo 运行 `python -m scripts.publish_runtime` 生成已回读核验的制品描述，并配置 team-evolver Root Key 或控制台登录环境变量。回放 Environment / Vault 自动准备，无需额外的 ID 环境变量；runtime 凭证缺失时按连接状态补充。

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

模板默认先采集与生成 Candidate，经验审阅可直接执行；候选执行或完整 A/B 需开启 `replay_enabled`，
系统自动准备独立 Environment/Vault，外部工具隔离与缺失凭证仍需补齐。不要把私有环境文件提交到版本库。也可复制 CodingVideo 示例，
替换租户、连接、入口、Environment/Vault、默认用户及环境变量名称；没有专有运行时可删掉
`runtime_binding`。`$env` 从环境读取，`$json` 加 `pointer` 可读取现有部署文件，`$text` 读取无凭据
的初始化脚本，普通字段可直接填写非敏感 ID。省略 `skills`、`replay`、`model` 时保留租户现有策略。

暂停连接停止新采集和同步，已经受理的任务固定 revision；远端当前版本保留。新候选验证仅采样启用连接的 MA Case，保留被排除历史及采样数量，不将暂停连接列为发布必验目标；没有可执行 Case 时明确阻塞，不能视为验证通过。错误、漂移和清理待办可在连接状态与 release 历史中检查，再受理 reconcile。平台范围限定为同一火山方舟 MA API。
