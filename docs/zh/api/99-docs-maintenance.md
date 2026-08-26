# 文档维护指南

本文档描述 teamEvolver 中文档的编写规范、维护流程和质量检查机制。文档以 Markdown 源文件维护，登录控制台后通过内置文档阅读器（支持目录树浏览、全文搜索、中英双语切换、Markdown/GFM 渲染）查阅。

## 何时更新文档

以下情况必须同步更新文档：

| 场景 | 需要更新的文档 |
|------|--------------|
| API 接口变更（新增、修改、删除端点，参数变化） | `docs/zh/api/` 对应文件，以及 `docs/zh/agent-integrations/02-protocol-v1.md` |
| 新增功能或能力（新的 capability、新的接入方式） | `docs/zh/agent-integrations/` 相关文件，必要时新增 API 文档 |
| 配置项变更（新增/修改/废弃环境变量、配置参数） | 相关指南文档和 API 文档中的参数说明 |
| 概念变更（术语、架构、流程调整） | `docs/zh/concepts/` 和相关文档中的术语使用 |
| 新增 Agent 集成 | `docs/zh/agent-integrations/` 新增对应接入指南 |
| 代码路径重构导致引用失效 | 所有引用了旧路径的文档 |
| JSON Schema 变更 | `docs/schemas/` 和引用 schema 的文档 |

## 文件结构约定

### 目录结构

```
docs/
├── zh/                   # 中文文档
│   ├── getting-started/  # 开始使用（01-03）
│   ├── concepts/         # 核心概念（01-09）
│   ├── guides/           # 使用指南（01-08）
│   ├── agent-integrations/  # Agent 接入文档（01-05）
│   ├── api/              # API 参考文档（01-11, 99）
│   ├── faq/              # 常见问题
│   └── about/            # 关于
├── en/                   # 英文文档（镜像 zh/ 结构）
├── design/               # 设计文档（不分语言）
├── schemas/              # JSON Schema 定义
├── assets/               # 图片资源
└── scripts/
    └── check-docs-refs.mjs  # 引用校验脚本
```

### 文件命名约定

- 使用**数字前缀**控制排序：`01-overview.md`、`02-protocol-v1.md`、`99-docs-maintenance.md`
- 数字前缀与标题之间用连字符 `-` 连接
- 使用英文小写文件名，单词间用连字符
- 概览文件统一命名为 `01-overview.md`
- 文档维护指南统一命名为 `99-docs-maintenance.md`

### 中英文并行

- `docs/zh/` 和 `docs/en/` 保持相同的目录结构和文件命名
- 新增中文文档时，如条件允许应同步创建英文版本
- 控制台文档阅读器会自动扫描 `docs/zh/`、`docs/en/` 和 `docs/design/` 目录，按数字前缀排序，从一级标题提取显示名称，新增文件无需手动注册

### Section 层级

- 文档标题使用 `#`（一级标题，文件名去除数字前缀后的标题）
- 主要章节使用 `##`（对应文档结构中的各大节）
- 子章节使用 `###`
- 更深层级使用 `####`，尽量避免超过四级

## 代码引用格式

引用代码文件和符号时遵循以下格式：

```
teamEvolver/<模块>/<文件>.py:<符号名>
```

示例：

- `teamEvolver/integrations/agent_registry.py:register_agent` -- 模块级函数
- `teamEvolver/proxy/agent_context.py:119` -- 具体行号（必要时使用）
- `teamEvolver/integrations/replay_adapters.py:82` -- HttpReplayAdapter 类
- `teamEvolver/integrations/agent_protocol.py` -- 整个文件（无具体符号时）

注意事项：

- 路径始终以 `teamEvolver/` 开头
- 使用正斜杠 `/` 作为路径分隔符
- 符号名使用类名或函数名，不包含括号
- 引用行号时仅在需要精确指向某段代码时使用，避免频繁使用行号（代码变动会导致失效）
- 引用文件路径使用 `file:///` 绝对路径链接，校验脚本会验证文件/目录是否存在

## API 文档结构

每个 API 文档遵循以下结构（参考 OpenViking 风格）：

### 1. API 实现介绍

说明接口的用途、核心设计原则、相关代码入口。

包含：
- 接口功能说明
- 认证要求
- 核心设计原则（如不透明引用、幂等性等）
- 相关代码文件路径

### 2. 接口和参数说明

使用表格列出所有接口、方法、参数。

- 按端点分节，包含 HTTP 方法和路径
- 参数表包含：字段名、类型、是否必填、说明
- 枚举值列出所有可选值
- 嵌套对象使用子表或缩进说明

### 3. 使用示例

提供可运行的 `bash` 代码块（curl 示例）：

```bash
curl -X POST "http://localhost:52010/internal/agents/register" \
  -H "Authorization: Bearer <key>" \
  -H "Content-Type: application/json" \
  -d '{...}'
```

JSON 响应示例使用 `json` 代码块。

### 4. 响应契约与错误处理

- 列出所有响应字段及其类型和说明
- 提供成功响应示例
- 使用表格列出所有错误码：HTTP 状态码、错误信息、原因
- 说明幂等性、缓存、限流等行为约定

## 文档引用检查

项目提供文档引用检查脚本，用于验证代码引用路径、文件链接和图片的有效性。

```bash
node docs/scripts/check-docs-refs.mjs
```

该脚本会检查：
1. 所有 `file:///` 引用的文件/目录是否存在
2. 所有 `teamEvolver/...` 代码引用路径下的文件和符号（class/def/async def）是否存在
3. 文档间的 Markdown 相对链接是否指向存在的目标文件
4. 图片引用是否指向存在的图片文件
5. zh/en 并行文件是否缺失（warning）

提交文档变更前应运行此检查，确保没有失效引用。

## 新增文档页面

### 新增文件到已有章节

将 Markdown 文件放入对应目录（如 `docs/zh/guides/`），文件名以合适的数字前缀开头。控制台阅读器会自动扫描并加载该文件，按文件名数字排序，并从文件的一级标题（`#`）提取显示名称。

### 新增章节分组

后端 `DocsMixin._build_docs_tree` 中维护了章节顺序列表 `section_order`。如果新增顶级章节目录（如 `docs/zh/plugins/`），需要在该列表中添加新章节名称，并在 `_section_label()` 中添加中英文标签。

## 截图更新流程

文档中使用的截图需要注意：

1. **先匿名化**：截图前确保界面中没有敏感信息（内部 URL、用户名、API Key、公司名称、客户数据等）。
2. **全屏滚动捕获**：使用浏览器开发者工具或截图工具捕获完整的滚动区域，避免截断内容。
3. **保存到 assets**：截图保存到 `docs/assets/` 目录，使用描述性文件名。文档中通过 `/docs-assets/<filename>` 引用。
4. **中文界面**：中文文档使用中文界面截图。
5. **保持最新**：UI 有重大变更时更新对应截图。

## 写作风格指南

### 术语一致性

核心术语保持一致，避免使用同义词：

| 术语 | 正确用法 | 避免 |
|------|---------|------|
| Agent | 指代接入的 AI Agent 运行时 | "代理"、"智能体" |
| Skill | 指代团队/个人技能包 | "技能"（英文语境用 Skill）、"prompt" |
| Session | 指代一次完整对话会话 | "会话"（作为概念可混用）、"对话" |
| Context | 指代上下文工作区 | "上下文"（中文叙述中可用） |
| Replay | 指代 True Replay 验证 | "回放"、"重放" |
| Capability | 指代 Agent 声明的能力 | "功能"、"特性" |
| integration_id | 集成 ID | "agent_id"（注意区分） |
| external_subject | 外部主体标识 | "用户 ID"、"username" |

### 语言规范

- 使用简体中文
- 代码标识符（变量名、函数名、类名、端点路径、JSON 字段名、配置键名）保持英文，不翻译
- 技术术语首次出现时可附英文原文，如"上下文工作区（Context Workspace）"
- 使用主动语态，避免口语化表达
- 句子简洁明了，避免冗长复合句
- 列表和表格用于结构化信息，避免大段文字堆砌

### Markdown 规范

- 使用 `##` 作为主要分节标记
- 不使用 emoji（项目规范）
- 代码块指定语言：`` `bash ``、`` `json ``、`` `python ``、`` `yaml ``
- 表格使用标准 Markdown 表格语法
- 链接使用相对路径
- 不使用 HTML 标签（除非必要）

## 验证清单

提交文档变更前，请逐项确认：

- [ ] 所有代码引用路径真实存在于 `/home/zhangpengkun/teamEvolver/` 下
- [ ] 运行 `node docs/scripts/check-docs-refs.mjs` 无报错
- [ ] 新文档文件命名符合数字前缀约定
- [ ] 一级标题（`#`）准确反映文档主题
- [ ] API 文档遵循四段式结构（实现介绍、参数说明、使用示例、响应与错误）
- [ ] 所有代码块指定了正确的语言标识
- [ ] 表格对齐可读，参数说明完整
- [ ] curl 示例中的参数与参数表一致
- [ ] 未使用 emoji
- [ ] 截图已匿名化处理
- [ ] 中英文结构保持一致（如新增文件）
- [ ] 术语使用保持一致
- [ ] 所有必填字段在参数表中标注"是"
- [ ] 错误码覆盖了所有代码中定义的错误情况
