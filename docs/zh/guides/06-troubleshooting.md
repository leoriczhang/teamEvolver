# 故障排查

按“进程 → 数据库 → 任务 → 业务模块”的顺序排查。多数问题可以通过 `/readyz`、`GET /api/jobs/{job_id}` 和服务日志定位。

## 第一步：确认进程与日志

```bash
teamEvolver status
curl -s http://127.0.0.1:52010/readyz
curl -s http://127.0.0.1:52011/readyz
```

| 端口 | 角色 | 说明 |
|------|------|------|
| 52010 | `api` | 控制台与 HTTP 接口；`/readyz` 返回 503 表示未就绪或正在停机 |
| 52011 | `worker` | 仅健康检查 |

日志默认位于 `~/.teamEvolver/teamEvolver.log`，容器中可用 `TEAMEVOLVER_LOG_DIR` 指定目录，同时输出到标准错误流。后台模式未指定 `--log-file` 时进程输出写入 `/tmp/te_runtime.log`。日志字段与降级行为见 [服务日志](./15-service-logging.md)。

## 启动失败

| 输出 | 原因与处理 |
|------|------------|
| `runtime requires PostgreSQL; configure TEAMEVOLVER_PG_DSN` | 未配置数据库连接，设置 `TEAMEVOLVER_PG_DSN` 或 `TEAMEVOLVER_PG_HOST` 等变量 |
| `all runtime is restricted to local development` | `--role all` 只允许 `TEAMEVOLVER_ENV=local`；生产分别启动 `api` 与 `worker` |
| `teamEvolver is already running (PID=...)` | 后台模式检测到存活的 PID；先 `teamEvolver stop` |
| `teamevolver_config single row is missing; run schema apply` | 数据库未初始化，执行 `teamEvolver schema apply` |
| `Invalid logging option: ...` | 日志配置非法，见 [服务日志](./15-service-logging.md#配置) |
| 加载 YAML 报字段错误 | 启动配置只接受平铺的当前字段名，旧的分节或点路径写法会被拒绝，见 [配置参考](./01-configuration.md) |

`teamEvolver status` 显示运行中但进程已退出时，PID 文件是残留的；`teamEvolver stop` 会清理它。

## 数据库

- `/readyz` 持续 503：检查数据库地址、端口、密码 URL 编码与网络
- 租户数据互相不可见或全部为空：确认应用角色不是 SUPERUSER 或 BYPASSRLS，且 Schema 由 `teamEvolver schema apply` 创建
- 连接数耗尽：按 `(storage_pg_pool_max + storage_pg_control_pool_max) × 实例数` 预留连接

## 任务一直不完成

所有长任务都以 `worker_tasks` 记录，受理接口返回 `job_id`：

```bash
curl -s -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" -H 'X-Tenant-Id: demo' \
  http://127.0.0.1:52010/api/jobs/<job_id>
```

| 状态 | 处理 |
|------|------|
| `queued` 长时间不变 | Worker 未运行，或租户状态不是 `active` |
| `retry_wait` | 按退避等待重试，`error_code` 为上次失败原因 |
| `waiting_external` | 等待上游（如 OpenViking Compile），检查 `upstream_task_id` 对应的上游任务 |
| `reconciling` | 结果未知，正在与上游对账；不要手工重复提交 |
| `failed` | 查看 `error_code`；修复后 `POST /api/jobs/{job_id}/retry` |

Worker 进程退出时，未完成任务的租约到期后由其他 Worker 接管。状态说明见 [健康与任务 API](../api/07-health-status.md)。

## Agent 接入

| 现象 | 处理 |
|------|------|
| `401 TENANT_TOKEN_REQUIRED` | Agent 未携带有效租户 Key；在「成员与接入」轮换后更新 Agent 配置 |
| 上报返回 202 但控制台看不到 Session | 用返回的 `job_id` 查询任务；`session.analyze` 需要可用模型 |
| `GET /sync/skills` 一直 304 | 没有新版本；发布后 ETag 才会变化 |
| 拉取不到某个 Skill | 确认 Skill 已发布且未删除；拉取按 `user_id` 返回可见 Skill |

协议细节见 [Agent 协议](../agent-integrations/06-protocol-v2.md)。

## 进化没有产生 Candidate

1. 检查 `evolution_trigger_mode`；手动模式需点击控制台「立即进化」或调用 `POST /api/skill-evolution/trigger`，自动模式还需等待配置的间隔。
2. 检查 Skill 分组是否满足 `min_group_sessions` 与 `min_group_users`（默认都为 2），见 [Prompt Studio 指南](./08-prompt-studio.md#设置项)。
3. 检查 Session 是否被价值分类判为 chitchat：「运行总览 → 过滤审计」。
4. 检查模型配置：「模型配置」页面的连接测试。

## True Replay

| 现象 | 处理 |
|------|------|
| 验证结果 `inconclusive` | 缺少效率指标（轮次、工具调用、Token），检查 Replay 适配器返回的字段 |
| 两个分支结果相同 | 确认适配器为每个分支使用独立工作区 |
| 验证任务失败 | 查看 `skill.validate` 任务的 `error_code` 与 Replay 适配器连通性 |

契约见 [Replay 分支 API](../api/05-replay-branch.md)。

## OpenViking

| 现象 | 处理 |
|------|------|
| 资产页、知识库挖掘显示未配置 | 在「运行状态 → OpenViking 部署」配置 Endpoint、Account 与 Root Key |
| 读写返回 503 | OpenViking 不可达，检查 Endpoint 与网络 |
| 写入团队资源返回 403 | 只有租户管理员可写团队作用域 |
| 内置终端不可用 | 服务器未安装 OpenViking CLI，或 `OPENVIKING_CLI_BIN` 路径错误 |

## 控制台

| 现象 | 处理 |
|------|------|
| `503 console assets are not built` | 前端未构建，执行 `npm --prefix web-ui run build` |
| 登录后无菜单 | 账号没有租户成员关系，联系租户管理员添加 |
| 登录后很快退出 | Session Cookie 过期或被撤销；反向代理需保留 Cookie 且使用同一域名 |
| 切换租户后数据未变 | 页面会整体刷新；若仍是旧数据，清理浏览器缓存后重试 |

## 链路观测与数据源

- 链路观测没有数据：见 [链路观测指南](./04-observability.md#故障排查)
- 数据源测试或拉取失败：见 [数据源接入指南](./10-datasource-onboarding.md#常见错误)
- 成功经验未同步：见 [成功经验同步指南](./16-successful-experience-sync.md#状态与排查)

## 收集信息

提交问题时附带：

- `teamEvolver config` 输出（已脱敏）
- 相关 `job_id` 与 `GET /api/jobs/{job_id}` 结果
- 请求的 `X-Request-ID` 与对应时间段的日志
- `GET /api/logging/status` 输出
