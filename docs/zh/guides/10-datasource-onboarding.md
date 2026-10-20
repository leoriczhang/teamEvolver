# 数据源接入指南

数据源 Adapter 把上游系统（Doris、Langfuse 等）中的 Agent 会话转换为标准 Session，供 Worker 拉取、分析与进化。Adapter 是 Python 文件，源码按不可变版本保存在 PostgreSQL，租户绑定到一个固定 revision。接口与字段约束见 [Session 接入 API](../api/03-session-ingest.md#数据源-adapter)。

## 组成

| 位置 | 内容 |
|------|------|
| `session_ingestion/adapters/*.py` | 各租户的 Adapter 模板，每个文件定义 `SOURCE` 与 `build_adapter()` |
| `session_ingestion/adapters/sources/` | 共享传输实现：`doris.py`（`DorisSourceAdapter`）、`langfuse.py`（`LangfuseSource`） |
| `session_ingestion/adapters/_shared/` | 字段映射、Langfuse 客户端与转换、构建函数 |
| PostgreSQL `adapter_versions` | 已保存的 Adapter 源码，每次保存生成新 revision |
| PostgreSQL `adapter_bindings` | 租户绑定，`binding_key='datasource'` 指向 `(adapter_id, revision)` |

仓库中的模板不会自动生效，必须先保存到 PostgreSQL 再绑定。运行中的拉取任务固定受理时的 revision，之后保存新版本不影响在途任务。

## Adapter 文件约定

参考 `session_ingestion/adapters/example-doris.py`，使用前配置上游并将 `enabled` 改为 true：

```python
SOURCE = {
    "label": "Example Doris", "provider": "doris",
    "host": "doris.example.com", "project_id": "example-project",
    "enabled": False, "max_sessions": 200,
    "supported_filters": ["from_timestamp", "to_timestamp", "session_id", "user_id"],
    "required_filters": ["from_timestamp", "to_timestamp"],
}


def build_adapter():
    from session_ingestion.adapters._shared.builders import build_doris_adapter
    return build_doris_adapter(SOURCE, "agent-call")
```

校验规则（`session_ingestion/adapters/_runtime.py:validate_content`）：

- 文件名以 `.py` 结尾，不以 `_` 开头，不含路径；源码不超过 256 KiB
- 顶层必须定义 `build_adapter()` 和字面量字典 `SOURCE`
- `required_filters` 必须是 `supported_filters` 的子集；`max_sessions` 取 1–1000
- `exclude_session_id_patterns` 是大小写敏感的 glob 列表，在拉取详情前过滤噪声 Session；显式指定 `session_id` 时不生效

`build_adapter()` 返回的对象需提供 `health()`、`iter_session_id_batches(filters, batch_size)`、`fetch_session(session_id)`、`convert_session(session, traces)` 与 `close()`。Worker 每批最多处理 1000 个 Session ID，以 8 路并发拉取详情与转换；单条失败记为 error，不中断整批。

## 凭证

上游凭证从 Worker 进程的环境变量读取，不写入 Adapter 源码或 `SOURCE`：

| 上游 | 环境变量 |
|------|----------|
| Doris | `DORIS_HOSTS`、`DORIS_PORT`、`DORIS_USER`、`DORIS_PASSWORD`、`DORIS_DB`；表名为 `DORIS_TRACES_TABLE`、`DORIS_OBSERVATIONS_TABLE`、`DORIS_MAPPING_TABLE`；可选 `DORIS_PROXY_URL`、`DORIS_FORCE_PROXY`、`DORIS_CA_BUNDLE` |
| Langfuse | `LANGFUSE_PULL_HOST`、`LANGFUSE_PULL_PUBLIC_KEY`、`LANGFUSE_PULL_SECRET_KEY` |

修改环境变量后需要重启 Worker。出站链路观测使用独立的 `langfuse_tracing_*` 配置，与数据源凭证无关，见 [链路观测指南](./04-observability.md)。
直连 Doris 需安装可选驱动 `pymysql`；HTTP 查询代理只在显式配置 `DORIS_PROXY_URL` 后可用，默认验证 TLS，可用 `DORIS_CA_BUNDLE` 指定证书。

## 接入步骤

### 1. 保存 Adapter 版本

保存与读取源码要求 Root Key。以下命令把仓库模板保存为租户 `demo` 的第一个版本：

```bash
export TEAMEVOLVER_ROOT_API_KEY=...
jq -Rs '{file: "example-doris.py", code: ., expected_revision: 0}' \
  session_ingestion/adapters/example-doris.py |
curl --fail -X PUT http://127.0.0.1:52010/api/datasource/code \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" -H 'X-Tenant-Id: demo' \
  -H 'Content-Type: application/json' -d @-
```

`expected_revision` 必须等于当前最新 revision（首次为 0），否则返回 409。内容与最新版本相同时返回原 revision，不产生新版本。

保存前可用 `POST /api/datasource/code/test` 对草稿执行 `validate`、`health`、`preview` 或 `session` 测试；`validate` 同步返回，其余模式以后台任务执行。

### 2. 绑定

```bash
teamEvolver datasource --tenant demo bind example-doris.py
```

绑定默认取最新 revision；通过 `PUT /api/datasource` 传入 `{"file", "revision"}` 可绑定指定版本，`file` 为空串表示解绑。租户管理员也可以在控制台「数据源接入」中选择文件并绑定。

### 3. 验证

```bash
teamEvolver datasource --tenant demo test
teamEvolver datasource --tenant demo list --from-timestamp 2026-10-01T00:00:00+08:00 \
  --to-timestamp 2026-10-01T02:00:00+08:00 --max-sessions 20
```

两条命令都返回 `job_id`，结果通过 `GET /api/datasource/jobs/{job_id}/result` 读取。先用窄时间窗口预览，核对 Session 分组、用户、工具调用与使用的 Skill。

### 4. 拉取

```bash
teamEvolver datasource --tenant demo pull \
  --from-timestamp 2026-10-01T00:00:00+08:00 --to-timestamp 2026-10-02T00:00:00+08:00
```

拉取遍历整个过滤窗口，按条受理 Session 并入队 `session.normalize`。之后的分析、归档与进化见 [Session](../concepts/05-sessions.md)。

### 5. 定时拉取

```json
{"enabled": true, "time": "02:00", "timezone": "Asia/Shanghai", "window": "previous_day"}
```

通过 `PUT /api/datasource/schedule` 或控制台保存。Worker 每轮租户刷新检查一次：本地时间达到 `time` 后按前一自然日窗口受理一次拉取，同一窗口只受理一次。`POST /api/datasource/schedule/run` 立即按该窗口拉取。

## 控制台

「数据源接入」页面（租户管理员）提供绑定、连接测试、预览、拉取与定时配置。源码编辑与上传需要 Root Key 身份；Cookie 登录时 `GET /api/datasource` 返回 `source_editable=false`，页面只显示已保存版本。

## 常见错误

| 状态 | 原因 |
|------|------|
| 400 | Adapter 校验失败、过滤字段不受支持、缺少必填过滤、`max_sessions` 越界 |
| 403 | 读取或保存源码时未使用 Root Key |
| 404 | 绑定的 revision 不存在 |
| 409 | 未绑定 Adapter 就测试或拉取；保存时 `expected_revision` 已过期 |

## 相关文档

- [Session 接入 API](../api/03-session-ingest.md)
- [Session](../concepts/05-sessions.md)
- [运维 CLI](./05-daemon-cli.md)
