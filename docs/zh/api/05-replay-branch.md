# Replay 与实验工作台 API

True Replay 用租户绑定的 Replay Adapter 驱动客户真实 Agent，对 Baseline 与 Candidate 两个分支执行相同任务，由 TeamEvolver 负责 Checklist 裁判、拟人化用户反馈和效率比较。概念见 [True Replay](../concepts/06-true-replay.md)。

代码入口：

- Adapter 控制面：`team_replay/api/adapters.py:create_router`
- 数据集：`team_replay/api/datasets.py:create_router`
- Replay 运行：`team_replay/api/runs.py:create_router`
- 实验工作台：`team_skills/api/workbench.py:create_router`
- 运行接口：`team_replay/hooks.py`；源码校验与加载：`team_replay/adapters/loader.py`

所有接口要求租户管理员；源码读写与测试要求服务 Root Key。

## Replay Adapter

Replay Adapter 是租户提供的 Python 文件，源码按版本保存在 PostgreSQL，绑定保存在 `adapter_bindings`（`binding_key='replay'`）。每次运行固定受理时的 revision。

### 文件要求

- 文件名匹配 `[A-Za-z0-9][A-Za-z0-9_-]*.py`，内容 1 字节到 256 KiB。
- 顶层恰好一个 `build_replay_adapter(config)` 函数，返回实现 `open(context)` 的工厂。
- 顶层字面量字典 `REPLAY_ADAPTER`：`label`（文本，≤160 字符）、`enabled`（bool，默认 true）。

```python
from team_replay.hooks import AgentObservation

REPLAY_ADAPTER = {"label": "Customer Agent", "enabled": True}


def build_replay_adapter(config):
    return CustomerFactory(config)


class CustomerFactory:
    def __init__(self, config):
        self.config = config

    def open(self, context):
        # 为本分支创建独立会话与工作区，应用 context.treatment，
        # 返回实现 send(user_message) -> AgentObservation 与 close() 的对象。
        raise NotImplementedError
```

Adapter 在 Worker 进程内执行。AST 校验只检查结构，不是沙箱，因此源码只能由 Root Key 写入。

`config` 是受理时冻结的 `replay` 模块配置，以 `SimpleNamespace` 传给工厂；可用 `vars(config)` 转成字典。接入参数放在该模块配置中，绑定接口只选择源码文件及 revision。

### 运行接口

| 对象 | 字段 / 方法 |
| --- | --- |
| `ReplayContext` | `request_id`、`runtime_type`、`treatment`（`branch` 为 `baseline` 或 `candidate`，`skill` 为该分支的 Skill bundle）、`materials`、`context_snapshot`、`timeout_seconds`、`source_session`（冻结的来源 Session）、`source_turn_num`（来源轮次，默认 1） |
| `ReplaySession` | `send(user_message) -> AgentObservation`、`close()` |
| `AgentObservation` | `response`（必填）、`messages`、`artifacts`、`metrics`、`trace_id`、`metrics_incomplete_reason` |

首轮任务和后续用户反馈都通过 `send()` 传入，Adapter 负责保持对话连续。每个分支必须使用独立会话和工作区，模型、工具、初始材料、上下文和限制保持一致，唯一差别是 treatment。无法保证隔离时抛出 `team_replay.hooks.ReplayUnsupported`。

`metrics` 只填运行时真实观测到的值（如 `tool_call_count`、`total_tokens`），缺失项记为不可用，不补零。任何轮次都不会向 Adapter 传入 Checklist 或裁判结论。

仓库提供两个逐轮传输实现，都暴露 `call_turn(request) -> result`（协议 `teamevolver.replay-turn-request.v1` / `-result.v1`，见 `team_replay/protocol.py`），Adapter 的 `send()` 可以委托给它们：

| 传输 | 位置 | 用途 |
| --- | --- | --- |
| `TurnBasedReplayAdapter` | `team_replay/adapters/__init__.py` | 调用实现逐轮协议的 HTTP 端点 |
| `MappedHttpAdapter` | `team_replay/adapters/__init__.py` | 用请求模板与响应映射调用客户自有 HTTP Agent |

传输的 API Key 从环境变量 `TEAMEVOLVER_AGENT_<PROFILE>_REPLAY_API_KEY` 读取（`<PROFILE>` 为 `auth_profile` 的大写形式）。`scripts/replay_turn_server.py` 是逐轮协议的 Agent 侧示例服务：`python scripts/replay_turn_server.py --port 8010 --api-key <secret>`，端点为 `POST /turn/<runtime_type>`。

### AgentsHub 单文件接入

上传 `team_replay/adapters/examples/agentshub.py` 并绑定 `agentshub.py`，在租户 `replay` 配置中设置：

```json
{
  "timeout_seconds": 600,
  "max_interactions": 4,
  "agentshub": {
    "backend_root": "/opt/AgentsHub/backend",
    "tenant_id": "demo",
    "agent_id": "agent-example",
    "user_id": "user-example",
    "systemd_scope": "system"
  }
}
```

每个执行 Worker 都需安装可访问的 AgentsHub backend 及其 Python 环境；`python` 可覆盖默认的 `backend_root/.venv/bin/python`，`pi_command` 可指定 Pi 启动器的绝对路径，避免依赖 Worker 的 PATH。来源 Session 的身份优先于配置默认值。Adapter 使用 AgentsHub 已安装的 Harness 与隔离工具，为每个分支仅启动一次独立 SQLite、模型 broker、systemd worker 和工作区，后续 `send()` 继续同一个 Session，`close()` 清理整个隔离进程组及临时目录。AgentsHub 无需增加 HTTP 接口。

`systemd_scope` 默认使用 user systemd；设为 `system` 时需允许执行 `sudo -n systemd-run/systemctl`，隔离 Agent 仍使用当前普通用户身份。隔离失败会停止回放。Skill bundle 的 UTF-8 文件完整注入；不能挂载的二进制 Skill 文件会报错。裁判与用户反馈由 TeamEvolver 执行。工具轨迹和 Token usage 按本轮增量返回，缺失 usage 保持不可用。

### 接口

| 方法 | 路径 | 权限 | 说明 |
| --- | --- | --- | --- |
| GET | `/api/replay-adapter` | 租户管理员 | 当前绑定、可用 Adapter 与 `source_editable` |
| PUT | `/api/replay-adapter` | 租户管理员 | `{"file": "customer.py", "revision": 2}` 绑定；`file` 为空串解绑；省略 revision 取最新 |
| GET | `/api/replay-adapter/code?file=customer.py` | Root Key | 读取源码；省略 `file` 读取当前绑定 |
| PUT | `/api/replay-adapter/code` | Root Key | `{"file", "code", "expected_revision"}`；新建时 `expected_revision` 为 `null`，冲突返回 409 |
| POST | `/api/replay-adapter/code/test` | Root Key | `{"file", "code", "context", "query"}`，返回 202 |

测试请求的 `context` 只允许 `runtime_type`（必填）、`skill`（对象或 null）、`materials`（数组）、`context_snapshot`（对象）、`timeout_seconds`（1–120，默认 30）；`query` 为 1–32000 字符。测试草稿以 `test-<sha256>.py` 保存为独立 Adapter，不影响当前绑定，也不读取已发布数据。

## 数据集

数据集文档格式为 `team-replay.dataset`：

| 字段 | 说明 |
| --- | --- |
| `dataset_id`、`name`、`description` | 标识与说明 |
| `skills[]` | 关联的 Skill 集合，每项 `{"skill_id", "revision"?, "content_hash"?}`，`skill_id` 不重复 |
| `cases[]` | Replay Case：`case_id`、`query`（必填）、`skill_ids`、`checks`、`materials`、`provenance`、`replay`、`metadata` |
| `source`、`metadata` | 来源（手工或 Session 选择）与附加信息 |

Baseline 与 Candidate 必须解析出相同的 Skill 集合；实验可以替换其中一个或多个 Skill。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/datasets?search=&limit=100&offset=0` | 列表 |
| POST | `/api/datasets` | 创建（201）。传完整文档，或传 `{"name", "description", "session_ids" / "filters"}` 从 Session 生成 Case |
| GET | `/api/datasets/{dataset_id}?search=&limit=&offset=` | 详情，Case 分页最多 500 |
| PATCH | `/api/datasets/{dataset_id}` | 修改 `name`、`description`、`skills`、`cases`，可带 `expected_revision` |
| DELETE | `/api/datasets/{dataset_id}` | 删除 |
| POST | `/api/datasets/{dataset_id}/cases` | 从 Session 追加 Case |
| GET / PATCH / DELETE | `/api/datasets/{dataset_id}/cases/{case_id}` | 单个 Case |
| GET | `/api/datasets/{dataset_id}/export` | 导出文档 |
| POST | `/api/datasets/{dataset_id}/runs` | 以数据集当前 Skill 版本运行 Replay，返回 202 |
| GET | `/api/datasets/{dataset_id}/runs` | 运行列表 |
| GET | `/api/datasets/{dataset_id}/runs/{run_id}` | 运行详情 |
| GET | `/api/datasets/{dataset_id}/runs/{run_id}/cases/{case_id}` | 运行时固定的 Case |
| GET | `/api/datasets/{dataset_id}/runs/{run_id}/results/{case_id}` | 单个 Case 结果 |
| POST | `/api/datasets/{dataset_id}/runs/{run_id}/cancel` | 取消运行 |

## Replay 运行

`POST /api/replays/{domain}`，`domain` 为 `skill` 或 `memory`，返回 202：

| 字段 | 必填 | 说明 |
| --- | --- | --- |
| `subject_id` | 是 | 被评估的 Skill 或 Memory 标识 |
| `dataset` | 是 | 内联数据集文档 |
| `baseline` / `candidate` | 是 | Skill：`{"revision", "payload"}`；Memory：`{"revision", "uri", "content"}`，两个分支 URI 必须相同 |
| `runtime` | 是 | `{"type": "<runtime_type>", ...}` |
| `options` | 否 | `concurrency`（1–2）、`timeout_seconds`（30–1800）、`max_interactions`（1–20） |

```json
{"status": "queued", "run_id": "skill:...", "job_id": "skill:...", "status_url": "/api/jobs/skill:..."}
```

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/replays/{domain}/{run_id}` | 运行状态与结果 |
| GET | `/api/replays/{domain}/{run_id}/input` | 受理时固定的 `spec`、`job` 与 `input_hash` |

Case 中的材料在受理时读取并固定。带 `Idempotency-Key` 的重复请求返回同一个 `run_id`。

## 实验工作台

实验工作台在控制台中编辑团队 Skill 并对数据集运行 Replay。Skill 读写直接作用于 PostgreSQL 中的团队 Skill 库。

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/api/replay-lab/workspace`、`/api/replay-lab/skills` | 团队 Skill 索引 |
| GET | `/api/replay-lab/skills/{name}` | 当前版本 `SKILL.md`、文件列表与 sha256 |
| POST | `/api/replay-lab/skills/{name}/save` | `{"candidate_skill_md", "expected_sha256"}` 保存为新版本；sha 不匹配返回 409 |
| GET | `/api/replay-lab/workspace/{name}/file?path=` | 读取 bundle 文件 |
| PUT | `/api/replay-lab/workspace/{name}/file` | `{"path", "content", "expected_sha256"}` 写入文件（≤2 MiB），新文件 `expected_sha256` 为 `null` |
| GET / POST | `/api/replay-lab/datasets` | 列出（可按 `skill_id` 过滤）/ 保存数据集 |
| POST | `/api/replay-lab/datasets/{dataset_id}/clone` | 复制数据集，请求体必须为空 |
| DELETE | `/api/replay-lab/datasets/{dataset_id}` | 删除数据集 |
| POST | `/api/replay-lab/datasets/synthesize` | `{"skill_id", "session_ids"?}` 用最多 30 个 Session 合成 Case，返回 202 |
| GET | `/api/replay-lab/jobs/{job_id}/result` | 合成结果 |
| POST | `/api/replay-lab/runs` | `{"skill_id", "dataset_id", "candidate_skill_md", "candidate_files"?, "timeout_seconds"?, "max_interactions"?}`，返回 202 |
| GET | `/api/replay-lab/runs?skill_id=&limit=` | 运行历史 |
| GET | `/api/replay-lab/runs/{run_id}` | 运行详情 |

工作台运行以数据集声明的 Skill 版本作为 Baseline，以 `candidate_skill_md` 和 `candidate_files` 替换 `skill_id` 后作为 Candidate。数据集中的 `content_hash` 与版本内容不符时拒绝运行。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | 字段缺失或多余、文件名或源码不合法、选项越界 |
| 403 | 非租户管理员，或源码接口未使用 Root Key（`service Root Key required for Python source access`） |
| 404 | Adapter、数据集、Case 或运行不存在 |
| 409 | revision / sha256 冲突 |
| 413 | 请求体超过 96 MiB |
| 503 | 租户模块配置缺失 |
