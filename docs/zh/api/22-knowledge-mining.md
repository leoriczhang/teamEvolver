# 知识库挖掘 API

知识库挖掘把 OpenViking 中的原始知识目录编译为结构化 Wiki。TeamEvolver 负责受理、权限、持久化状态与结果核对；编译由 OpenViking 原生 Compile 使用挖掘 Skill 执行。使用方法见 [知识库挖掘指南](../guides/07-skill-miner.md)。

代码入口：`team_miner/knowledge_miner/api/__init__.py:create_router`、`team_miner/knowledge_miner/handlers.py`、`team_miner/knowledge_miner/config.py:load`、`team_miner/knowledge_miner/schemas.py`。

所有接口要求租户管理员，前缀 `/api/knowledge-mining`。

## 配置

配置保存在租户 `miner` 模块配置（`tenant_configs` 中 `module='miner'` 的行），没有专用写入接口，由运维修改。未设置的字段取默认值：

| 字段 | 默认 | 约束 |
| --- | --- | --- |
| `source_uri` | `viking://resources/agent_knowledge_workspace/input/raw_knowledge_base` | 必须在 `viking://resources/` 下 |
| `target_uri` | `viking://resources/agent_knowledge_workspace/output/processed_knowledge` | 必须在 `viking://resources/` 下 |
| `skill_uri` | `viking://agent/skills/llm-wiki` | 必须在 `viking://agent/skills/` 下 |
| `account_id` | 空，取租户 `sharing_viking_account` | — |
| `user_id` | `team` | 访问 OpenViking 使用的 User |

配置中不能出现凭证字段（名称含 key、password、token），也不能出现未知字段，否则所有接口返回 503。请求中的 `account` 参数必须等于生效的 Account，否则返回 `403 TENANT_ACCOUNT_MISMATCH`。

源目录和目标目录按父目录到子目录的顺序自动初始化；已有目录与内容保持不变。
默认 `llm-wiki` Skill 缺失时由 Worker 自动安装，自定义 `skill_uri` 不会自动生成。

## 受理与任务

写操作（编译、上传、编辑、删除、生成图谱）都受理为 `miner.track_compile` 任务，返回：

```json
{"ok": true, "job_id": "miner:...", "status": "queued", "status_url": "/api/jobs/miner:...", "account": "demo"}
```

支持 `Idempotency-Key`：相同 Key 与相同请求返回同一任务，相同 Key 与不同请求返回 409。编译任务提交到 OpenViking 后，Worker 保存上游任务回执并轮询结果；完成后记录 `last_compile_time` 水位，并派生 `miner.rebuild_index` 任务重建 Wiki 索引。

## 接口

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| GET | `/config?account=` | 生效配置、`revision` 与 Account |
| GET | `/capabilities?account=` | OpenViking Compile 能力与上次 `last_compile_time` |
| POST | `/tasks` | 发起编译，见下文 |
| GET | `/tasks?account=` | 本租户发起过的 OpenViking 编译任务 |
| GET | `/tasks/{identifier}?account=` | 单个上游任务的状态与对应 `job_id` |
| POST | `/tasks/{identifier}/cancel` | 取消上游任务，同时请求取消本地任务 |
| GET | `/jobs/{job_id}/result` | 任务结果，未完成返回 404 |
| GET | `/tree?kind=source|wiki` | `source_uri` 或 `target_uri` 的目录树（最多 10000 个节点、20 层） |
| GET | `/wiki-roots` | Wiki 根目录 |
| GET | `/content?uri=&kind=source|wiki` | 读取一个文件 |
| POST | `/content` | `{"kind", "uri", "content"}` 写入一个文件（≤2 MiB） |
| POST | `/upload` | 上传文件，见下文 |
| DELETE | `/entry?uri=&kind=source|wiki` | 删除一个文件或目录（不能删除根目录） |
| POST | `/graph` | 生成 Wiki 图谱，结果含 `graph` 与 `html` |

所有 URI 必须位于对应根目录内，不能包含 `..`、隐藏路径段或编码字符。

### POST /api/knowledge-mining/tasks

| 字段 | 说明 |
| --- | --- |
| `account` | 可选，必须等于生效 Account |
| `instruction` | 可选，附加说明，≤4000 字符 |
| `last_compile_time` | 可选，ISO 8601；省略时使用上次编译的水位，实现增量编译 |
| `source_uri` / `target_uri` / `skill_uri` | 可省略；提供时必须等于配置值 |

请求体上限 128 KiB。

### POST /api/knowledge-mining/upload

```json
{"kind": "source", "directory_uri": "viking://resources/.../raw_knowledge_base/policies",
 "files": [{"path": "退货政策.docx", "content_b64": "..."}]}
```

| 限制 | 值 |
| --- | --- |
| 文件数 | 1–200 |
| 单文件 | 20 MiB |
| 总大小 | 100 MiB |
| 支持格式 | `.md`、`.markdown`、`.txt`、`.docx`、`.xlsx`、`.pdf`、`.csv`、`.pptx` |

Worker 把每个文件转换为 Markdown 后写入目标目录（扩展名替换为 `.md`），两个文件转换后同名时请求被拒绝。

## 错误

| 状态 | 原因 |
| --- | --- |
| 400 | 字段非法、URI 越界、文件超限、`instruction` 过长、尝试修改配置的根目录或 Skill |
| 403 | 非租户管理员，或 `TENANT_ACCOUNT_MISMATCH` |
| 404 | 结果未就绪，或上游任务不属于当前租户 |
| 409 | `Idempotency-Key` 被用于不同请求 |
| 413 | 请求体超过上限 |
| 503 | `miner` 模块配置缺失或非法 |
| 4xx/5xx | OpenViking 返回的错误 |
