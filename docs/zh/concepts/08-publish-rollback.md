# 发布与回滚

teamEvolver 中所有团队 Skill 的变更（发布、更新、回滚、删除）都必须经过 `SkillMutationService` 执行，保证事务性提交、版本单调递增、完整审计链和可靠的同步分发。**任何版本都不会被物理删除**——回滚通过创建新版本恢复历史内容实现，审计链始终完整。

## SkillMutationService

`SkillMutationService` 是团队 Skill 变更的唯一入口，负责：

- 事务性提交（commit records + tombstone）
- 持久化同步 outbox（确保下发可靠性）
- 版本号单调递增
- 保留完整审计链
- 幂等执行（同一 mutation_id 不重复执行）

所有直接操作 SkillHub 存储绕过此服务的写入都是不合规的，会破坏审计链和同步保证。

### 变更命令（SkillMutationCommand）

所有变更通过 `SkillMutationCommand` 描述：

| 字段 | 说明 |
|------|------|
| `action` | 操作类型：`publish`、`update`、`rollback`、`delete` |
| `name` | Skill 唯一标识名 |
| `mutation_id` | 变更唯一 ID（幂等键，重复提交返回已有结果） |
| `skills_dir` | publish/update 时，本地 Skill Bundle 目录路径 |
| `target_version` | rollback 时，目标回滚版本号 |
| `tenant_ids` | 目标租户/集成 ID 列表（用于定向同步） |
| `skill_filter` | 可选过滤器 |
| `metadata` | 附加元数据 |

## 事务性提交

执行变更时，`SkillMutationService` 按以下顺序写入记录：

1. **幂等检查**：查询 `skill_mutation_commits/{mutation_id}.json`，若已存在直接返回已有结果
2. **执行变更**：调用 SkillHub 执行实际写入
   - `publish`/`update`：调用 `push_skills` 上传 Skill Bundle 到存储
   - `rollback`：读取 `target_version` 的历史 Bundle，写回为当前活跃版本
   - `delete`：删除 Skill 对象子树，写入 tombstone
3. **写入 Commit 记录**：将变更结果写入 `skill_mutation_commits/`
4. **写入/更新 Outbox 事件**：在 `skill_sync_outbox/` 中创建或更新同步事件
5. **写入 Tombstone**（仅 delete 操作）：在 `skill_tombstones/{name}/v{version}.json` 中记录删除标记

所有写入操作在对象存储层面是原子的（put_object），Commit 记录和 Outbox 事件通过稳定指纹（`_stable_id`）做幂等去重，确保同一变更不会产生重复事件。

> **注意**：SkillHub 的 `push_skills` 操作如果返回 `uploaded=0`（内容无变化），MutationService 返回 `status: "unchanged"`，不创建新的 Outbox 事件。

## Commit 记录

每次变更产生一条不可变的 Commit 记录，路径为 `skill_mutation_commits/{mutation_id}.json`，schema 版本为 `teamevolver.skill-mutation-commit.v1`：

```json
{
  "schema_version": "teamevolver.skill-mutation-commit.v1",
  "mutation_id": "...",
  "action": "publish|update|rollback|delete",
  "expected": {
    "name": "skill-name",
    "version": 5,
    "sha256": "...",
    "tree_sha256": "..."
  },
  "tenant_ids": ["integration-1", "integration-2"],
  "event_id": "skill_evt_...",
  "result": { ... },
  "metadata": { ... },
  "committed_at": "2025-01-01T00:00:00+00:00"
}
```

Commit 记录是审计链的核心，记录了谁在什么时间做了什么变更、变更后期望的 Skill 状态（version、sha256、tree_sha256），以及关联的同步事件 ID。

## Tombstone（墓碑标记）

删除操作不直接抹除历史，而是写入 Tombstone：

- 路径：`skill_tombstones/{name}/v{version}.json`
- 内容：包含 `name`、`version`、`sha256`、`tree_sha256`、`deleted: true`、`deleted_at`、`mutation_id`
- 版本号：删除时版本号在当前版本基础上 +1（如当前 v3，删除后写入 v4 的 tombstone）
- 作用：标记该 Skill 已被删除，同时保留版本链连续性；`reconcile` 操作可根据 tombstone 补建缺失的 commit 记录

Tombstone 确保即使 Skill 对象被删除，版本历史和变更记录仍然可追溯。

## 持久化同步 Outbox

Commit 成功后，变更不会直接推送给 Agent，而是写入持久化 Outbox 队列，由后台 drain 过程异步分发。这保证了"至少一次"投递语义。

### Outbox 事件结构

路径：`skill_sync_outbox/{event_id}.json`，schema 版本为 `teamevolver.skill-sync-outbox.v1`：

```json
{
  "schema_version": "teamevolver.skill-sync-outbox.v1",
  "event_id": "skill_evt_...",
  "action": "publish|update|rollback|delete",
  "mutation_id": "...",
  "skills": [{ "name": "...", "version": 5, "sha256": "...", ... }],
  "tenant_ids": ["integration-1"],
  "status": "pending|synced|dead_letter|cancelled",
  "attempt": 0,
  "next_retry_at": "...",
  "deliveries": {
    "integration-1": {
      "status": "synced|pending|failed|cancelled",
      "attempt": 2,
      "acked_at": "...",
      "last_error": "...",
      "next_retry_at": "..."
    }
  },
  "created_at": "...",
  "updated_at": "..."
}
```

### 投递机制

`drain()` 方法消费 Outbox 事件：

1. 扫描 `skill_sync_outbox/` 前缀下的事件
2. 跳过 `synced`/`cancelled` 状态的事件
3. 检查 `next_retry_at`，未到期的计入 pending
4. 到期事件调用 `deliverer`（默认为 `sync_skill_event`）执行实际分发
5. 根据分发结果更新每个 integration 的投递状态：
   - **synced**：Agent 确认收到（`{"ok": true, "results": {...}}`）
   - **cancelled**：该 integration 不支持或被显式取消
   - **pending**：失败，指数退避重试（2^attempt 秒，最大 3600 秒）
   - **dead_letter**：重试次数 ≥8 次，进入死信队列
6. 所有 deliveries 都达到终态（synced/cancelled）后，事件标记为 `synced`

重试退避策略：第 N 次失败后等待 `min(3600, 2^N)` 秒后重试。

### Outbox 修复（Reconcile）

`reconcile()` 方法修复不一致状态：

- 扫描所有 commit 记录，检查对应的 outbox 事件是否存在，缺失则补建
- 扫描所有 tombstone，为没有 commit 记录的 tombstone 补建 commit 和 outbox 事件
- 扫描当前 manifest 中的 Skill，确保每个当前版本都有对应的 commit 记录
- 这在服务重启、存储部分失败或手动修复后恢复一致性

## 版本单调递增

teamEvolver 中的 Skill 版本号始终单调递增，永不复用旧版本号：

- 每次 publish/update 创建新版本号（由 `SkillIDRegistry.record_update` 分配）
- 删除操作在当前版本基础上 +1 写入 tombstone
- 回滚操作也创建新版本号：读取历史版本的 Bundle 内容，写入新的版本号
- 版本链只追加（append-only），不修改已有版本记录

版本号由 `SkillIDRegistry` 管理，每次 `record_update` 自增。Registry 本身也持久化在对象存储中。

## 审计链

完整的审计链由以下部分构成：

1. **Commit 记录**（`skill_mutation_commits/`）：每次变更的不可变记录，包含 who（actor/uploaded_by）、when（committed_at）、what（action、expected 状态）、why（metadata）
2. **版本历史**（Registry）：每个 Skill 的完整版本号序列和每个版本的 action（create/update/rollback/delete）、时间戳
3. **版本 Bundle**（`skills/{name}/versions/v{n}/`）：每个版本的完整 Skill Bundle 快照，可随时重建
4. **Tombstone**（`skill_tombstones/`）：删除操作的标记
5. **Outbox 事件**（`skill_sync_outbox/`）：分发记录，包含每个 integration 的投递历史、重试次数、错误信息
6. **投递审计**（deliveries 中的 audit 字段）：取消/重试操作的操作人、原因、时间

从任意时间点的当前状态，都可以通过版本链追溯到该 Skill 的完整变更历史，包括每一次发布、更新、回滚和删除。

## 回滚即新版本

回滚不是恢复到旧版本然后继续在旧版本号上修改——回滚的本质是：**将历史版本的完整内容重新创建为一个新版本**。

`rollback_skill` 的执行逻辑：

1. 读取 `target_version` 的历史 Bundle（`versions/v{target_version}/`）
2. 验证 SKILL.md 存在
3. 将 Bundle 内容写回当前活跃位置（`skills/{name}/SKILL.md` 和附属文件）
4. 清理多余的旧 Bundle 文件
5. Registry 记录新的版本号，action 标记为 `rollback:v{target_version}`
6. 保存新版本的 Bundle 快照到 `versions/v{new_version}/`
7. 更新 manifest，新版本号成为当前版本

这意味着：
- 回滚后版本号只会增加，不会减少
- 被回滚的版本（出问题的版本）和回滚前的历史版本都完整保留
- 可以再次回滚到任意历史版本，包括回滚之前的"坏版本"
- 审计链显示"在 v7 回滚到 v3 的内容，创建了 v8"

> **提示**：回滚的内容与 target_version 完全一致（tree_sha256 相同），但版本号是新的。Agent 拉取时看到的是一个正常的新版本发布，不需要特殊的回滚逻辑。

### 为什么不直接删除或覆盖旧版本？

- **可审计性**：任何时候都能回答"v5 那个有问题的版本当时是什么样的"
- **可重放**：True Replay 可以在任意历史版本上重新执行验证
- **可追溯**：如果回滚后发现回滚本身有问题，可以再回滚到回滚之前的版本
- **分发一致性**：Agent 端只需要理解"新版本号=需要更新"，不需要处理版本回退的特殊逻辑

## Outbox 健康监控

`health()` 方法返回 Outbox 队列的健康状态：

- `backlog`：待处理事件数（不含 synced/cancelled）
- `oldest_age_seconds`：最老未完成事件的等待时间
- `dead_letter`：死信事件数（重试 8 次仍失败）
- `last_error`：最近的错误信息

管理操作：
- `retry(event_id, integration_id?)`：重置事件或特定 integration 的投递状态，重新投递
- `discard(event_id, integration_id?, actor, reason)`：取消事件或特定 integration 的投递，记录审计信息

## 代码入口

| 模块 | 路径 |
|------|------|
| SkillMutationService | [skills/mutations.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/mutations.py) |
| SkillHub（底层存储操作） | [skills/hub.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/hub.py) |
| Skill Bundle 模型 | [skills/bundle.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/bundle.py) |
| 同步适配器 | [integrations/skill_sync_adapters.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/skill_sync_adapters.py) |
| Skill ID 注册表 | [skills/registry.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/skills/registry.py) |

## 相关文档

- [Skill 体系](./03-skills)：Skill 的结构、版本状态、生命周期
- [进化闭环](./02-evolution-loop)：Publish 阶段在进化闭环中的位置
- [True Replay](./06-true-replay)：Candidate 通过验证后才会进入发布
- [Checklist 门禁](./07-checklist)：Checklist 是自动发布的前置门禁
- [架构总览](./01-architecture)：Skill Mutation Service 在系统架构中的位置
