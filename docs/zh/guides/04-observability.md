# Langfuse 可观测性指南

teamEvolver 深度集成 Langfuse 提供 LLM 应用可观测能力。Langfuse 集成包含两种独立工作模式，可分别启用：

1. **入站会话拉取（Session Pull）**：从 Langfuse 拉取已有的 Agent 会话数据，送入 teamEvolver 进化流水线
2. **出站追踪（Tracing）**：将 teamEvolver 自身进化过程中的 LLM 调用发送到 Langfuse 进行追踪分析

相关代码位于：
- `teamEvolver/observability/langfuse.py`：追踪运行时和配置管理
- `teamEvolver/integrations/langfuse_client.py`：Langfuse v3 公共 API HTTP 客户端
- `teamEvolver/integrations/langfuse_pull.py`：会话拉取编排层
- `teamEvolver/integrations/langfuse_convert.py`：Langfuse 会话格式到 teamEvolver 格式的转换

## 两种模式对比

| 维度 | 入站会话拉取 | 出站追踪 |
|------|-------------|---------|
| 方向 | Langfuse → teamEvolver | teamEvolver → Langfuse |
| 配置开关 | `langfuse.enabled` | `langfuse.tracing_enabled` |
| 用途 | 将外部 Agent 的会话作为进化输入 | 调试和监控进化流水线自身的 LLM 调用 |
| 数据内容 | 完整会话轨迹、工具调用、评分 | 进化各阶段的 System Prompt、User Message、模型输出、Token 用量 |
| 运行方式 | 定时/手动拉取 | 每次 LLM 调用自动捕获 |
| 对进化的影响 | 为进化提供原材料 | 无副作用，fail-open 设计 |

两种模式共享同一套 Langfuse 凭证（`public_key`/`secret_key`）和 `host` 配置，但可以独立开关。

## 配置说明

所有 Langfuse 相关配置位于 config.yaml 的 `langfuse` 节：

### 基础连接配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `enabled` | boolean | `false` | 是否启用入站会话拉取 |
| `host` | string | `"https://cloud.langfuse.com"` | Langfuse 服务地址。自托管实例改为你的地址，如 `"http://127.0.0.1:3000"` |
| `public_key` | string | `""` | Langfuse Project Public Key，从 Langfuse 项目设置中获取 |
| `secret_key` | string | `""` | Langfuse Project Secret Key |

### 出站追踪配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `tracing_enabled` | boolean | `false` | 是否启用出站 LLM 调用追踪 |
| `tracing_environment` | string | `"local"` | 环境标签，用于在 Langfuse UI 中区分不同部署环境。建议值：`production`、`staging`、`local`。只允许字母、数字、`-`、`_` |
| `tracing_release` | string | `""` | 版本标签，标记当前部署版本（如 git commit hash、版本号） |
| `tracing_sample_rate` | float | `1.0` | 采样率，范围 0.0-1.0。生产环境建议设为 `0.1`（10% 采样）以降低成本 |
| `tracing_capture_content` | boolean | `true` | 是否捕获完整的 LLM 输入输出内容。设为 `false` 则仅记录元数据和 Token 用量 |
| `tracing_flush_at` | integer | `1` | 累积多少条 trace 后批量刷新到 Langfuse |
| `tracing_flush_interval_seconds` | float | `1.0` | 定时刷新间隔（秒） |
| `timeout_seconds` | integer | `30` | Langfuse API 请求超时时间 |

### 入站拉取默认过滤配置

| 参数 | 类型 | 默认值 | 说明 |
|------|------|--------|------|
| `page_limit` | integer | `50` | 分页拉取每页大小 |
| `max_sessions` | integer | `100` | 单次拉取最大会话数 |
| `default_environment` | list | `[]` | 默认按 environment 过滤，空列表表示不过滤 |
| `default_user_id` | string | `""` | 默认按 userId 过滤 |
| `default_tags` | list | `[]` | 默认按 tags 过滤（所有指定标签都必须匹配） |
| `default_release` | string | `""` | 默认按 release 过滤 |
| `default_version` | string | `""` | 默认按 version 过滤 |
| `default_trace_name` | string | `""` | 默认按 trace name 过滤 |

### 凭证获取

1. 登录 Langfuse（cloud.langfuse.com 或你的自托管实例）
2. 进入 Settings → Projects
3. 创建或选择一个 Project
4. 在 API Keys 部分点击 "Create new API keys"
5. 复制 Public Key 和 Secret Key

注意：Public Key 以 `pk-lf-` 开头，Secret Key 以 `sk-lf-` 开头。

## 配置示例

### 仅启用出站追踪（推荐用于生产监控）

```yaml
langfuse:
  enabled: false
  host: "https://cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_environment: "production"
  tracing_release: "v1.2.3"
  tracing_sample_rate: 0.1
  tracing_capture_content: true
```

### 同时启用入站拉取和出站追踪

```yaml
langfuse:
  enabled: true
  host: "https://cloud.langfuse.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_environment: "production"
  tracing_sample_rate: 0.5
  default_environment:
    - "production"
  default_tags:
    - "agent"
    - "coding"
  max_sessions: 50
```

### 自托管 Langfuse

```yaml
langfuse:
  enabled: true
  host: "http://langfuse.internal.example.com"
  public_key: "pk-lf-xxxxxxxx"
  secret_key: "sk-lf-xxxxxxxx"
  tracing_enabled: true
  tracing_environment: "local"
```

也可以通过环境变量配置（优先级高于 config.yaml）：

```bash
export LANGFUSE_HOST="https://cloud.langfuse.com"
export LANGFUSE_PUBLIC_KEY="pk-lf-xxxxxxxx"
export LANGFUSE_SECRET_KEY="sk-lf-xxxxxxxx"
export LANGFUSE_TRACING_ENABLED="true"
export LANGFUSE_TRACING_ENVIRONMENT="production"
export LANGFUSE_SAMPLE_RATE="0.1"
```

## 出站追踪：追踪什么内容

启用出站追踪后，进化流水线中每个 LLM 调用都会被自动捕获并发送到 Langfuse。追踪采用 fail-open 设计：如果 Langfuse 不可用，不会影响进化流程正常运行，仅在日志中记录警告。

### 被追踪的 LLM 调用

进化流水线中以下阶段的 LLM 调用会被追踪：

| 阶段 | Trace Name | 标签 |
|------|-----------|------|
| 价值分类（Session Filter） | `teamEvolver.evolve.session_filter` | `["teamEvolver", "evolve", "session_filter"]` |
| 会话总结（Summarize） | `teamEvolver.evolve.summarize` | `["teamEvolver", "evolve", "summarize"]` |
| 会话评分（Judge） | `teamEvolver.evolve.judge` | `["teamEvolver", "evolve", "judge"]` |
| 技能改进（Evolve Skill） | `teamEvolver.evolve.evolve_skill` | `["teamEvolver", "evolve", "evolve_skill"]` |
| 新技能创建（Create Skill） | `teamEvolver.evolve.create_skill` | `["teamEvolver", "evolve", "create_skill"]` |
| 冲突合并（Merge） | `teamEvolver.evolve.merge` | `["teamEvolver", "evolve", "merge"]` |
| 测试集生成（Dataset Synthesis） | `teamEvolver.evolve.dataset_synthesis` | `["teamEvolver", "evolve", "dataset_synthesis"]` |
| Checklist 裁判（Replay Checklist） | `teamEvolver.evolve.replay_checklist` | `["teamEvolver", "evolve", "replay_checklist"]` |
| Prompt Studio 测试 | `teamEvolver.evolve.prompt_test.<stage_id>` | `["teamEvolver", "evolve", "prompt-studio", <stage_id>]` |

DreamCycle 启用后，其 Job 的 LLM 调用也会被追踪。

### 每条 Trace 包含的数据

对于每个被追踪的 LLM 调用，会记录以下信息：

- **Input**：完整的 System Prompt 和 User Message（当 `tracing_capture_content: true` 时）
- **Output**：模型返回的完整文本
- **Model**：使用的模型名称
- **Model Parameters**：temperature、max_tokens 等参数
- **Usage Details**：prompt tokens、completion tokens、total tokens
- **Metadata**：组件名、操作名、阶段 ID、源会话 ID 等结构化元数据
- **Session ID**：关联的 teamEvolver 会话 ID（用于在 Langfuse 中按会话分组）
- **Tags**：自动添加 `teamEvolver` 标签和阶段特定标签
- **Environment**：由 `tracing_environment` 配置
- **Release**：由 `tracing_release` 配置

### Trace 层级结构

追踪使用嵌套 Span 结构：

- 每个进化轮次对应一个 Trace
- 每个 LLM 阶段对应 Trace 下的一个 Span
- 同一阶段内的子调用对应嵌套 Span

这样在 Langfuse UI 中可以清晰看到一次完整进化循环中各阶段的调用关系和耗时。

## 入站会话拉取：如何工作

入站会话拉取模式从 Langfuse 拉取外部 Agent 的会话数据，经过格式转换后送入 teamEvolver 的会话队列，触发后续的价值分类和技能进化。

### 支持的过滤维度

拉取时支持丰富的过滤条件，可在配置文件中设置默认值，每次拉取时也可通过 CLI 参数临时覆盖：

- **时间范围**：`from_timestamp` / `to_timestamp`（ISO 8601 格式）
- **环境**：`environment`（可指定多个，匹配任意一个）
- **用户 ID**：`user_id`
- **标签**：`tags`（必须全部匹配）
- **版本**：`release` / `version`
- **Trace 名称**：`trace_name`
- **指定会话**：`session_id`（拉取单个会话）
- **元数据**：`metadata`（按 metadata 键值对过滤）

### 拉取流程

1. 根据过滤条件查询匹配的 Trace 列表（通过 `/api/public/traces` 端点）
2. 从 Trace 结果中解析出唯一的 Session ID 集合
3. 逐个拉取 Session 详情（通过 `/api/public/sessions/{id}` 端点），包含所有 Trace 和 Observation
4. 调用 `langfuse_convert.py` 将 Langfuse 格式转换为 teamEvolver 会话格式
5. 送入会话队列，经过 Session Filter 价值分类
6. 高价值会话进入进化流水线

### 认证方式

Langfuse API 使用 HTTP Basic Auth：
- 用户名：Public Key
- 密码：Secret Key

`langfuse_client.py` 使用 `httpx` 直接调用公共 REST API，不依赖 Langfuse Python SDK 进行数据拉取。

## CLI 命令

teamEvolver 提供了 `teamEvolver langfuse` 命令组管理 Langfuse 集成：

### 查看连接状态

```bash
teamEvolver langfuse status
```

输出包括：

- host 地址
- session_pull_enabled / tracing_enabled 状态
- tracing_environment
- public_key/secret_key 是否存在
- max_sessions 配置
- 默认过滤条件
- Langfuse SDK 是否可用
- 是否能成功连接 Langfuse 服务
- 总会话数（若可访问）

### 预览匹配的会话

```bash
# 列出使用默认过滤条件匹配的会话
teamEvolver langfuse list

# 列出指定环境的会话
teamEvolver langfuse list --environment production --environment staging

# 列出指定用户的会话
teamEvolver langfuse list --user-id user-123

# 列出指定标签的会话
teamEvolver langfuse list --tag agent --tag coding

# 按时间范围过滤
teamEvolver langfuse list --from 2026-01-01T00:00:00Z --to 2026-01-31T23:59:59Z

# 拉取单个会话
teamEvolver langfuse list --session-id abc-123-def

# 限制返回数量
teamEvolver langfuse list --max-sessions 20
```

`list` 命令只查询并展示，不会执行 ingestion。

### 拉取会话进入进化流水线

```bash
# 使用默认配置拉取
teamEvolver langfuse pull

# 带过滤条件拉取
teamEvolver langfuse pull --environment production --max-sessions 50

# 拉取并指定用户别名
teamEvolver langfuse pull --user-alias "langfuse-import"

# 强制重新处理已处理过的会话
teamEvolver langfuse pull --force

# 拉取但不触发进化（仅入队）
teamEvolver langfuse pull --defer-trigger

# 本地处理（不依赖正在运行的 teamEvolver 服务）
teamEvolver langfuse pull --in-process
```

拉取完成后会显示统计：queued（入队）、skipped（低价值跳过）、duplicate（重复跳过）、empty（空会话跳过）、error（处理错误）。

## 在 Langfuse UI 中查看 Traces

### 查看 teamEvolver 出站 Traces

1. 登录 Langfuse
2. 进入 Tracing 页面
3. 在左侧过滤栏中：
   - 选择 Environment（如 `production`）
   - 添加 Tag 过滤：`teamEvolver`
4. 可以看到所有 teamEvolver 发出的 Trace

### 按阶段筛选

要查看特定阶段的调用，添加对应的 Tag 过滤，如 `summarize`、`judge`、`evolve_skill` 等。

### 查看 Token 用量和成本

在 Langfuse 的 Metrics 页面可以按 Environment、Tags、Model 维度查看：
- 总 Token 用量趋势
- 各阶段耗时分布
- 成本估算（需在 Langfuse 中配置模型价格）

### 会话回放

点击单个 Trace 可以看到完整的：
- 输入输出内容
- 嵌套调用关系
- 每个 Span 的耗时
- Token 用量明细
- 元数据

## Langfuse 与 teamEvolver 控制台的区别

| 功能 | teamEvolver 控制台 | Langfuse UI |
|------|-------------------|-------------|
| 进化状态监控 | 支持 | 不支持 |
| 技能管理 | 支持 | 不支持 |
| 候选审核 | 支持 | 不支持 |
| Prompt 编辑测试 | 支持 | 不支持 |
| LLM 调用全链路追踪 | 基础日志 | 完整支持 |
| Token 用量统计 | 基础 | 详细，可按多维度聚合 |
| 成本分析 | 不支持 | 支持（需配置价格） |
| Trace 历史检索 | 有限 | 强大的过滤和搜索 |
| 外部 Agent 会话浏览 | 不支持 | 原生支持 |
| 会话评分和标注 | 不支持 | 原生支持 |

建议同时使用两者：teamEvolver 控制台用于操作和管理，Langfuse 用于深度调试和 LLM 调用层面的问题排查。

## 故障排查

### Trace 没有出现在 Langfuse 中

1. 检查 `tracing_enabled` 是否为 `true`
2. 检查 `public_key` 和 `secret_key` 是否正确
3. 检查 `host` 是否能从服务器访问（自托管实例注意网络连通性）
4. 查看 teamEvolver 日志中是否有 `[Langfuse] tracing unavailable` 警告
5. 运行 `teamEvolver langfuse status` 检查 `reachable: True`

### 入站拉取没有找到会话

1. 确认会话是在与 `default_environment` 匹配的环境中记录的
2. 检查 Trace 上的 Tags 是否包含你过滤的标签
3. 先运行 `teamEvolver langfuse list` 确认匹配数量
4. 确认会话是在 `from_timestamp` 之后创建的
5. Langfuse 的 Sessions 列表端点原生只支持 environment + 时间过滤，其他维度通过 /traces 端点间接匹配，确保你的 Trace 打了正确的 userId/tags/release 属性

### 采样率设置建议

- **开发环境**：`tracing_sample_rate: 1.0`（全量采样，便于调试）
- **预发布环境**：`tracing_sample_rate: 1.0`（全量采样，验证集成）
- **生产环境**：`tracing_sample_rate: 0.05-0.2`（5%-20% 采样，平衡成本和可观测性）
- **排查问题时**：临时设为 `1.0`，问题解决后调回

### 敏感数据

如果进化流程中处理敏感数据：

1. 设置 `tracing_capture_content: false`，仅记录元数据、Token 用量和耗时
2. 或者在自托管 Langfuse 实例中运行，数据不离开你的网络
3. 注意：入站拉取会从 Langfuse 拉取完整会话内容，确保 Langfuse 本身的访问控制已配置好
