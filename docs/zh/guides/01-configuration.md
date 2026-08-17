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

技能共享与 OpenViking 云端同步配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `true` | 是否启用技能云端共享。 |
| `backend` | string | `"viking"` | 共享后端，目前仅支持 `"viking"`（OpenViking）。 |
| `viking_deployment` | string | `"cloud"` | OpenViking 部署模式：`"cloud"`（火山引擎托管）或 `"local"`（自托管 openviking-server）。 |
| `viking_endpoint` | string | `""` | OpenViking API 端点。留空时根据 `viking_deployment` 自动推导。 |
| `viking_api_key` | string | `""` | 通用 API 密钥（向后兼容，推荐使用分域密钥）。 |
| `viking_personal_api_key` | string | `""` | 个人空间 API 密钥。 |
| `viking_personal_api_keys` | list | `[]` | 多个个人空间 API 密钥列表。 |
| `viking_team_api_key` | string | `""` | 团队空间 API 密钥。 |
| `viking_root_prefix` | string | `"team-skill-evolver"` | OpenViking 中 teamEvolver 资源的命名空间根前缀，请勿随意修改。 |
| `viking_agent` | string | (常量) | OpenViking Agent 命名空间，由代码常量固定。 |
| `viking_account` | string | `"default"` | Viking 账户标识。 |
| `viking_user` | string | `"default"` | Viking 用户标识。 |
| `viking_personal_user` | string | `""` | 个人空间用户名。 |
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
| `skill_backend` | string | `""` | 技能专用后端（留空时使用 backend）。 |
| `session_backend` | string | `""` | 会话专用后端（留空时使用 backend）。 |

### evolve 节

进化流水线核心参数配置。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `interval_seconds` | integer | `600` | 进化轮次间隔（秒），即每隔多久执行一次进化循环。 |
| `publish_mode` | string | `"validated"` | 候选技能发布模式：`"validated"`（校验通过后自动发布）、`"manual"`（全部人工审核）、`"off"`（不自动发布）。 |
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
| `use_session_judge` | boolean | `true` | 是否使用会话价值分类器。 |
| `candidate_coalesce_enabled` | boolean | `true` | 是否启用候选合并。 |
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

Langfuse 可观测性与会话拉取配置。Langfuse 集成分为两种独立模式：入站会话拉取（从 Langfuse 拉取会话进入进化流水线）和出站追踪（将进化过程中的 LLM 调用发送到 Langfuse）。

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | 是否启用入站会话拉取模式。 |
| `host` | string | `"https://cloud.langfuse.com"` | Langfuse 服务地址。自托管实例请改为对应地址。 |
| `public_key` | string | `""` | Langfuse Public Key，用于 API 访问。 |
| `secret_key` | string | `""` | Langfuse Secret Key。 |
| `tracing_enabled` | boolean | `false` | 是否启用出站 LLM 调用追踪。 |
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
| `EVOLVE_EVIDENCE_ENABLED` | `evolve.evidence_enabled` |
| `EVOLVE_EVIDENCE_MAX_ENTRIES` | `evolve.evidence_max_entries` |
| `EVOLVE_INGEST_API_KEY` | 全局 ingest 端点 API Key |
| `TEAMEVOLVER_PROXY_API_KEY` | 模型代理 API Key |
| `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` | `langfuse.host` |
| `LANGFUSE_PUBLIC_KEY` | `langfuse.public_key` |
| `LANGFUSE_SECRET_KEY` | `langfuse.secret_key` |
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

service:
  port: 52010
  host: "127.0.0.1"

skills:
  enabled: true
  dir: "~/.hermes/skills"

sharing:
  enabled: true
  backend: "viking"
  viking_deployment: "cloud"
  viking_personal_api_key: "vk-xxxxxxxx"
  viking_team_api_key: "vk-yyyyyyyy"
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
  enabled: true
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
- Prompt 覆盖（通过 Prompt Studio）

通过 CLI `teamEvolver config set` 修改的配置会在下次进化轮次开始时自动加载。
