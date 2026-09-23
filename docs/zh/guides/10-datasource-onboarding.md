# 租户数据源接入

## 唯一入口

一个租户绑定 `session_ingestion/adapters/` 顶层的一个 `.py` 文件，一个文件只能绑定一个租户。
文件拥有上游连接、项目选择、筛选、转换逻辑；平台只接收标准 Session。
共享 Doris/Langfuse 传输在 `session_ingestion/adapters/sources/`，解析和旧版迁移工具在
`session_ingestion/adapters/_shared/`。平台出站链路观测独立保留，不是上游数据源。

## 文件约定

参考 `session_ingestion/adapters/product-agent.py`。定义字面量字典 `SOURCE`，包含 `label`、
`provider`、`enabled`、`supported_filters`、`required_filters`、`max_sessions`。
可选的 `project_id` 只用于显示上游项目，不决定租户身份。

`build_adapter()` 无参数，返回包含以下方法的对象：

| 方法 | 契约 |
|------|------|
| `health()` | 实际检查连接，返回 `{"ok": bool}` |
| `list_session_ids(filters, *, max_sessions)` | 返回有界 Session ID 列表 |
| `fetch_session(session_id)` | 返回原始 Session 和 Trace 列表 |
| `convert_session(session, traces)` | 返回含 `session_id`、`turns` 的标准 Session |
| `close()` | 释放资源，异常时也会调用 |
| `preview_sessions(filters, *, max_sessions)` | 可选，返回含标题、用户等的预览列表 |

单次拉取最多 1000 个 Session，最多 8 路并发。连接必须线程安全。
缺失文件、非法配置、转换失败均不得静默切换数据源。顶层文件按内容哈希热加载；
共享模块及环境变量变更需要重启。管理员可上传、新建或编辑 `.py` 运行副本，
并直接校验、测试连接、预览 Session 或转换单条 Session。草稿测试不写磁盘。

当前没有适配器持久化托管：点击“保存运行副本”只原子写入当前服务实例，
重新部署、替换容器或重新安装后可能丢失。验证通过后必须联系项目 Owner，
将变更合入 `session_ingestion/adapters/<tenant>.py` 并重新发布。保存使用 revision 校验，
文件已被其他管理员修改时返回 409，避免覆盖他人变更。

## 绑定与验证

在控制台切换目标租户，打开「数据源接入」，选择文件并保存绑定。
租户绑定写入 `datasource_adapter`，default 绑定写入当前 YAML 的 `datasource.adapter`。
绑定不继承、不通过显示名猜测，旧租户须显式绑定后才能拉取。

1. 部署完整 adapters 目录和重建后的前端。
2. 通过环境变量注入 Doris 连接凭据，不在源码或 SOURCE 中存储密码。
3. 核对四个租户文件中的上游项目。陆网文件目前通过项目名查询映射表，须验证映射。
4. 保存绑定，测试连接，填写窄时间窗口预览，再拉取少量 Session 对比工具与 Skill 信息。

## 接口

`GET /api/datasource` 返回当前描述与可用文件；`PUT` 接收 `{"file":"product-agent.py"}`。
空字符串表示解绑。`GET /api/datasource/code` 读取源码，`PUT` 保存运行副本；
`POST /api/datasource/code/test` 对未保存源码执行 `validate`、`health`、`preview`
或 `session` 测试；`POST /test` 测试已保存连接；
`POST /sessions` 预览；`POST /pull` 拉取（后三者均在 `/api/datasource` 下）。
使用控制台身份或 Root Key 和 `X-Tenant-Id`；租户 Token 只允许调用自己的 pull，且不能访问管理员接口（`/api/*`）与 Agent 注册接口（`/internal/agents/register`）。
未支持的筛选字段和非法参数返回 400；文件已占用返回 409；并发超限返回 429。
旧 Langfuse/Converter/Mapper 配置接口返回 410，旧 pull/sessions URL 转入新流程。

旧持久化字段及 Python 导入别名仅保留供离线迁移，不参与在线数据源选择。
