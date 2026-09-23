# 配置参考

本指南详细说明 teamEvolver 的所有配置项。配置文件位于 `~/.teamEvolver/config.yaml`，可通过 CLI 命令或直接编辑 YAML 文件进行修改。

## 配置文件位置

teamEvolver 使用 YAML 格式的配置文件，默认路径为：

```
~/.teamEvolver/config.yaml
```

首次运行时，若配置文件不存在，CLI 会提示你先运行 `teamEvolver config` 进行初始化。配置文件由 `teamEvolver/config_store/defaults.py` 中的默认值与用户自定义值深度合并而成。

## CLI 配置命令

使用 `teamEvolver config` 命令读取或修改配置：

```bash
# 查看当前所有配置
teamEvolver config show

# 读取单个配置项
teamEvolver config <key>

# 设置单个配置项（支持点分隔的嵌套键）
teamEvolver config <key> <value>
```

示例：

```bash
teamEvolver config llm.api_key sk-xxxxxxxx
teamEvolver config service.port 52010
teamEvolver config sharing.enabled true
teamEvolver config langfuse.tracing_enabled true
```

CLI 会自动将字符串值转换为合适的类型（布尔值、整数、浮点数）。

## 配置节说明

### team 节

团队基本信息配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `display_name` | string | `"Team"` | 团队显示名称，在控制台和共享技能中标识团队。可通过环境变量 `EVOLVE_TEAM_DISPLAY_NAME` 覆盖。 |

### llm 节

进化流水线使用的大语言模型配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `provider` | string | `"custom"` | LLM 服务提供商，目前支持自定义 OpenAI 兼容接口。 |
| `model_id` | string | `"doubao-seed-evolving"` | 模型标识符。 |
| `api_base` | string | `"https://ark.cn-beijing.volces.com/api/v3"` | API 基础 URL，必须是 OpenAI `/chat/completions` 兼容端点。 |
| `api_key` | string | `""` | API 密钥，用于认证上游模型服务。 |
| `max_tokens` | integer | `100000` | 单次 LLM 调用的最大输出 token 数。 |
| `temperature` | float | `0.4` | 采样温度，范围 0.0–2.0。 |
| `max_concurrency` | integer | `8` | 单租户 LLM 最大并发数；每个租户拥有独立执行池和并发额度。 |
| `queue_capacity` | integer | `64` | 单租户 LLM 待处理容量（包含执行中的调用），队列满只影响当前租户。 |

### service 节

HTTP 服务监听配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `port` | integer | `52010` | 服务监听端口。 |
| `host` | string | `"0.0.0.0"` | 服务绑定地址。生产环境建议改为 `"127.0.0.1"` 并通过反向代理暴露。 |

### skills 节

本地技能库配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用技能管理功能。 |
| `dir` | string | `"~/.hermes/skills"` | 本地技能目录路径。默认指向 Hermes 的技能目录以便无缝集成。 |

### sharing 节

Skill 与 Session 可分别选择存储后端。`skill_backend` 支持本地/NAS 或 OpenViking，保存正式 Skill、候选、证据、注册表及版本历史；`session_backend` 保存 Session 与运行状态。Skill 使用本地后端时可异步镜像到 OpenViking；OpenViking 不可用时可按 `local_fallback_enabled` 回退。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用技能云端共享。 |
| `backend` | string | `"viking"` | 兼容共享后端，支持 `"local"` 或 `"viking"`；新配置优先使用分域字段。 |
| `viking_deployment` | string | `"cloud"` | OpenViking 部署模式：`"cloud"`（火山引擎托管）或 `"local"`（自托管 openviking-server）。 |
| `viking_endpoint` | string | `""` | OpenViking API 端点覆盖。留空时根据 `viking_deployment` 推导；远程自建实例填写其可达 URL。 |
| `viking_api_key` | string | `""` | Trusted 模式 Root Key；个人记忆与团队资源统一使用。 |
| `viking_personal_api_key` | string | `""` | 已废弃兼容字段，不再参与 Workspace 鉴权。 |
| `viking_personal_api_keys` | list | `[]` | 已废弃兼容字段，不再参与 Workspace 鉴权。 |
| `viking_team_api_key` | string | `""` | Root Key 的兼容字段名，用于团队资源、技能同步与团队记忆聚合。 |
| `viking_root_prefix` | string | `"team-skill-evolver"` | OpenViking 中 teamEvolver 资源的命名空间根前缀，请勿随意修改。 |
| `viking_agent` | string | (常量) | OpenViking Agent 命名空间，由代码常量固定。 |
| `viking_account` | string | `"default"` | Viking 账户标识。 |
| `viking_user` | string | `"team"` | 访问团队共享资源时发送的 OpenViking 用户标识。 |
| `viking_personal_user` | string | `""` | 旧默认个人 Name；控制台用户绑定优先。 |
| `viking_customer_id` | string | `""` | 客户 ID，用于 DreamCycle 记忆空间定位。 |
| `viking_group_id` | string | `""` | 分组 ID。 |
| `viking_agent_id` | string | `""` | Agent ID。 |
| `user_alias` | string | `""` | 用户别名，用于会话归属标记。 |
| `auto_pull_on_start` | boolean | `true` | 启动时自动从云端拉取最新技能。 |
| `push_min_injections` | integer | `5` | 推送到云端前技能的最小注入次数门槛。 |
| `push_min_effectiveness` | float | `0.3` | 推送到云端前技能的最低有效率门槛。 |
| `session_upload_interval` | integer | `0` | 会话自动上传间隔（秒），0 表示不上传。 |
| `skill_reload_mode` | string | `"poll"` | 技能重载模式：`"off"`（关闭）、`"poll"`（轮询）、`"callback"`（回调）。 |
| `skill_reload_interval_seconds` | integer | `30` | 轮询模式下的技能检查间隔（秒），最小值为 5。 |
| `endpoint` | string | `""` | 通用端点（留空时使用 viking_endpoint）。 |
| `skill_backend` | string | `""` | Skill 专用后端，支持 `"local"`（本地盘/NAS）或 `"viking"`；留空时使用 `"local"`。 |
| `session_backend` | string | `""` | Session 专用后端，支持 `"local"`、`"viking"` 或 `"postgres"`；启用 `storage_pg` 时强制为 `"postgres"`。 |
| `local_fallback_enabled` | boolean | `true` | OpenViking 不可用（连接错误/超时/5xx）时自动回退到本地存储；4xx 不触发回退。 |
| `local_root` | string | `""` | 本地对象存储根目录，留空时使用 `~/.teamEvolver/local_store`；回退目录按实例隔离命名。 |
| `skill_local_root` | string | `""` | Skill 本地存储根目录，可指向 NAS 挂载点；留空时继承 `local_root`。 |
| `skill_mirror_enabled` | boolean | `true` | 是否将本地 Skill 库异步镜像到 OpenViking（通过持久化 spool 外发队列）。 |
| `skill_mirror_spool_dir` | string | `""` | Skill 镜像 spool 目录，留空时使用 `~/.teamEvolver/skill_mirror_spool`。 |

### evolve 节

进化流水线核心参数配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `interval_seconds` | integer | `600` | 进化轮次间隔（秒），即每隔多久执行一次进化循环。 |
| `publish_mode` | string | `"validated"` | 候选 Skill 发布模式：`"validated"`（进入验证与审核链路）或 `"direct"`（直接发布）。 |
| `human_review_enabled` | boolean | `true` | 是否启用人工审核流程。 |
| `human_review_timeout_seconds` | integer | `86400` | 人工审核超时时间（秒），默认 24 小时。 |
| `evidence_enabled` | boolean | `true` | 是否启用证据收集机制。 |
| `evidence_max_entries` | integer | `400` | 证据库最大条目数。 |
| `evidence_recent_limit` | integer | `20` | 近期证据窗口大小。 |
| `evidence_historical_limit` | integer | `20` | 历史证据窗口大小。 |
| `evidence_replay_cases_per_window` | integer | `1` | 每个证据窗口的回放用例数。 |
| `evidence_change_debt_threshold` | integer | `3` | 变更债务阈值，超过此数触发强制进化。 |
| `dataset_synthesis_enabled` | boolean | `true` | 是否启用测试集自动合成。 |
| `dataset_test_cases` | integer | `2` | 每次合成生成的测试用例数。 |
| `dataset_min_requirements` | integer | `12` | 测试用例最少检查项数量。 |
| `dataset_max_requirements` | integer | `24` | 测试用例最多检查项数量。 |
| `dataset_disclosure_batch_size` | integer | `4` | 渐进披露批量大小。 |
| `validation_max_rejections` | integer | `1` | 连续拒绝多少次后暂停该技能的进化。 |
| `use_session_judge` | boolean | `true` | 是否启用进化阶段的 Session 评分 Judge（按任务完成度/响应质量/效率/工具使用四维打分并给出中文理由）；ingest 阶段的价值分类器独立运行，不受此项控制。 |
| `candidate_coalesce_enabled` | boolean | `true` | 是否启用候选合并。 |
| `max_parallel_groups` | integer | `4` | 单个进化周期并行处理的 Skill 分组上限。 |
| `drain_batch_size` | integer | `25` | 持续拉取模式下每批读取并处理的 Session 数量。 |
| `drain_batch_delay_seconds` | float | `1.0` | 相邻两批 drain 之间的间隔（秒），防止压垮存储层。 |
| `drain_max_per_cycle` | integer | `0` | 单个进化周期最多 drain 的 Session 数，0 表示不设上限；仍有积压时下一周期约 1 秒后自动启动。 |
| `min_group_sessions` | integer | `2` | 组建团队证据分组所需的最少 Session 数，不足则跳过该分组本轮进化。 |
| `min_group_users` | integer | `2` | 组建团队证据分组所需的最少用户数，不足则跳过该分组本轮进化。 |
| `bundle_text_extensions` | list | `[".py", ".sh"]` | 技能包中视为文本文件的扩展名列表。 |
| `bundle_max_file_bytes` | integer | `262144` | 技能包单个文件最大字节数（256KB）。 |
| `bundle_max_prompt_bytes` | integer | `786432` | 技能包最大 Prompt 字节数（768KB）。 |
| `bundle_allow_delete` | boolean | `true` | 是否允许进化过程删除文件。 |
| `bundle_static_checks_enabled` | boolean | `true` | 是否启用技能包静态检查。 |
| `server_url` | string | `"http://127.0.0.1:52010"` | 进化服务自引用 URL。 |

### dreamcycle 节

DreamCycle 记忆维护引擎配置。DreamCycle 是 teamEvolver 的自动化记忆整理子系统，在非活跃时段运行。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | 是否启用 DreamCycle 记忆维护。 |
| `auto_start` | boolean | `false` | 是否随主服务自动启动。 |
| `active_start_hour` | integer | `0` | 活跃窗口开始小时（0-23，24 小时制）。默认凌晨 0 点。 |
| `active_end_hour` | integer | `6` | 活跃窗口结束小时（0-23）。默认凌晨 6 点。 |
| `rounds_per_window` | integer | `3` | 每个活跃窗口执行的轮次数量。 |
| `round_interval_minutes` | integer | `90` | 轮次间隔（分钟）。 |
| `max_turns_per_job` | integer | `25` | 单个 Job 最大对话轮次。 |
| `max_consecutive_errors` | integer | `3` | 连续错误次数阈值，超过后退避重试。 |
| `retry_delay_seconds` | integer | `300` | 错误后退避等待时间（秒）。 |
| `enabled_jobs` | list | `["team_overview","deduplication","cleanup","onboarding_check","consolidate"]` | 启用的 Job 列表。可用 Job：`team_overview`、`deduplication`、`cleanup`、`onboarding_check`、`consolidate`。 |
| `llm_model` | string | `""` | DreamCycle 使用的模型，留空则复用全局 LLM 配置。 |
| `llm_base_url` | string | `""` | DreamCycle 专用 API Base URL。 |
| `llm_api_key` | string | `""` | DreamCycle 专用 API Key。 |
| `llm_max_tokens` | integer | `4096` | DreamCycle LLM 最大输出 token。 |
| `temperature` | float | `0.3` | DreamCycle LLM 采样温度。 |
| `embed_model` | string | `""` | 嵌入模型名称，配置后启用语义去重。 |
| `embed_base_url` | string | `""` | 嵌入模型 API Base URL。 |
| `embed_api_key` | string | `""` | 嵌入模型 API Key。 |
| `dedup_merge_threshold` | float | `0.86` | 语义相似度合并阈值（0-1）。 |
| `dedup_warn_threshold` | float | `0.72` | 语义相似度警告阈值（0-1）。 |
| `customer_id` | string | `""` | 目标客户 ID。 |
| `state_dir` | string | `""` | 状态文件目录。 |
| `log_level` | string | `"INFO"` | 日志级别。 |
| `daemon_command` | string | `"dreamcycle --daemon"` | DreamCycle 守护进程启动命令。 |
| `trigger_command` | string | `"dreamcycle --once"` | DreamCycle 单次触发命令。 |
| `viking_agent` | string | `"dreamcycle"` | DreamCycle 在 OpenViking 中的 Agent 命名空间。 |
| `job_prompts` | dict | `{}` | 各 Job 的 Prompt 覆盖配置。 |
| `job_settings` | dict | `{}` | 各 Job 的运行时参数覆盖配置。 |

### aggregation 节

跨 User 团队 Memory 聚合配置。控制台默认使用 `sharing` 中配置的 Endpoint、Account 和 Trusted Root Key；独立接口可覆盖 Endpoint、Account，并在 `root_key`（Trusted）与 `admin_key`（API-key）之间二选一。请求凭据不持久化。

API-key 模式读取 Admin 用户列表中的现存用户明文 Key，不执行 Key 生成或轮换；启用 API Key 哈希、仅返回 `key_prefix` 的 OpenViking 部署不支持该兼容路径。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | 聚合功能的配置标记。聚合仍由管理员在控制台显式触发。 |
| `shared_knowledge_prefix` | string | `"shared-knowledge"` | 最终团队 Memory 根：`viking://resources/<prefix>/`。可在「进化链路 → 团队 Memory 自进化」热更新。 |
| `okf_skill_uri` | string | `"viking://agent/skills/team-memory-okf"` | 账号级共享聚合 Skill；所有参与身份读取同一份内容和 revision。 |
| `insight_skill_uri` | string | `""` | 预留的洞察 Skill 标识，当前聚合执行链路尚未使用。 |
| `key_seed` | string | `"teamevolver-aggregation"` | 兼容保留字段；当前执行链路不使用它生成用户 Key。 |
| `staging_dir` | string | `"staging"` | merge 身份私有 Resources 下的工作目录段；原始快照不会写入 account 共享 Resources。 |
| `kinds` | list | `[]` | 个人 Memory 类别；空列表使用内置集合 `profile/entities/preferences/events/cases/patterns/trajectories/experiences/tools/skills`。 |
| `max_users_per_batch` | integer | `12` | 兼容保留字段；确定性 staging 不使用该值。 |
| `account_user_limit` | integer | `50000` | 单次 Account 全量聚合的用户安全上限。 |
| `account_user_page_size` | integer | `1000` | 稳定读取用户清单的分页大小，最大 1000。 |
| `phase1_concurrency` | integer | `6` | Phase 1 用户级确定性快照最大并发数。 |
| `merge_fan_in` | integer | `4` | tree-reduce 每轮合并的最大源数，运行时限制为 2–15。 |
| `merge_concurrency` | integer | `4` | merge 分组最大并发数。 |
| `partition_threshold` | integer | `512` | staging 用户超过该值后启用私有固定哈希分区归并。 |
| `partition_count` | integer | `256` | 私有临时分区数，范围 16–1024。 |
| `run_detail_limit` | integer | `2000` | 实时状态中最多保留的分组明细数。 |
| `compile_runtime_timeout_seconds` | integer | `3000` | 单个 compile 任务的运行超时秒数，最小 60。 |
| `state_dir` | string | `""` | 聚合状态目录；留空使用 `~/.team_memory/`。 |

### validation 节

候选技能校验配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用后台校验。 |
| `mode` | string | `"true_replay"` | 校验模式：`"true_replay"`（真回放，完整工作区隔离）或 `"replay"`（轻量回放）。 |
| `max_concurrency` | integer | `1` | 并发校验任务数上限。 |
| `required_results` | integer | `3` | 发布所需的有效校验结果数。 |
| `required_approvals` | integer | `2` | 发布所需的审批通过数。 |
| `agentshub_url` | string | `""` | Pi Agent 服务 URL（分布式回放 HTTP 端点）。配置项名保留历史命名。 |
| `agentshub_api_key` | string | `""` | Pi Agent Replay/Sync API Key。配置项名保留历史命名。 |
| `idle_after_seconds` | integer | `300` | 空闲等待时间（秒），超过后 Worker 进入休眠。 |
| `poll_interval_seconds` | integer | `60` | 轮询间隔（秒）。 |
| `max_jobs_per_day` | integer | `5` | 每日最大校验任务数。 |

### langfuse 节

Langfuse 集成分为两种独立模式：入站会话拉取使用租户级数据源连接；出站追踪使用服务级连接，所有租户统一上报且不能通过租户配置覆盖。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | 是否启用入站会话拉取模式。 |
| `host` | string | `"https://cloud.langfuse.com"` | Langfuse 服务地址。自托管实例请改为对应地址。 |
| `public_key` | string | `""` | Langfuse Public Key，用于 API 访问。 |
| `secret_key` | string | `""` | Langfuse Secret Key。 |
| `tracing_enabled` | boolean | `false` | 是否启用出站 LLM 调用追踪。 |
| `tracing_host` | string | `""` | 全局观测 Langfuse 地址，独立于租户数据源 `host`。 |
| `tracing_public_key` | string | `""` | 全局观测 Project Public Key。 |
| `tracing_secret_key` | string | `""` | 全局观测 Project Secret Key。 |
| `tracing_environment` | string | `"local"` | 追踪环境标签，用于在 Langfuse UI 中区分不同部署环境（如 production、staging、local）。 |
| `tracing_release` | string | `""` | 追踪版本标签。 |
| `tracing_sample_rate` | float | `1.0` | 追踪采样率（0.0-1.0），1.0 表示全量采样。 |
| `tracing_capture_content` | boolean | `true` | 是否捕获 LLM 输入输出内容。关闭后仅记录元数据。 |
| `tracing_flush_at` | integer | `1` | 累积多少条追踪后批量刷新。 |
| `tracing_flush_interval_seconds` | float | `1.0` | 定时刷新间隔（秒）。 |
| `timeout_seconds` | integer | `30` | Langfuse API 请求超时（秒）。 |
| `page_limit` | integer | `50` | 分页拉取时每页大小。 |
| `max_sessions` | integer | `100` | 单次拉取最大会话数。 |
| `default_environment` | list | `[]` | 默认拉取过滤的环境标签列表。 |
| `default_user_id` | string | `""` | 默认拉取过滤的用户 ID。 |
| `default_tags` | list | `[]` | 默认拉取过滤的标签列表。 |
| `default_release` | string | `""` | 默认拉取过滤的版本。 |
| `default_version` | string | `""` | 默认拉取过滤的版本号。 |
| `default_trace_name` | string | `""` | 默认拉取过滤的 Trace 名称。 |
| `mapper_enabled` | boolean | `false` | 已弃用：旧版单 mapper 开关，首次读取时自动迁移进 `mappers`（名为 `default` 的兜底条目），控制台首次保存后删除。 |
| `mapper_code` | string | `""` | 已弃用：旧版单 mapper 源码，迁移行为同上。 |
| `mappers` | list | 无 | **Per-Agent 映射注册表**。有序列表，每条为 `{name, enabled, note?, code, match}`；拉取时按列表顺序匹配，首个命中的启用条目负责该 trace 的映射。 |

#### 映射注册表（按 Agent 路由）

不同 Agent 的 Langfuse 数据形态各异，`mappers` 允许为每个 Agent 配置独立的映射逻辑与路由规则：

```yaml
langfuse:
  mappers:
    - name: openclaw-zhang
      enabled: true
      note: "openclaw agent，用户 zhang"
      match:
        trace_names: ["openclaw-turn"]        # fnmatch 通配，区分大小写
        tags: ["openclaw"]                     # 任一命中即可（ANY-of）
        session_id_patterns: ["agent:main:openresponses-user:42749155_*"]
      code: |
        def map_trace(trace, observations): ...
        def map_session(converted, session, traces):
            return {"user_alias": "zhang"}     # 可选会话钩子
    - name: default
      enabled: true
      match: {}                                # 空匹配 = 兜底，务必放最后
      code: |
        def map_trace(trace, observations): ...
```

**匹配语义**：`match` 中三类条件（trace 名称通配、tags、sessionId 模式）按 AND 组合，留空的组不设限，全空即兜底；列表顺序即优先级，首个命中的启用条目生效。条目也可以只定义 `map_session`（会话钩子条目），它不参与 trace 映射，但会对命中的会话生效。

#### 自定义 Trace 映射（进化标准格式）

Langfuse 的 trace 与 observation 结构统一，observation 仅通过 `parentObservationId` 形成嵌套。将其映射为进化所需的标准格式本质上是机械的，因此除内置映射外，teamEvolver 允许管理员编写一个函数亲自掌控这一步：

```python
def map_trace(trace, observations):
    # trace:        dict —— 单个 Langfuse trace（input/output/metadata/...）
    # observations: list[dict] —— 该 trace 的 observation（扁平列表，靠 parentObservationId 嵌套）
    # 返回:         dict —— 进化标准格式的 turn；返回部分字段会深合并到内置映射之上，
    #               返回 None 表示完全沿用内置映射。
    usage = (trace.get("metadata") or {}).get("usage") or {}
    return {
        "prompt_text": str(trace.get("input") or ""),
        "response_text": str(trace.get("output") or ""),
        "metrics": {"total_tokens": int(usage.get("total") or 0)},
    }
```

- 函数在受限环境中执行：内置 `json / re / math / datetime / collections / itertools / functools`，禁用 `import` 与文件访问。仅管理员可编辑（属于可执行配置）。
- 返回值会**深合并**到内置映射结果之上——只需覆盖关心的字段，其余自动回退到内置逻辑。
- 条目代码还可定义会话级钩子 `map_session(converted, session, traces)`：在整场会话转换完成后调用，返回部分会话字典（如 `user_alias`、`title`、`system_prompt`）深合并到会话上；同一会话命中多个带钩子的条目时按列表顺序依次生效，钩子优先于拉取时的 `user_alias` 默认值。
- 在控制台 Langfuse 页的「映射注册表」面板可增删条目、调整顺序、逐条插入参考模板与试运行（显示路由是否命中），面板级「路由预览」可粘贴样例 trace 查看整张注册表的路由结果。面板右上角的「标准格式说明」按钮会弹出进化标准格式（各字段含义 + 完整示例）的说明。
- 单条代码在拉取时抛错只影响该 trace（自动回退内置映射），不会中断整批拉取；编译失败的条目启动时跳过并在控制台路由预览中标红。

### mining 节

Skill Miner（文档到技能挖掘）配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `model.provider` | string | 继承全局 llm | 挖掘专用模型提供商。 |
| `model.model_id` | string | 继承全局 llm | 挖掘专用模型 ID。 |
| `model.base_url` | string | 继承全局 llm | 挖掘专用 API Base URL。 |
| `model.api_key` | string | 继承全局 llm | 挖掘专用 API Key。 |
| `model.max_tokens` | integer | 继承全局 llm | 挖掘模型最大输出 token。 |
| `model.context_length` | integer | `240000` | 模型上下文窗口大小。 |
| `model.temperature` | float | `0.2` | 挖掘模型采样温度。 |
| `pipeline.max_rounds` | integer | `3` | 反思环最大轮次。 |
| `pipeline.max_retries` | integer | `2` | 单步最大重试次数。 |
| `pipeline.retry_backoff_seconds` | float | `0.8` | 重试退避时间（秒）。 |
| `pipeline.oneshot_timeout_seconds` | integer | `1800` | 单次挖掘超时（秒），默认 30 分钟。 |
| `pipeline.step1_validation_retries` | integer | `1` | Step1 样本包校验失败后的重试次数。 |
| `pipeline.strict_step1` | boolean | `true` | Step1 校验失败是否中止本轮。 |
| `pipeline.benchmark_target_total` | integer | `16` | Benchmark 目标题目总数。 |
| `pipeline.benchmark_difficulty_dist` | string | `"easy:4,medium:7,hard:5"` | Benchmark 难度分布。 |
| `pipeline.benchmark_max_turns` | integer | `5` | 多轮 Benchmark 最大对话轮次。 |
| `prompts` | dict | `{}` | 各挖掘阶段的 Prompt 覆盖。 |

### openrouter 节

OpenRouter 备用路由配置（可选）。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `app_name` | string | `"teamEvolver"` | OpenRouter 应用名称。 |
| `app_url` | string | `""` | 应用 URL。 |
| `route` | string | `"fallback"` | 路由策略。 |
| `fallback_models` | string | `""` | 备用模型列表。 |
| `data_policy` | string | `""` | 数据策略。 |

## 环境变量覆盖

除了 YAML 配置文件外，以下环境变量可以覆盖对应配置项（优先级最高）：

| 环境变量 | 对应配置项 |
|----------|-----------|
| `EVOLVE_TEAM_DISPLAY_NAME` | `team.display_name` |
| `EVOLVE_MODEL` | `llm.model_id` |
| `EVOLVE_LLM_MAX_TOKENS` | `llm.max_tokens` |
| `EVOLVE_LLM_TEMPERATURE` | `llm.temperature` |
| `EVOLVE_USE_SESSION_JUDGE` | `evolve.use_session_judge` |
| `EVOLVE_PUBLISH_MODE` | `evolve.publish_mode` |
| `EVOLVE_VALIDATION_MAX_REJECTIONS` | `evolve.validation_max_rejections` |
| `EVOLVE_HUMAN_REVIEW_ENABLED` | `evolve.human_review_enabled` |
| `EVOLVE_HUMAN_REVIEW_TIMEOUT_SECONDS` | `evolve.human_review_timeout_seconds` |
| `EVOLVE_INTERVAL` | `evolve.interval_seconds` |
| `EVOLVE_MAX_PARALLEL_GROUPS` | `evolve.max_parallel_groups` |
| `EVOLVE_DRAIN_MAX_PER_CYCLE` | `evolve.drain_max_per_cycle` |
| `EVOLVE_MIN_GROUP_SESSIONS` | `evolve.min_group_sessions` |
| `EVOLVE_MIN_GROUP_USERS` | `evolve.min_group_users` |
| `TEAMEVOLVER_LLM_CONCURRENCY` | `llm.max_concurrency`，可通过租户 overrides 单独设置 |
| `TEAMEVOLVER_LLM_QUEUE_CAPACITY` | `llm.queue_capacity`，可通过租户 overrides 单独设置 |
| `EVOLVE_LLM_MAX_CONCURRENCY` | 进化运行时的单租户 LLM 并发覆盖值 |
| `EVOLVE_LLM_QUEUE_CAPACITY` | 进化运行时的单租户 LLM 队列容量覆盖值 |
| `TEAMEVOLVER_TENANT_CONCURRENCY` | 同时执行进化周期的租户总数上限；默认 `0` 表示不设总上限 |
| `EVOLVE_STORAGE_BACKEND` | `sharing.backend` |
| `EVOLVE_STORAGE_FALLBACK` | `sharing.local_fallback_enabled` |
| `EVOLVE_STORAGE_LOCAL_ROOT` | `sharing.local_root` |
| `EVOLVE_SKILL_STORAGE_BACKEND` | `sharing.skill_backend` |
| `EVOLVE_SKILL_STORAGE_LOCAL_ROOT` | `sharing.skill_local_root` |
| `EVOLVE_SKILL_MIRROR` | `sharing.skill_mirror_enabled` |
| `EVOLVE_SKILL_MIRROR_SPOOL_DIR` | `sharing.skill_mirror_spool_dir` |
| `EVOLVE_EVIDENCE_ENABLED` | `evolve.evidence_enabled` |
| `EVOLVE_EVIDENCE_MAX_ENTRIES` | `evolve.evidence_max_entries` |
| `EVOLVE_INGEST_API_KEY` | 全局 ingest 端点 API Key |
| `TEAMEVOLVER_PROXY_API_KEY` | 模型代理 API Key |
| `TEAMEVOLVER_SKILL_STORAGE_BACKEND` | `sharing.skill_backend` |
| `TEAMEVOLVER_SKILL_STORAGE_ROOT` | `sharing.skill_local_root` |
| `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` | `langfuse.tracing_host` |
| `LANGFUSE_PUBLIC_KEY` | `langfuse.tracing_public_key` |
| `LANGFUSE_SECRET_KEY` | `langfuse.tracing_secret_key` |
| `LANGFUSE_TRACING_ENABLED` | `langfuse.tracing_enabled` |
| `LANGFUSE_TRACING_ENVIRONMENT` | `langfuse.tracing_environment` |
| `LANGFUSE_SAMPLE_RATE` | `langfuse.tracing_sample_rate` |
| `ARK_API_KEY` | 火山方舟 API Key（Skill Miner 使用） |

## 配置文件示例

以下是一个完整的 `~/.teamEvolver/config.yaml` 示例：

```yaml
team:
  display_name: "我的团队"

llm:
  provider: "custom"
  model_id: "doubao-seed-evolving"
  api_base: "https://ark.cn-beijing.volces.com/api/v3"
  api_key: "sk-xxxxxxxx"
  max_tokens: 100000
  temperature: 0.4
  max_concurrency: 8
  queue_capacity: 64

service:
  port: 52010
  host: "127.0.0.1"

skills:
  enabled: true
  dir: "~/.hermes/skills"

sharing:
  enabled: true
  backend: "viking"
  viking_deployment: "local"
  # 自建服务在其他机器时填写其可达地址；留空则使用 http://localhost:1933
  viking_endpoint: "http://10.0.0.8:1933"
  viking_account: "default"
  viking_user: "team"
  # 兼容字段名；语义为服务/admin key，通常直接使用管理员 OpenViking Key。
  viking_team_api_key: "root-or-trusted-key"
  skill_reload_mode: "poll"
  skill_reload_interval_seconds: 30

evolve:
  interval_seconds: 600
  publish_mode: "validated"
  human_review_enabled: true
  human_review_timeout_seconds: 86400
  evidence_max_entries: 400
  dataset_test_cases: 2
  dataset_min_requirements: 12
  validation_max_rejections: 1

dreamcycle:
  enabled: false
  auto_start: false
  active_start_hour: 0
  active_end_hour: 6
  rounds_per_window: 3
  enabled_jobs:
    - team_overview
    - deduplication
    - cleanup
    - onboarding_check
    - consolidate

aggregation:
  enabled: true
  shared_knowledge_prefix: "shared-knowledge"
  staging_dir: "staging"
  account_user_limit: 50000
  account_user_page_size: 1000
  phase1_concurrency: 6
  merge_fan_in: 4
  merge_concurrency: 4
  partition_threshold: 512
  partition_count: 256
  run_detail_limit: 2000

validation:
  enabled: true
  mode: "true_replay"
  max_concurrency: 1
  required_results: 3
  required_approvals: 2

langfuse:
  enabled: false
  host: "https://cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_host: "https://observability-langfuse.example.com"
  tracing_public_key: "pk-lf-observability"
  tracing_secret_key: "sk-lf-observability"
  tracing_environment: "production"
  tracing_sample_rate: 0.1
```

## 配置热重载

大部分配置项在修改后需要重启服务才能生效。以下配置项支持通过 Web 控制台动态修改而无需重启：

- LLM 模型参数（`llm.*`）
- 进化流水线参数（`evolve.*`）
- 校验参数（`validation.*`）
- DreamCycle 参数（`dreamcycle.*`）
- Langfuse 参数（`langfuse.*`）
- Skill Miner 参数（`mining.*`）
- OpenViking 部署、Endpoint、Account 与服务 Key（通过「运行状态」）
- 团队 Memory 输出前缀（通过「进化链路 → 团队 Memory 自进化」）
- Prompt 覆盖（通过 Prompt Studio）

通过 CLI `teamEvolver config <key> <value>` 修改的其他配置，建议重启服务后使用。
