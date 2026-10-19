# 成功经验同步指南

成功经验同步把 Session 分析产生的 `exemplary` 经验投影到 OpenViking 目录，供 Agent 检索和知识库挖掘读取。PostgreSQL 是权威来源，OpenViking 不可用不影响经验保存与 Skill 进化。同步不调用模型，也不触发 Skill 发布。接口见 [成功经验 API](../api/16-experience-sync.md)。

## 数据流

1. Session 分析把 `exemplary`（成功做法）与 `defect`（缺陷）经验写入 Session 记录。
2. Worker 的经验扫描（`experience.sync` 任务，操作 `sync`）按入库时间分页读取 Session，把经验写入 `skill_experiences` 与 `experience_occurrences`。
3. 经验正文（`id`、`skill_name`、`kind`、`experience_key`、`description`）变化时，`exemplary` 经验在 `experience_sync_outbox` 追加一条待投递记录。
4. 同步启用时，Worker 派发投递任务，把记录写入 OpenViking 并回读核对。

出现次数、Session ID、用户和分数只保存在 PostgreSQL，不参与投影摘要，因此同一经验被再次观察不会产生新的写入。

## 配置

同步配置位于租户 `skills` 模块配置的 `experience_sync` 对象，没有专用写入接口，由运维修改 `tenant_configs` 中 `module='skills'` 的行：

```json
{
  "experience_sync": {
    "enabled": true,
    "endpoint": "https://openviking.example.com",
    "account_id": "demo",
    "user_id": "team",
    "secret_ref": "secret:...",
    "target_directory": "viking://resources/agent_knowledge_workspace/input/proven_experiences",
    "interval_seconds": 30
  }
}
```

| 字段 | 说明 |
|------|------|
| `enabled` | 是否投递到 OpenViking，默认 false；关闭时扫描仍会执行 |
| `endpoint` | OpenViking 地址，HTTP(S)，不含内联凭证、query 或 fragment |
| `account_id`、`user_id` | 写入使用的 Account 与 User |
| `secret_ref` | OpenViking Key 的 Secret Ref，由 Worker 解密 |
| `target_directory` | 位于 `viking://resources/` 下的目标目录 |
| `interval_seconds` | 自动扫描间隔，默认 30，最小 5 |

配置修改后在下一轮 Worker 租户刷新时生效，不需要重启。

## 调度

Worker 每轮租户刷新（约 30 秒）执行 `experience_tick`：

- 距上次扫描超过 `interval_seconds` 时受理一次增量扫描，起点为上次水位回退 120 秒，覆盖长事务迟到的数据
- `enabled=true` 时为待投递记录派发投递任务

扫描与投递分别持有租约（`experience:scan`、`experience:deliver`），多副本 Worker 不会重复执行。扫描每页 100 条，进度写入检查点，任务中断后从检查点继续。

## OpenViking 写入与核对

每条经验写入目标目录下的固定 URI。写入请求使用 `wait=true`，完成后回读正文比较内容摘要；只有摘要一致且 OpenViking 报告语义与向量处理完成，记录才计为 `synced`。正文已写入但索引未完成时记为 `OV_INDEX_NOT_READY` 并重试。

每次请求超时 30 秒，响应超过 2 MiB 视为错误。可重试错误（网络错误、408、429、5xx、索引未就绪、内容不一致）最多尝试 3 次，之后进入 `dead_letter`；不可重试错误（如 401、403、路径类型冲突）直接进入 `dead_letter`。

源端删除或修改经验时，已写入 OpenViking 的旧文件保留，不自动撤回。修改 `target_directory` 后新记录写入新目录，旧目录文件不搬移。

## 手动操作

| 操作 | 接口 | 说明 |
|------|------|------|
| 立即同步 | `POST /api/experience-sync/trigger` | 受理一次增量扫描 |
| 导入存量 | `POST /api/experience-sync/import` | 不使用水位，全量扫描所有 Session |
| 查看状态 | `GET /api/experience-sync/status` | 各状态计数、重试计划、最近一次手动命令 |

控制台「经验库」页面为租户管理员提供同样的入口。

## 状态与排查

`GET /api/experience-sync/status` 返回：

| 字段 | 关注点 |
|------|--------|
| `counts.pending` | 持续增长说明投递未运行，检查 `enabled` 与 Worker |
| `counts.retry` / `retry.next_attempt_at` | 正在退避重试的记录与下次时间 |
| `counts.dead_letter` | 已放弃的记录，需修复后重新导入 |
| `index_pending` | OpenViking 索引处理慢 |
| `manual.state` / `manual.error_code` | 最近一次手动扫描的结果 |

常见错误码：

| 错误码 | 原因 |
|--------|------|
| `SYNC_DISABLED` | 手动触发时 `enabled=false` |
| `OV_HTTP_401` / `OV_HTTP_403` | Key 无效或无权写入目标目录 |
| `OV_NETWORK_ERROR` | Worker 无法连接 OpenViking |
| `OV_PATH_TYPE_CONFLICT` | 目标路径上存在同名目录或文件 |
| `OV_INDEX_NOT_READY` | 正文已写入，索引尚未完成 |
| `OV_CONTENT_MISMATCH` | 回读正文与投影不一致 |
| `experience_secret_unavailable` | `secret_ref` 无法解密，检查 `TEAMEVOLVER_SECRET_MASTER_KEY` |

## 相关文档

- [成功经验 API](../api/16-experience-sync.md)
- [知识库挖掘指南](./07-skill-miner.md)
- [Web 控制台用户指南](./03-console.md)
