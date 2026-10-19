# 存储空间与目录布局

teamEvolver 的数据分为四类，归属固定：

| 存储 | 保存 | 不保存 |
| --- | --- | --- |
| PostgreSQL（默认 Schema `teamevolver`） | 全局配置、租户与成员、Secret Ref、Adapter、Session、Skill 版本链、Candidate、Evidence、Dataset、Replay、Memory 运行与检查点、Context 记录、Worker 任务、租约、用量、审计 | 外部知识制品 |
| PostgreSQL `storage_objects`（ObjectStore） | 不可变 payload、阶段结果、报告、Team Miner 水位与回执等不适合按字段查询的对象 | 领域主数据 |
| OpenViking | 个人与团队 Memory、个人 Skill、团队 Resources、Wiki、Compile Task | TeamEvolver 可变业务状态 |
| 本机目录 | 临时文件、日志、PID、可重建的 Skill 运行缓存 | 任何唯一副本 |

多副本之间只通过 PostgreSQL 协调。正式环境中 PG 或 OpenViking 故障时不回退本机目录：API 返回错误，Worker 退避重试，`/readyz` 返回 503。

## PostgreSQL

所有业务表都有 `tenant_id` 列并启用 `FORCE ROW LEVEL SECURITY`，策略为 `tenant_id = current_setting('app.tenant_id')`。`teamEvolver/database/transaction.py` 的租户事务在事务内设置该变量；运行时角色必须是 NOSUPERUSER、NOBYPASSRLS，否则 RLS 失效。

| 版本 | 内容 |
| --- | --- |
| `001_init.sql` | `tenants`、`teamevolver_config`、`tenant_configs`、`secret_refs`、`adapters`/`adapter_versions`/`adapter_bindings`、`worker_tasks`、`resource_leases`、`tenant_usage`、`artifacts`、`audit_log` |
| `002_session_ingestion.sql` | `ingestions`、`session_stage_results`、`sessions` |
| `003_skills.sql` | `skills`、`skill_versions`、`skill_blobs`、`skill_version_files`、`skill_mutations`、`skill_candidates*` |
| `004_skill_evolution.sql` | `skill_commands`、`skill_evolution_branches`、`skill_evolution_sessions`、`skill_evidence` |
| `006_experiences.sql` | `skill_experiences`、`experience_*` |
| `007_skill_transfer.sql` | `skill_transfers` |
| `009_replay.sql` | `datasets`、`dataset_revisions`、`dataset_cases`、`dataset_materials` |
| `010_memory_context.sql` | `context_*`、`memory_runs`、`memory_stages`、`memory_checkpoints`、`memory_changes` |
| `011_storage.sql` | `storage_objects` |
| `012_objects.sql` | `objects` 与租户 RLS |
| `013_console_identity.sql` | `console_users`、`tenant_memberships`、`console_*` |
| `014_tenant_module_defaults.sql` | 为已有租户补齐 5 个模块默认配置行 |

`schema_versions` 由 `teamEvolver/database/schema_runner.py` 维护。
开源版 Schema 是全新安装基线，版本号允许有空缺；只在空数据库中初始化。

### 配置

- `teamevolver_config`：单行全局配置（OpenViking 部署、团队显示名、链路追踪等），带 `revision`。
- `tenant_configs`：主键 `(tenant_id, module)`，`module` 为 `session`、`skills`、`replay`、`memory`、`miner`。创建租户时一次写入 5 行；运行时缺行直接返回 503，不回退 YAML。
- 写入都使用 `expected_revision` CAS；长任务在 `worker_tasks.config_revision` 和任务 payload 中固定配置。
- 凭证只以 Secret Ref（`sref_...`）出现在配置中，密文在 `secret_refs`，由 `TEAMEVOLVER_SECRET_MASTER_KEY` 加密。

## ObjectStore

`teamEvolver/storage/postgres.py:PostgresObjectStore` 按 `(tenant_id, key)` 存字节和 SHA-256，读取时校验 hash。常见 key：

| 前缀 | 内容 | 写入方 |
| --- | --- | --- |
| `ingest/<tenant>/<ingestion>.json` | Session 原始 payload | Session 接入 |
| `session/<hash>/<stage>/<digest>.json` | 每个分析阶段的结果 | Session Worker |
| Team Miner 前缀 | Compile 水位、回执、链接索引 | Team Miner |

控制台「平台资产」以 `platform://` URI 只读展示这些对象，见 [平台资产与 Workspace API](../api/23-workspace-assets.md)。

## 租户 ↔ OpenViking 空间映射

每个租户绑定一个 OpenViking Account（`sharing_viking_account`，创建租户时默认等于 `tenant_id`）。TeamEvolver 使用单一 Root Key 访问所有 Account，空间由请求头决定：

| 请求头 | 值 |
| --- | --- |
| `X-OpenViking-Account` | 租户绑定的 Account |
| `X-OpenViking-User` | 个人空间为用户 ID；团队资源为 `sharing_viking_user`（默认 `team`） |
| `X-API-Key`、`Authorization: Bearer` | Root Key（`TEAMEVOLVER_OV_API_KEY` 或控制台保存的 Secret Ref） |
| `X-OpenViking-Role: admin` | 仅共享管理操作（如 Skill 发布到 `viking://agent/skills`） |

客户端提供的 Account 不能覆盖租户配置。Root Key 只在服务端使用，不返回给浏览器或 Agent。

### 作用域

作用域定义在 `teamEvolver/platform_docs/workspace_core.py:_scope_map`，Context 接口和「个人与团队资产」共用：

| 作用域 | OpenViking 根 URI | 普通用户可写 |
|--------|-------------------|--------------|
| `personal_memory` | `viking://user/<user>/memories` | 是 |
| `personal_skills` | `viking://resources/<root_prefix>/peers/<user>/skills` | 是 |
| `personal_resources` | `viking://user/<user>/resources` | 是 |
| `personal_workspace` | `viking://user` | 是（仅本人子树） |
| `team_memory` | `viking://resources/<shared_knowledge_prefix>` | 否，仅租户管理员 |
| `team_skills` | `viking://resources/<root_prefix>/skills` | 否，仅租户管理员 |
| `team_resources` | `viking://resources/team` | 否，仅租户管理员 |
| `team_workspace` | `viking://resources` | 否，仅租户管理员 |

| 变量 | 来源 | 默认 |
|------|------|------|
| `root_prefix` | `sharing_viking_root_prefix` | `team-skill-evolver`（数据契约常量） |
| `shared_knowledge_prefix` | `memory` 模块 `aggregation.shared_knowledge_prefix` | `shared-knowledge` |
| `user` | Agent 请求的 `user_id` 或控制台用户 ID | — |

团队 Skill 的权威版本在 PostgreSQL；`team_skills` 作用域是 OpenViking 侧的团队 Skill 目录，供 Context 检索使用。

### 目录骨架

创建租户或执行 `POST /api/sharing-config/bootstrap-dirs` 时，`teamEvolver/admin/openviking_dirs.py:ensure_openviking_dirs` 幂等创建缺失目录，已有内容不覆盖：

```text
viking://resources/team
viking://resources/<root_prefix>/{skills,peers}
viking://resources/<root_prefix>/peers/<user>/skills
viking://resources/<shared_knowledge_prefix>
viking://user/<user>/{memories,resources,skills}
```

### 其他 OpenViking 路径

| 路径 | 用途 |
| --- | --- |
| `viking://agent/skills/team-memory-okf`、`team-memory-maintenance` | Memory 聚合与维护 Skill |
| `viking://user/<merge-user>/resources/teamEvolver/staging/<run>/` | Memory 运行工作根（私有） |
| `viking://resources/agent_knowledge_workspace/input/raw_knowledge_base` | Team Miner 默认知识源 |
| `viking://resources/agent_knowledge_workspace/output/processed_knowledge` | Team Miner 默认 Wiki 输出 |
| `viking://resources/agent_knowledge_workspace/input/proven_experiences` | 成功经验同步默认目录 |
| `viking://agent/skills/llm-wiki` | Team Miner 默认 Compile Skill |

## 本机目录

| 路径 | 内容 | 可删除 |
| --- | --- | --- |
| `~/.teamEvolver/teamEvolver.log`（`TEAMEVOLVER_LOG_DIR`） | 服务日志 | 是 |
| `~/.teamEvolver/runtime/skills`（`skills_runtime_cache_dir`） | Skill 运行缓存 | 是，下次读取时重建 |
| `/tmp/te_runtime.log` | `--daemon` 默认日志 | 是 |
| PID 文件 | `teamEvolver start --daemon` 记录的进程 | 进程停止后 |

## 控制台可视化

| 页面 | 数据源 |
| --- | --- |
| 个人与团队资产 | OpenViking，按上表作用域 |
| 平台资产 | PostgreSQL ObjectStore，只读 |
| 运行状态 | `GET /api/storage/status`：连接池与后端（固定 `postgres`） |

## 代码入口

| 模块 | 路径 |
|------|------|
| 连接池与事务 | [pool.py](../../../teamEvolver/database/pool.py)、[transaction.py](../../../teamEvolver/database/transaction.py) |
| RLS | [rls.py](../../../teamEvolver/database/rls.py) |
| Schema | [schema/](../../../teamEvolver/database/schema/) |
| ObjectStore | [postgres.py](../../../teamEvolver/storage/postgres.py) |
| OpenViking 客户端 | [client.py](../../../teamEvolver/openviking/client.py) |
| 作用域 | [workspace_core.py](../../../teamEvolver/platform_docs/workspace_core.py) |
| 目录骨架 | [openviking_dirs.py](../../../teamEvolver/admin/openviking_dirs.py) |

## 相关文档

- [架构总览](./01-architecture.md)
- [Memory 体系](./04-memory.md)
- [配置参考](../guides/01-configuration.md)
