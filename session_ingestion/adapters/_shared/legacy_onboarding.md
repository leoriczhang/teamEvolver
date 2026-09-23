# 自定义数据源接入指南

本指南面向需要将**非标准 Langfuse 数据源**接入 teamEvolver 进化管线的客户。如果你的数据源已经是标准 Langfuse v3 格式，无需阅读本文——直接在控制台配置 Langfuse 连接即可。

## 什么时候需要写数据源适配器

| 场景 | 用什么 |
|------|--------|
| 数据来自标准 Langfuse，trace 格式也标准 | 直接配置 Langfuse 连接，不需要适配器 |
| 数据来自 Langfuse，但 trace 格式不标准 | Langfuse Mapper（控制台内联 Python）|
| 数据来自 Langfuse，但不同 agent 的业务逻辑不同 | Per-agent Hook 文件（`session_ingestion/adapters/<agent_id>.py`）|
| **数据不来自 Langfuse**（自定义 API、Doris、文件、其他可观测平台） | **本文：文件级 Source Adapter** |

## 架构总览

teamEvolver 的数据采集管线分三层，互不耦合：

```
                    ┌─────────────────────────────────────┐
                    │          采集管线（System 层）         │
                    │  并发控制 · 空内容跳过 · session_id   │
                    │  清洗 · ingest · 去重 · 进化触发      │
                    └──────────────┬──────────────────────┘
                                   │ 调用
                    ┌──────────────▼──────────────────────┐
                    │      Source Adapter（数据源层）        │
                    │  list_session_ids()  → 列出会话       │
                    │  fetch_session()     → 拉取详情       │
                    │  convert_session()   → 转标准格式     │
                    │  health()            → 连通性探活     │
                    └──────────────┬──────────────────────┘
                                   │ 文件位置
              ┌────────────────────┼────────────────────┐
              │                    │                    │
     内置 Langfuse         文件级 Source Adapter    Per-agent Hook
     (source_type=          session_ingestion/adapters/sources/        session_ingestion/adapters/<agent_id>.py
      "langfuse")           <type>.py               按业务逻辑微调
```

**你只需要关心中间那一层**：写一个 `session_ingestion/adapters/sources/<type>.py` 文件，实现 3 个方法。管线会自动处理并发、去重、存储、进化触发等一切后续工作。

## 快速开始

### 第一步：获取模板

```bash
# 通过 API 获取（需管理员权限）
curl http://127.0.0.1:52010/api/datasource-config/source-template?source_type=my-source \
  -H "Authorization: Bearer $ROOT_KEY"
```

或直接从 `teamEvolver/integrations/source_adapter.py` 中的 `default_source_adapter_template()` 获取。

### 第二步：实现适配器

将模板保存为 `<adapters_dir>/sources/my-source.py`，实现 3 个必需方法：

```python
# session_ingestion/adapters/sources/my-source.py

def build_adapter(config, options):
    """config — 完整 TeamEvolverConfig；options — datasource.options 字典"""
    return MySourceAdapter(config, options)


class MySourceAdapter:
    source_type = "my-source"

    def __init__(self, config, options):
        self.config = config
        self.options = options or {}
        # 从 options 读取连接参数
        self.host = self.options.get("host", "")
        self.token = self.options.get("token", "")

    def list_session_ids(self, filters, *, max_sessions):
        """列出匹配过滤条件的会话 ID。

        filters 可能包含的 key：
          - from_timestamp / to_timestamp: ISO 8601 时间字符串
          - user_id: 用户标识
          - tags: 标签列表
          - session_id: 指定会话 ID
          - trace_name: trace 名称

        只需要实现你的数据源支持的那些；其余可忽略。
        """
        # 示例：从自定义 API 拉取
        import httpx
        resp = httpx.get(
            f"{self.host}/api/sessions",
            headers={"Authorization": f"Bearer {self.token}"},
            params={
                "from": filters.get("from_timestamp", ""),
                "to": filters.get("to_timestamp", ""),
                "limit": max_sessions,
            },
            timeout=30,
        )
        resp.raise_for_status()
        return [s["id"] for s in resp.json().get("sessions", [])]

    def fetch_session(self, session_id):
        """拉取一个会话的完整数据。

        返回: (session_dict, traces_list)
          - session_dict: 至少包含 {"id": session_id}
          - traces_list: 每个元素代表一个交互轮次，按时间排序
          - trace 的结构由你自己的 convert_session 决定
        """
        import httpx
        resp = httpx.get(
            f"{self.host}/api/sessions/{session_id}",
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        return data.get("session", {"id": session_id}), data.get("turns", [])

    def convert_session(self, session, traces):
        """将原始数据转换为 teamEvolver 标准格式。

        这是唯一需要理解目标格式的方法——见下方「标准格式」章节。
        """
        turns = []
        for i, trace in enumerate(traces or [], 1):
            turns.append({
                "turn_num": i,
                "prompt_text": str(trace.get("input") or ""),
                "response_text": str(trace.get("output") or ""),
                "tool_calls": [],
                "tool_results": [],
                "metrics": {"total_tokens": 0},
            })
        return {"session_id": session.get("id") or "", "turns": turns}

    # 可选：连通性探活（控制台「测试连接」按钮调用）
    def health(self):
        import httpx
        try:
            resp = httpx.get(
                f"{self.host}/api/health",
                headers={"Authorization": f"Bearer {self.token}"},
                timeout=5,
            )
            return {"ok": resp.status_code == 200}
        except Exception as e:
            return {"ok": False, "error": str(e)}

    # 可选：资源释放（每次拉取结束后调用）
    def close(self):
        pass
```

### 第三步：配置

在 `~/.teamEvolver/config.yaml` 中配置：

```yaml
datasource:
  type: my-source          # 与文件名一致（不含 .py）
  adapters_dir: ""         # 留空使用发行包内置 session_ingestion/adapters/
  options:                 # 自由格式的连接参数，透传给 build_adapter()
    host: https://my-api.example.com
    token: sk-xxxxxxxxxxxx
```

或通过控制台 API 保存：

```bash
curl -X POST http://127.0.0.1:52010/api/datasource-config \
  -H "Authorization: Bearer $ROOT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "type": "my-source",
    "options": {"host": "https://my-api.example.com", "token": "sk-xxx"}
  }'
```

### 第四步：验证

```bash
# 拉取 10 个会话测试
curl -X POST http://127.0.0.1:52010/langfuse/pull \
  -H "Authorization: Bearer $ROOT_KEY" \
  -H "Content-Type: application/json" \
  -d '{
    "max_sessions": 10,
    "from_timestamp": "2026-09-01T00:00:00Z",
    "to_timestamp": "2026-09-02T00:00:00Z"
  }'
```

文件修改后**无需重启**——适配器通过 mtime 热加载，保存即生效。

## 标准格式（Evolution Turn）

`convert_session` 返回的字典必须符合以下结构。**一个 trace 对应一个 turn（交互轮次）**，一个 session 包含多个 turn。

### 会话级

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `session_id` | str | 是 | 会话唯一标识。含特殊字符会被自动清洗为 `[A-Za-z0-9_.-]` |
| `title` | str | 否 | 会话标题，用于控制台展示。留空时取首轮 prompt_text |
| `turns` | list[dict] | 是 | 交互轮次列表，按时间顺序排列 |
| `user_alias` | str | 否 | 用户标识（工号等）。管线会自动填充默认值 |

### 轮次级（Turn）

| 字段 | 类型 | 必需 | 说明 |
|------|------|------|------|
| `turn_num` | int | 否 | 轮次序号（从 1 开始）。留空时由管线自动分配 |
| `trace_id` | str | 否 | 来源 trace 的 ID，便于回溯 |
| `prompt_text` | str | **至少其一** | 本轮用户/输入侧文本 |
| `response_text` | str | **至少其一** | 本轮 Agent/输出侧文本。与 prompt_text 至少一个非空，否则该会话被跳过 |
| `messages` | list[dict] | 否 | 完整消息序列，每条形如 `{"role": "user"/"assistant"/"tool", "content": "..."}` |
| `tool_calls` | list[dict] | 否 | 工具调用，形如 `{"id": "call_1", "type": "function", "function": {"name": "exec", "arguments": "{...}"}}`。arguments 为字符串化 JSON |
| `tool_results` | list[dict] | 否 | 工具返回，形如 `{"tool_call_id": "call_1", "tool_name": "exec", "content": "结果文本", "has_error": false}` |
| `injected_skills` | list[str] | 否 | 本轮注入到上下文的团队 Skill 名称 |
| `used_skills` | list[str] | 否 | 本轮实际使用的 Skill 名称 |
| `read_skills` | list[dict] | 否 | 本轮读取过的 Skill，元素形如 `{"skill_name": "xxx"}` |
| `modified_skills` | list[dict] | 否 | 本轮被创建/修改的 Skill |
| `metrics` | dict | 否 | 效率指标（见下表） |

### 指标（metrics）

| 字段 | 类型 | 说明 |
|------|------|------|
| `tool_call_count` | int | 工具调用次数 |
| `api_call_count` | int | LLM API 调用次数 |
| `input_tokens` | int | 输入 Token 数 |
| `output_tokens` | int | 输出 Token 数 |
| `total_tokens` | int | 总 Token 数 |

### 完整示例

```json
{
  "session_id": "agent:main:user:3989221a",
  "title": "请运行本地脚本并静默完成任务",
  "turns": [
    {
      "turn_num": 1,
      "trace_id": "ddda7e6b-0dc8-4752-819e-2b546196f4b3",
      "prompt_text": "请运行本地脚本并静默完成任务。",
      "response_text": "脚本已执行，跳过发送（今日已发送过）。",
      "messages": [
        {"role": "user", "content": "请运行本地脚本…"},
        {"role": "assistant", "content": "脚本已执行…"}
      ],
      "tool_calls": [
        {
          "id": "call_1",
          "type": "function",
          "function": {
            "name": "exec",
            "arguments": "{\"command\": \"python3 report.py\"}"
          }
        }
      ],
      "tool_results": [
        {
          "tool_call_id": "call_1",
          "tool_name": "exec",
          "content": "SKIP:already sent today",
          "has_error": false
        }
      ],
      "injected_skills": ["daily-report"],
      "used_skills": ["daily-report"],
      "read_skills": [],
      "modified_skills": [],
      "metrics": {
        "tool_call_count": 1,
        "api_call_count": 2,
        "input_tokens": 533,
        "output_tokens": 87,
        "total_tokens": 3180
      }
    }
  ]
}
```

## 常见接入场景

### 从 Langfuse 接入（但格式不标准）

如果你的数据来自 Langfuse 但 trace 格式与标准 `openclaw-turn` 不同，**不需要写 Source Adapter**。使用控制台的 Langfuse Mapper 功能：

1. 控制台 → 数据源接入 → 会话转换模式 → 编辑 Mapper
2. 编写 `map_trace(trace, observations)` 函数，返回部分字段覆盖内置映射
3. 用「离线试跑」功能粘贴真实 trace JSON 验证

Mapper 返回的字段会深合并到内置映射之上，只需覆盖你关心的字段。

### 从 Doris / 自定义 SQL 接入

```python
def build_adapter(config, options):
    return DorisAdapter(config, options)

class DorisAdapter:
    source_type = "doris"

    def __init__(self, config, options):
        self.options = options or {}
        self.jdbc_url = self.options.get("jdbc_url", "")
        self.query_sql = self.options.get("query_sql", "")

    def list_session_ids(self, filters, *, max_sessions):
        # 用 pymysql / jaydebeapi 等执行 SQL
        # 返回 session_id 列表
        ...

    def fetch_session(self, session_id):
        # 查出该 session 的所有轮次
        # 返回 (session_dict, traces_list)
        ...

    def convert_session(self, session, traces):
        # 将 SQL 行转为标准 turn 格式
        ...

    def close(self):
        # 关闭数据库连接池
        ...
```

**依赖**：如果适配器使用了 `httpx`（已内置）以外的库（如 `pymysql`），需要将该依赖加入 `docker/build-requirements.in` 并重新生成锁文件。

### 从文件目录接入

```python
import json
from pathlib import Path

def build_adapter(config, options):
    return FileSourceAdapter(config, options)

class FileSourceAdapter:
    source_type = "file"

    def __init__(self, config, options):
        self.data_dir = Path(options.get("data_dir", ""))

    def list_session_ids(self, filters, *, max_sessions):
        files = sorted(self.data_dir.glob("*.json"))
        return [f.stem for f in files[:max_sessions]]

    def fetch_session(self, session_id):
        path = self.data_dir / f"{session_id}.json"
        data = json.loads(path.read_text(encoding="utf-8"))
        return data.get("session", {"id": session_id}), data.get("traces", [])

    def convert_session(self, session, traces):
        turns = []
        for i, t in enumerate(traces, 1):
            turns.append({
                "turn_num": i,
                "prompt_text": str(t.get("input") or ""),
                "response_text": str(t.get("output") or ""),
                "metrics": {"total_tokens": int(t.get("tokens", 0))},
            })
        return {"session_id": session.get("id") or "", "turns": turns}
```

### 从其他可观测平台接入（如 LangSmith、OpenTelemetry）

```python
def build_adapter(config, options):
    return LangSmithAdapter(config, options)

class LangSmithAdapter:
    source_type = "langsmith"

    def __init__(self, config, options):
        self.options = options or {}
        self.api_key = self.options.get("api_key", "")
        self.project = self.options.get("project", "")

    def list_session_ids(self, filters, *, max_sessions):
        import httpx
        resp = httpx.get(
            "https://api.smith.langchain.com/runs",
            headers={"x-api-key": self.api_key},
            params={
                "project": self.project,
                "start_time": filters.get("from_timestamp"),
                "end_time": filters.get("to_timestamp"),
                "limit": max_sessions,
            },
        )
        # 将 LangSmith 的 run 分组为 session
        sessions = {}
        for run in resp.json():
            sid = run.get("session_id") or run.get("id")
            if sid and sid not in sessions:
                sessions[sid] = run
        return list(sessions.keys())[:max_sessions]

    def fetch_session(self, session_id):
        # 拉取该 session 的所有 run
        ...

    def convert_session(self, session, traces):
        # 将 LangSmith run 转为标准 turn
        ...
```

## 从客户现有 Langfuse 配置迁移到自定义数据源

如果客户当前使用 Langfuse，但希望切换到自定义数据源（例如直连后端 API 避免 Langfuse 中转），以下是迁移步骤。

### 当前 Langfuse 配置（参考）

客户当前的 `config.yaml` 中 Langfuse 配置通常包含：

```yaml
langfuse:
  enabled: true
  host: https://ai-langfuse.sf-express.com
  public_key: pk-lf-xxx
  secret_key: sk-lf-xxx
  default_trace_name: openclaw-turn
  mappers:
  - name: default
    code: "def map_trace(trace, observations): ..."
```

### 迁移步骤

**1. 分析现有 Mapper 逻辑**

现有 Mapper 中的 `map_trace` 函数处理了三类逻辑：
- **心跳检测**：跳过空会话
- **工具调用提取**：从 `tool: <name>` 格式的 observation 中提取
- **Skill 名称提取**：从命令参数和 systemPrompt 中正则匹配

迁移到自定义 Source Adapter 时，这些逻辑直接搬入 `convert_session` 即可，因为 `convert_session` 拿到的是同样的原始 trace + observations 数据。

**2. 确认数据获取方式**

自定义数据源有两种路径：
- **直接调 Langfuse API**（与现有管线相同的传输层，只是不走内置 adapter）
- **直连业务后端 API**（绕过 Langfuse，从业务系统直接取数据）

**3. 编写适配器文件**

如果仍调 Langfuse API 但需自定义转换逻辑：

```python
import httpx
import re
import json

def build_adapter(config, options):
    return CustomLangfuseAdapter(config, options)

class CustomLangfuseAdapter:
    source_type = "custom-langfuse"

    def __init__(self, config, options):
        self.options = options or {}
        self.host = self.options.get("host", "")
        self.public_key = self.options.get("public_key", "")
        self.secret_key = self.options.get("secret_key", "")

    def _client(self):
        return httpx.Client(
            base_url=f"{self.host}/api/public",
            auth=(self.public_key, self.secret_key),
            timeout=30,
        )

    def list_session_ids(self, filters, *, max_sessions):
        with self._client() as c:
            resp = c.get("/sessions", params={
                "fromTimestamp": filters.get("from_timestamp", ""),
                "toTimestamp": filters.get("to_timestamp", ""),
                "limit": max_sessions,
            })
            resp.raise_for_status()
            return [s["id"] for s in resp.json().get("data", [])][:max_sessions]

    def fetch_session(self, session_id):
        with self._client() as c:
            resp = c.get(f"/sessions/{session_id}")
            resp.raise_for_status()
            session = resp.json()
            trace_name = self.options.get("trace_name", "")
            traces = session.get("traces", [])
            if trace_name:
                traces = [t for t in traces if t.get("name") == trace_name]
            return session, traces

    def convert_session(self, session, traces):
        # 搬入现有 Mapper 的转换逻辑
        turns = []
        for i, trace in enumerate(traces, 1):
            obs = trace.get("observations") or []
            tool_calls = []
            tool_results = []
            used_skills = []

            for o in obs:
                name = str(o.get("name") or "")
                if not name.startswith("tool:"):
                    continue
                tool = name.split(":", 1)[1].strip()
                tool_calls.append({
                    "id": str(o.get("id") or ""),
                    "type": "function",
                    "function": {"name": tool, "arguments": json.dumps(o.get("input") or {})},
                })
                output_text = _extract_text(o.get("output"))
                tool_results.append({
                    "tool_call_id": str(o.get("id") or ""),
                    "tool_name": tool,
                    "content": output_text,
                    "has_error": str(o.get("level") or "").upper() == "ERROR",
                })

            turns.append({
                "turn_num": i,
                "prompt_text": str(trace.get("input") or ""),
                "response_text": str(trace.get("output") or ""),
                "tool_calls": tool_calls,
                "tool_results": tool_results,
                "used_skills": used_skills,
                "metrics": {"tool_call_count": len(tool_calls), "total_tokens": 0},
            })
        return {"session_id": session.get("id") or "", "turns": turns}

    def close(self):
        pass

def _extract_text(value):
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, dict):
        content = value.get("content")
        if isinstance(content, list):
            parts = []
            for item in content:
                if isinstance(item, dict) and isinstance(item.get("text"), str):
                    parts.append(item["text"])
            return "\n".join(parts)
        if isinstance(content, str):
            return content
        return json.dumps(value, ensure_ascii=False)
    return str(value)
```

**4. 切换配置**

```yaml
# 新配置
datasource:
  type: custom-langfuse
  options:
    host: https://ai-langfuse.sf-express.com
    public_key: pk-lf-xxx
    secret_key: sk-lf-xxx
    trace_name: openclaw-turn

# 旧 Langfuse 配置保持不变（控制台仍可用 Langfuse 页面查看连接状态）
langfuse:
  enabled: true
  host: https://ai-langfuse.sf-express.com
  # ...
```

**5. 对比验证**

切换前先用同一时间窗口分别用旧 Langfuse adapter 和新自定义 adapter 各拉取 10 个会话，核对：
- Session ID 集合是否一致
- 每个会话的轮次数是否一致
- prompt_text / response_text 是否一致
- tool_calls / tool_results 数量和内容是否一致
- used_skills 是否一致

确认无误后再切换生产流量。

## 三层机制的关系

文件级 Source Adapter 与现有两层机制**不冲突**，可以组合使用：

| 层级 | 文件位置 | 解决什么 | 热加载 |
|------|----------|----------|--------|
| Source Adapter | `session_ingestion/adapters/sources/<type>.py` | 数据从哪来、怎么拉、怎么转 | 是 |
| Per-agent Hook | `session_ingestion/adapters/<agent_id>.py` | 同一来源下不同 agent 的业务逻辑 | 是 |
| Langfuse Mapper | 控制台 YAML 内联 | Langfuse 来源下 trace 格式微调 | 否（YAML） |

典型组合：Source Adapter 定义数据拉取和基本转换；Per-agent Hook 为特定 agent 做额外过滤和字段提取。

## 错误处理与调试

### 适配器加载失败

适配器文件有语法错误或缺少必需方法时，管线**不会静默降级到 Langfuse**，而是直接报错：

```
SourceAdapterError: source adapter /path/to/my-source.py is missing required methods: list_session_ids
```

检查点：
- 文件是否定义了 `build_adapter(config, options)` 函数
- `build_adapter` 返回的对象是否有 `list_session_ids`、`fetch_session`、`convert_session` 三个方法
- 文件是否有 Python 语法错误（`python -m py_compile my-source.py`）

### 会话被跳过为 empty

如果拉取后大量会话状态为 `empty`，检查 `convert_session` 的输出：
- 每个 turn 的 `prompt_text` 或 `response_text` 至少一个非空
- 如果两个都为空，该会话会被判定为无意义内容并跳过

### 控制台预览不显示元数据

默认情况下，自定义数据源的预览只返回 `{"session_id": "xxx"}` 最小行。如需在控制台展示标题、用户、时间等信息，实现可选的 `preview_sessions` 方法：

```python
def preview_sessions(self, filters, max_sessions=100):
    """返回富元数据行列表（可选）"""
    return [
        {
            "session_id": "s1",
            "title": "处理报表任务",
            "user_id": "u12345",
            "timestamp": "2026-09-01T10:00:00Z",
            "trace_count": 3,
        },
    ][:max_sessions]
```

### 第三方依赖

适配器在 teamEvolver 服务进程内运行。`httpx` 已是内置依赖，可直接使用。如需其他库：

1. 在 `docker/build-requirements.in` 中添加依赖
2. 重新生成锁文件（`pip-compile`）
3. 重新构建镜像

离线部署时，在联网构建机上准备好 wheel 包，通过 `docker save/load` 传输。

## 参考文件

| 文件 | 说明 |
|------|------|
| `teamEvolver/integrations/source_adapter.py` | Source Adapter 核心机制：自动发现、热加载、模板 |
| `teamEvolver/integrations/langfuse_convert.py` | 内置 Langfuse 转换逻辑（参考实现） |
| `teamEvolver/integrations/langfuse_mapper.py` | Langfuse Mapper 机制和标准格式定义 |
| `teamEvolver/integrations/langfuse_pull.py` | 采集管线编排层（调用 adapter 的地方） |
| `teamEvolver/config_store/defaults.py` | 默认配置定义 |
| `docs/schemas/agent-session-v1.schema.json` | Session Schema 正式定义 |
