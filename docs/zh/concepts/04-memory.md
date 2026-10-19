# Memory 体系

Memory 是可检索的长期事实、背景、偏好和团队共识。它为 Agent 提供上下文，不规定完整任务流程。个人 Memory 和团队 Memory 都保存在 OpenViking；Memory 运行、阶段、检查点、Memory Change 和 Replay 记录保存在 PostgreSQL。

## 个人 Memory 与团队 Memory

| 维度 | 个人 Memory | 团队 Memory |
|------|-------------|-------------|
| 路径 | `viking://user/<user>/memories/` | `viking://resources/<shared_knowledge_prefix>/`，默认 `shared-knowledge` |
| Agent 权限 | Context `remember` / `forget` 只写自己的 Memory | 只读 |
| 控制台 | 本人编辑自己的 Memory；管理员可切换用户 | 仅租户管理员编辑 |
| 来源 | Agent 写入、用户编辑 | 跨用户聚合 + DreamCycle 维护 |

团队 Memory 位于 Account 共享的 Resources 命名空间，授权用户都能检索同一份产物，写权限仍由 teamEvolver 控制。

## Memory Evolution

一次 Memory 运行由 `POST /api/aggregation/run`（`pipeline` 为 `both`、`aggregation` 或 `maintenance`）或 `POST /api/memory/maintenance/run`（固定 `maintenance`）受理。API 固定当时的 `memory` 模块配置 revision，写入 `memory_runs` 和 `memory.aggregate` 任务后返回 `202`。Worker 执行 `team_memory/pipeline.py`：

1. **排他**：同一 OpenViking Account 同时只允许一个 Memory 运行，由 `resource_leases` 的 `memory:account:<account>` 租约保证；被占用时 5 秒后重试。
2. **变更前快照**：把目标目录复制到运行工作根 `before/`。
3. **聚合阶段**（`aggregation`、`both`）：
   - 固定聚合 Skill（默认 `viking://agent/skills/team-memory-okf`）到工作根，记录 revision。
   - 分页枚举 Account 用户（默认每页 1000，安全上限 50000），排除 merge 身份自己。
   - 逐个用户读取 `memories/` 下所选类型的原文，确定性写入私有 staging 的 `snapshot.jsonl`；单用户上限 20 MiB，不调用模型。
   - 增量模式下，用户原文指纹和 Skill revision 都未变化时复用上次快照。
   - 以 `merge_fan_in`（默认 4，范围 2–15）为组做 tree-reduce Compile；用户数达到 `partition_threshold`（默认 512）时先按稳定哈希分为 `partition_count`（默认 256）个分区。
4. **维护阶段 DreamCycle**（`maintenance`、`both`）：固定维护 Skill（默认 `viking://agent/skills/team-memory-maintenance`），把当前团队 Memory 复制为私有快照作为输入，再 Compile 回同一目录，完成去重、修订和过时标记。
5. **变更后快照**：复制到 `after/`，并写入一条 Memory Change（before/after 引用）。

每个阶段完成后写入 `memory_stages`；每个用户的快照指纹写入 `memory_checkpoints`。Worker 崩溃或租约过期后，另一个 Worker 从已完成阶段续跑，不重复已完成的用户快照或 Compile。

工作根：

```text
个人源     viking://user/<user>/memories/<kind>/
工作根     viking://user/<merge-user>/resources/teamEvolver/staging/<run-id>/
最终根     viking://resources/<shared_knowledge_prefix>/
```

工作根位于 merge 身份的私有 Resources，原始快照与中间产物不暴露到 Account 共享 Resources。

## 聚合 Skill 与维护 Skill

两个 Skill 定义聚合输出结构和维护规则，保存在 OpenViking。管理员在「进化链路 → 团队 Memory 自进化」中读写：

- `GET /api/aggregation/okf-skill?stage=aggregation|maintenance`
- `PUT /api/aggregation/okf-skill?stage=...`：Skill `name` 必须与配置 URI 末段一致，正文不超过 128 KiB；保存作为 `memory.compile` 任务受理，返回 `202`。

Skill 变化只使下一次运行重新执行受影响的 Compile，不重复复制未变化用户的快照。

## Memory Change 与 True Replay

`GET /api/memory/changes` 列出 Memory Change。管理员可以把一次变更作为 Memory Replay：

- `POST /api/memory/changes/{change_id}/true-replay`：读取变更前后快照（上限 2 MiB），Baseline 为变更前内容，Candidate 为变更后内容。
- `POST /api/openviking/memory/true-replay`：直接提交 `memory_path`、`before_content`、`after_content`。

两者都作为 `memory.replay` 任务执行，复用 [True Replay](./06-true-replay.md) 的 Checklist 门禁和效率比较。历史结果见 `GET /api/memory/changes/{change_id}/true-replays`。

## 资产页编辑

「个人与团队资产」通过 `/api/openviking/workspace/*` 浏览和编辑 `viking://user` 与 `viking://resources`：

- 浏览目录、文件及 L0/L1 摘要
- 管理员或资产所有者进入编辑模式，跨多个文件保留草稿，统一查看 Diff 后批量保存
- 批量写使用内容哈希前置条件，防止覆盖并发修改

## 配置

`memory` 模块配置保存在 `tenant_configs`，`GET/POST /api/aggregation/settings` 读写其中的 `aggregation` 对象，必须携带 `expected_revision`：

```json
{
  "expected_revision": 3,
  "shared_knowledge_prefix": "shared-knowledge",
  "okf_skill_uri": "viking://agent/skills/team-memory-okf",
  "maintenance_skill_uri": "viking://agent/skills/team-memory-maintenance",
  "kinds": [],
  "account_user_limit": 50000,
  "account_user_page_size": 1000,
  "merge_fan_in": 4,
  "merge_concurrency": 4,
  "partition_threshold": 512,
  "partition_count": 256,
  "preserve_manual_edits": false,
  "incremental_publish": false,
  "publish_batch_users": 8
}
```

`preserve_manual_edits=true` 时，当前团队 Memory 作为额外输入参与聚合；`incremental_publish=true` 时只把变化用户按 `publish_batch_users` 分批写入目标目录。维护默认手动运行；将 `maintenance.trigger_mode` 设为 `automatic` 后，Worker 按 `maintenance.interval_seconds` 定时受理维护任务，管理员仍可随时手动运行。

## 代码入口

| 模块 | 路径 |
|------|------|
| HTTP | [api/](../../../team_memory/api/) |
| 运行受理 | [runs.py](../../../team_memory/runs.py) |
| 流水线 | [pipeline.py](../../../team_memory/pipeline.py) |
| Compile 分组 | [merge.py](../../../team_memory/merge.py) |
| Worker Handler | [handlers.py](../../../team_memory/handlers.py) |
| 仓库 | [repository.py](../../../team_memory/repository.py) |
| OpenViking 适配 | [openviking.py](../../../team_memory/openviking.py) |
| 维护 Skill 默认内容 | [maintenance_skill.py](../../../team_memory/maintenance/maintenance_skill.py) |
| Memory Replay | [replay.py](../../../team_memory/replay.py) |

## 相关文档

- [存储空间与目录布局](./09-storage-layout.md)
- [True Replay](./06-true-replay.md)
- [团队记忆聚合 API](../api/11-team-memory-aggregation.md)
