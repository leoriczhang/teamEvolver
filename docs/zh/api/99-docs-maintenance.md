# 文档维护指南

本页说明文档的目录约定、写作规则和检查方式。文档以 Markdown 维护在仓库 `docs/` 下；控制台不内置文档阅读器。

## 何时更新文档

| 变更 | 需要更新 |
| --- | --- |
| 新增、修改或删除路由 | 重新生成 [端点索引](./00-endpoints.md)，并更新对应 API 详情页 |
| 修改 Agent 数据面（Session、Context、Skill 拉取） | `docs/{zh,en}/api/` 对应页与 [协议 v2](../agent-integrations/06-protocol-v2.md)、`docs/schemas/` |
| 新增或修改 CLI 命令、启动配置字段、环境变量 | [配置参考](../guides/01-configuration.md)、[CLI 指南](../guides/05-daemon-cli.md) |
| 修改 PostgreSQL 表、OpenViking 路径或目录布局 | [存储空间与目录布局](../concepts/09-storage-layout.md) |
| 修改术语 | 仓库根目录 `CONTEXT.md` 与使用该术语的页面 |
| 移动或重命名代码文件 | 所有引用旧路径的页面 |

## 目录结构

```text
docs/
├── README.md            # 文档索引
├── zh/                  # 中文文档
│   ├── getting-started/
│   ├── concepts/
│   ├── guides/
│   ├── agent-integrations/
│   ├── api/             # 00-endpoints.md 由脚本生成
│   ├── faq/
│   └── about/
├── en/                  # 英文文档，与 zh/ 同名同结构
├── design/              # 设计说明
├── agents/              # Coding Agent 协作约定
├── schemas/             # Agent 协议 JSON Schema
├── assets/              # 图片，服务以 /docs-assets/ 提供
└── scripts/check-docs-refs.mjs
```

当前维护范围见 [文档索引](../../README.md)。

## 文件命名

- 使用数字前缀排序，如 `01-overview.md`，前缀与名称用 `-` 连接。
- 文件名用英文小写，单词间用 `-`。
- `docs/zh/` 与 `docs/en/` 中每个页面必须同名存在，`#`、`##`、`###` 标题数量一致。

## 写作规则

- 直接陈述系统当前的行为，不描述历史版本或已删除功能。
- 端点、字段、命令、环境变量、代码路径与实现保持一致，标识符保持英文原文。
- 术语使用 `CONTEXT.md` 中的定义：Agent、Session、Skill、Skill Candidate、True Replay、Replay Adapter、团队 Memory、租户 Key、服务 Root Key 等。
- 代码块标注语言（`bash`、`json`、`python`、`yaml`、`text`）。
- 不使用 emoji，不使用 HTML 标签。
- 截图先匿名化，保存到 `docs/assets/`，通过 `/docs-assets/<file>` 引用。

## 代码引用

用反引号写仓库相对路径，可附符号名：

- `teamEvolver/app.py:create_app`
- `team_skills/api/candidates.py:create_router`
- `session_ingestion/push/protocol.py`

可引用 `teamEvolver`、`team_memory`、`team_skills`、`team_replay`、`team_miner`、`session_ingestion`、`web-ui`、`scripts`、`cicd_scripts`、`tests` 下的 `.py`、`.ts`、`.tsx`、`.sh`、`.sql`、`.mjs` 文件。也可以用相对链接指向仓库文件或目录，例如 `[app.py](../../../teamEvolver/app.py)`。

## 端点索引

[端点索引](./00-endpoints.md) 由 `scripts/docs_endpoint_index.py` 从 `teamEvolver/app.py:create_app` 实际组装的路由生成，脚本中的 `MODULES` 把每个源文件映射到详情页。修改路由后重新生成：

```bash
.venv/bin/python scripts/docs_endpoint_index.py zh > docs/zh/api/00-endpoints.md
.venv/bin/python scripts/docs_endpoint_index.py en > docs/en/api/00-endpoints.md
```

新增 API 模块时先在 `MODULES` 中登记，否则脚本报错。

## 检查

```bash
node docs/scripts/check-docs-refs.mjs --strict
.venv/bin/python -m pytest tests/contracts/test_docs.py
```

`check-docs-refs.mjs` 扫描 `docs/zh`、`docs/en`、`docs/design`、`docs/README.md` 与 `docs/coding-agent*.md`，检查：

1. 反引号代码引用的文件存在，符号在文件中有 `def`、`class` 或赋值定义（符号缺失为警告）。
2. `file:///` 引用、Markdown 页面链接、指向仓库文件或目录的相对链接、图片均存在。
3. zh 与 en 页面一一对应（缺失为警告）。
4. 文档引用的 `docs/schemas/*.schema.json` 存在；`docs/schemas/` 中每个 Schema 都被至少一个页面引用（未引用为警告）。

`--strict` 把警告也视为失败。

`tests/contracts/test_docs.py` 检查：

1. 端点索引与 `create_app` 的路由一致。
2. 文档、`README.md`、`README.en.md`、`AGENTS.md` 中用反引号写出的 TeamEvolver 路由都存在（`/api/v1/`、`/api/public/` 等上游路径除外）。
3. 文档中出现的 `teamEvolver <command>` 都是已注册的 CLI 命令。
4. 文档中出现的 `TEAMEVOLVER_*` 环境变量都被代码读取。
5. zh 与 en 页面标题数量一致。

## 提交前清单

- [ ] 两条检查命令通过
- [ ] 新页面已有中英两个版本，标题结构一致
- [ ] 改了路由时已重新生成端点索引
- [ ] 示例中的字段与参数表一致
- [ ] 截图已匿名化
