# Skill 拉取 API

Skill 分发统一使用拉取模式。teamEvolver 不维护 Agent 注册回调或 Skill Sync
webhook outbox；发布、回滚和删除由 `SkillMutationService` 写入版本与
tombstone，客户端通过快照接口获取当前状态。

## 完整 Bundle 快照

```http
GET /sync/skills?user_id=alice
Authorization: Bearer tevt_<random>
```

服务端从租户凭证解析 Account，并校验 `user_id`。客户端声明的租户不能改变
数据范围。

响应包含：

| 字段 | 说明 |
|------|------|
| `status` | `ok` 或 `error` |
| `skills` | 当前租户的 Skill Bundle 列表 |
| `skills[].name` | Skill 名称 |
| `skills[].version` | 已发布版本 |
| `skills[].files` | Bundle 文件及 base64 内容 |
| `skills[].tombstone` | 删除标记（存在时客户端应移除本地版本） |
| `total` | Skill 总数 |

## Context Skill 清单

```http
GET /internal/agents/context/skills?user_id=alice&scope=team
Authorization: Bearer tevt_<random>
```

该接口返回 `name`、`qualified_skill_id` 和 `context_ref`，随后可通过 Context
`read` 获取内容。`integration_id` 可作为来源标签，但不需要注册记录。

## 客户端同步规则

1. 以 `name + version` 判断是否需要更新。
2. 原子替换完整 Bundle，不拼接单个文件。
3. 保留最近一次成功快照；拉取失败时继续使用该快照。
4. 遇到 tombstone 时删除对应本地 Bundle。
5. 不从响应或日志中持久化租户 Key。
