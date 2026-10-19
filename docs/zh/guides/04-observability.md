# 链路观测指南

链路观测把 teamEvolver 自身的模型调用上报到 Langfuse，用于排查进化各阶段的 Prompt、输出、Token 用量和耗时。它是服务级配置，所有租户上报到同一个 Langfuse Project。从 Langfuse 拉取 Agent 会话属于数据源接入，使用独立的凭证，见 [数据源接入指南](./10-datasource-onboarding.md)。

代码入口：`teamEvolver/observability/langfuse.py`（运行时与配置）、`teamEvolver/llm.py`（模型调用埋点）、`teamEvolver/admin/api/settings.py`（配置接口）。

## 配置来源

配置按以下优先级合并，高优先级覆盖低优先级：

1. `LANGFUSE_*` 环境变量
2. 控制台「链路观测」保存的在线配置（`teamevolver_config.tracing`）
3. 启动配置 `langfuse_tracing_*` 字段

Worker 在每轮租户刷新时（至少间隔 10 秒）重新读取在线配置并应用，配置变化时重建 Langfuse 客户端，不需要重启进程。

| 启动配置字段 | 环境变量 | 默认 | 说明 |
|--------------|----------|------|------|
| `langfuse_tracing_enabled` | `LANGFUSE_TRACING_ENABLED` | false | 是否上报 |
| `langfuse_tracing_host` | `LANGFUSE_BASE_URL` / `LANGFUSE_HOST` | 空 | Langfuse 地址 |
| `langfuse_tracing_public_key` | `LANGFUSE_PUBLIC_KEY` | 空 | Project Public Key |
| `langfuse_tracing_secret_key` | `LANGFUSE_SECRET_KEY` | 空 | Project Secret Key |
| `langfuse_tracing_environment` | `LANGFUSE_TRACING_ENVIRONMENT` | `local` | 环境标签，只允许字母、数字、`-`、`_` |
| `langfuse_tracing_release` | `LANGFUSE_RELEASE` | 空 | 版本标签 |
| `langfuse_tracing_sample_rate` | `LANGFUSE_SAMPLE_RATE` | 1.0 | 采样率 0–1 |
| `langfuse_tracing_capture_content` | `LANGFUSE_CAPTURE_CONTENT` | true | 是否记录模型输入输出；关闭后只记录元数据与用量 |
| `langfuse_tracing_flush_at` | `LANGFUSE_FLUSH_AT` | 1 | 批量刷新条数 |
| `langfuse_tracing_flush_interval_seconds` | `LANGFUSE_FLUSH_INTERVAL` | 1.0 | 刷新间隔（秒） |
| `langfuse_tracing_timeout_seconds` | `LANGFUSE_TIMEOUT` | 30 | 请求超时（秒） |

公钥与私钥同时存在时才会创建客户端。

## 控制台与接口

平台管理员在「审查与发布 → 链路观测」配置开关、地址、采样率与是否采集模型输入输出，并测试连接。

| 方法 | 路径 | 说明 |
|------|------|------|
| GET | `/api/langfuse-tracing-config` | 在线配置，以及启动配置中是否存在公钥/私钥 |
| PUT | `/api/langfuse-tracing-config` | 保存在线配置；字段限于 `enabled`、`host`、`environment`、`release`、`sample_rate`、`capture_content`、`flush_at`、`flush_interval_seconds`、`public_key`、`secret_key` |
| POST | `/api/langfuse-tracing-config/test` | `{"host", "public_key", "secret_key"}` 测试连接，返回 202 与 `job_id` |

提交的 `secret_key` 加密保存为 Secret Ref，接口不回显明文；加密需要 `TEAMEVOLVER_SECRET_MASTER_KEY`（未设置时使用 `TEAMEVOLVER_ROOT_API_KEY`）。连接测试由 Worker 请求 `<host>/api/public/projects`，结果通过 `GET /api/jobs/{job_id}` 查询。

## 上报内容

每次模型调用生成一个 `generation` 类型的 observation，包含：

- 输入消息与工具定义（`capture_content=true` 时）
- 模型输出、`finish_reason`
- 模型名、`temperature`、`max_completion_tokens`
- Token 用量
- Metadata：组件、操作、重试次数等
- 标签：统一附加 `teamEvolver` 与 `llm`，再加阶段标签

重试的每次尝试单独记录，`metadata.attempt` 标明序号。上报失败不影响模型调用，错误记入日志 `[Langfuse] tracing unavailable` 并在状态中保留 `last_error`。

### Trace 名称

| 阶段 | Trace 名称 |
|------|------------|
| Session 分析 | `team_skills.evolution.analyze_session` |
| Trace 坏例分析 | `team_skills.evolution.analyze_trace_badcase` |
| Skill 改进 | `team-skill-evolver:evolve_skill:<skill>` |
| 新建 Skill | `team-skill-evolver:create_skill` |
| Skill 合并 | `team-skill-evolver:merge_skill:<skill>` |
| 测试集生成 | `team-skill-evolver:dataset_synthesis:<skill>` |
| Prompt 测试 | `team_skills.evolution.prompt_test.<stage_id>` |
| Session 切分 | `session_ingestion.split_session` |
| 经验模式编译 | `skill_experience_pattern.compile` |
| 未指定名称的调用 | `teamEvolver.llm.chat` |

Skill 改进、新建与合并的标签包含 `team-skill-evolver`、`team_skills.evolution`、操作名、`skill:<skill>` 以及最多 20 个源 Session ID；Metadata 最多保留 50 个源 Session ID。设置环境变量 `EVOBENCH_RUN_ID` 时追加 run id 标签，并以 `team-skill-evolver:<run_id>:<operation>:<skill>` 作为 Langfuse Session ID。

## 在 Langfuse 中查看

1. 在 Tracing 页面按 Environment 过滤部署环境。
2. 用标签 `team-skill-evolver` 查看 Skill 进化调用，用 `skill:<name>` 定位某个 Skill。
3. 用源 Session ID 标签反查某次进化消费了哪些 Session。
4. 在 Metrics 页面按模型、标签统计 Token 用量与耗时。

## 故障排查

| 现象 | 检查 |
|------|------|
| 没有任何 Trace | 开关是否开启；公钥/私钥是否都已配置；`LANGFUSE_TRACING_ENABLED` 是否覆盖为 false |
| 控制台修改不生效 | Worker 是否在运行；`LANGFUSE_*` 环境变量是否覆盖了同名字段 |
| 连接测试失败 | 从 Worker 所在主机访问 `<host>/api/public/projects`，确认网络与密钥 |
| 只有元数据没有内容 | `capture_content` 为 false |

处理敏感数据时关闭 `capture_content`，或使用自托管 Langfuse。生产环境建议采样率 0.05–0.2，排查问题时临时调到 1.0。

## 相关文档

- [配置参考](./01-configuration.md)
- [服务日志](./15-service-logging.md)
- [Prompt Studio 指南](./08-prompt-studio.md)
