# Skill pull API

`GET /sync/skills?user_id=alice` 使用 `Authorization: Bearer tevt_...`。
服务端先解析主体，再读取该租户共享的 bundle；Skill 不按用户过滤。
匿名、失效凭证和非法 user_id 被拒绝。客户端声明的租户不能改变 bundle 来源。

以下保留完整的 Skill Sync（拉取 + 推送）参考，供兼容窗口内使用。

## 1. API 实现介绍

Skill Sync 用于将 teamEvolver 发布/回滚的团队 Skill 实时同步到已注册的 Agent 运行时。支持两种模式：

1. **拉取模式（Pull）**：Agent 通过 `GET /internal/agents/context/skills` 主动拉取技能清单，或通过 `GET /sync/skills` 获取完整 bundle 快照。
2. **推送模式（Push）**：Agent 注册时提供 `skill_sync_url`，teamEvolver 在 Skill 发布/回滚/删除时向该 URL 发送 webhook 回调，并要求 Agent 返回版本验证确认。

推送模式支持幂等投递（`Idempotency-Key` header）、失败重试和确认验证。Agent 必须在响应中返回每个 Skill 的版本号和哈希校验结果，teamEvolver 验证匹配后才标记为同步成功。

代码实现：`teamEvolver/integrations/skill_sync_adapters.py`
Skill 变更投递：`teamEvolver/skills/mutations.py`（SkillMutationService）
轻量快照端点：`teamEvolver/proxy/skills_admin.py:511` (`/sync/skills`)

## 2. 接口和参数说明

### 2.1 拉取模式：获取技能清单

```
GET /internal/agents/context/skills?external_subject=<user>&scope=team&integration_id=<agent_id>
Authorization: Bearer <租户机器凭证 tevt_<random>>
```

`integration_id` 为必填，必须是当前租户下已注册且状态为 `active` 的 Agent；认证只接受租户机器凭证（`tevt_`）。

详细接口文档见 [Context Workspace API](./04-context-workspace.md) 中的 `GET /internal/agents/context/skills` 部分。

此接口返回每个 Skill 的 `name`、`qualified_skill_id`、`context_ref`，可用于后续 `read` 获取完整内容。

### 2.2 拉取模式：完整 Bundle 快照（轻量 Agent）

```
GET /sync/skills?user_id=<user>
Authorization: Bearer <租户机器凭证 tevt_<random>>
```

匿名、失效凭证和非法 user_id 会被拒绝（如 Hermes 等轻量部署通常运行在内网环境）。返回该租户所有团队 Skill 的完整文件 bundle（base64 编码）。

**响应字段：**

| 字段 | 类型 | 说明 |
|------|------|------|
| `status` | string | `ok` 或 `error` |
| `source` | string | `shared`（从 OpenViking 拉取）或 `local`（本地 skills 目录） |
| `skills` | array | Skill 列表 |
| `skills[].name` | string | Skill 名称 |
| `skills[].version` | integer | 版本号 |
| `skills[].skill_id` | string | Skill ID |
| `skills[].files` | array | 文件列表 |
| `skills[].files[].path` | string | 相对路径（如 `SKILL.md`） |
| `skills[].files[].content_b64` | string | 文件内容（base64 编码） |
| `total` | integer | Skill 总数 |
| `error` | string | 错误信息（status=error 时） |

代码：`teamEvolver/proxy/skills_admin.py:304` (`_sync_bundle_payload`)

### 2.3 推送模式：Webhook 回调

当 Skill 发布、回滚或删除时，teamEvolver 向 Agent 注册的 `skill_sync_url` 发送 POST 请求。

**请求方向：**

```
teamEvolver --> POST https://<agent-skill-sync-url>
```

**请求头：**

| Header | 值 |
|--------|-----|
| `Content-Type` | `application/json` |
| `Idempotency-Key` | `<event_id>:<agent_id>`（幂等键） |
| `Authorization` | `Bearer <skill-sync-api-key>`（如配置了 auth_profile） |

Skill Sync API Key 通过环境变量配置：`TEAMEVOLVER_AGENT_<AUTH_PROFILE>_SKILL_SYNC_API_KEY`（auth_profile 转为大写下划线格式）。早期 Pi Agent 版本兼容使用 `validation_agentshub_api_key` 配置。

代码：`teamEvolver/integrations/skill_sync_adapters.py:_sync_api_key`

**请求体（`teamevolver.skill-changed.v1`）：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `schema_version` | string | 是 | `teamevolver.skill-changed.v1` |
| `protocol_version` | string | 是 | `1.0` |
| `event_id` | string | 是 | 事件唯一 ID（`skill_evt_<hash>`） |
| `action` | string | 是 | 操作类型：`publish`（发布）、`update`（更新）、`rollback`（回滚）、`delete`（删除） |
| `job_id` | string | 是 | 变更任务 ID（mutation_id） |
| `skills` | array | 是 | 变更的 Skill 列表 |
| `skills[].name` | string | 是 | Skill 名称 |
| `skills[].version` | integer | 是 | 新版本号 |
| `skills[].sha256` | string | 是 | SKILL.md 内容 SHA-256 |
| `skills[].tree_sha256` | string | 否 | 完整文件树 SHA-256 |
| `skills[].action` | string | 否 | 同顶层 action |
| `tenant_ids` | array[string] | 是 | 目标租户 ID 列表（多租户过滤） |
| `expected_skills` | array | 否 | 同 skills（遗留兼容字段） |

**多租户过滤：** 如果 Agent 注册时在 `metadata.tenant_id` 中指定了租户 ID，teamEvolver 仅在该租户的 Skill 变更时向其发送回调。

代码：`teamEvolver/integrations/skill_sync_adapters.py:_target_tenant_ids`

### 2.4 推送确认响应

Agent 收到 webhook 后，处理完 Skill 更新，必须返回确认响应：

**响应字段：**

| 字段 | 类型 | 必填 | 说明 |
|------|------|------|------|
| `ok` | boolean | 是 | 必须为 `true` 表示接收成功 |
| `results` | object | 是 | 按租户 ID 分组的验证结果 |
| `results.<tenant_id>.verification` | object | 是 | 验证信息 |
| `results.<tenant_id>.verification.skills` | array | 是 | 每个 Skill 的验证结果 |
| `results.<tenant_id>.verification.skills[].name` | string | 是 | Skill 名称 |
| `results.<tenant_id>.verification.skills[].matched` | boolean | 是 | 名称是否匹配 |
| `results.<tenant_id>.verification.skills[].actual_version` | integer | action=publish 时必填 | 本地实际版本号 |
| `results.<tenant_id>.verification.skills[].actual_sha256` | string | action=publish 时必填 | 本地 SKILL.md SHA-256 |
| `results.<tenant_id>.verification.skills[].actual_tree_sha256` | string | 否 | 本地文件树 SHA-256 |
| `results.<tenant_id>.verification.skills[].removed` | boolean | action=delete 时必填 | 是否已删除 |

teamEvolver 会验证：
1. `ok` 必须为 `true`；
2. `results` 必须包含每个目标租户的验证结果；
3. publish 时：`matched=true`、`actual_version` 等于期望版本、`actual_sha256` 匹配；
4. delete 时：`matched=true` 且 `removed=true`。

验证失败会标记为同步失败并进入重试队列。

代码：`teamEvolver/integrations/skill_sync_adapters.py:_ack_matches`

### 2.5 重试机制

- 同步失败的事件会进入 outbox 队列，定期重试；
- 基于 `next_retry_at` 时间戳判断是否到期重试；
- Agent 被禁用或取消 `skill.sync.v1` capability 时，待投递事件标记为 `cancelled`；
- Agent 注销时，相关事件标记为 `cancelled`；
- 重试次数耗尽后，事件进入终态 `dead_letter`，可通过 `SkillMutationService.reconcile()` 或管理接口修复；
- 重试与丢弃支持按 integration 粒度操作（`integration_id`）：丢弃会在投递记录上写入 `cancelled_at`/`cancelled_by`/`cancel_reason`，并在事件的 `audit` 列表追加流水。

代码：`teamEvolver/integrations/skill_sync_adapters.py:_delivery_due`

## 3. 使用示例

### 拉取完整 Bundle 快照（Hermes 模式）

```bash
curl --fail "$TEAMEVOLVER_URL/sync/skills?user_id=alice"   -H "Authorization: Bearer $TEAMEVOLVER_TENANT_TOKEN"
```

返回 `skills` 清单及文件、版本、SHA-256。响应包含 ETag；
后续请求发送 `If-None-Match`，未变化返回 304。
Hermes 缓存 `{name: {version, sha256, tree_sha256}}`，未变化的 Skill 不删除、不重写。
拉取失败保留已有缓存；token/user/服务地址变化后重新取得对应缓存。

发布统一经 `SkillMutationService`。`delivery_mode=pull` 下状态为 `published`，
不创建 delivery outbox，不把无人接收标成 `synced`。
从 push 切换时，旧待投递记录标为 `cancelled`，原因为 `delivery_mode_changed_to_pull`。
兼容窗口仍可回到 push；最终清理版本删除 push worker 与管理接口。

Hermes hook 是 `pre_llm_call`，最小请求间隔为 15 秒。
交付语义是**满足间隔的下一次模型调用前可见**，不是发布后 15 秒主动推送。
安装见 [Hermes](../agent-integrations/03-hermes.md)；Context Skill refs 见 [Context API](./04-context-workspace.md)。
实现：[同步客户端](../../../teamEvolver/integrations/hermes_skill_sync/sync_skills.py)、
[MutationService](../../../team_skills/library/mutations.py)。
