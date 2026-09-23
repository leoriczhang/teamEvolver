# Agent 去注册化实现验收

代码交付覆盖阶段 0–6。当前工作目录保留可回滚的兼容版；阶段 6 是独立发布源码，不能在旧客户端仍有流量时直接替换现网。生产切换、观察周期、真实数据迁移和最终清理未执行。

## 交付内容

- [方案](./agent-deregistration-plan.md)：身份固定为租户 Key + 声明 user_id，Account 来自租户配置，不查全局用户成员关系。
- AgentPrincipal、Context 九个 v2 端点、Session v2、Hermes 三项接入配置、producer spool、认证 Skill pull/ETag 已实现。
- ReplayAdapterFactory、隔离分支、独立 Checklist 裁判、拟人化渐进反馈和客观指标比较已实现；客户 adapter 不接收 Checklist/裁判状态。
- 新控制台移除注册和 subject 映射，提供 Replay 预装 adapter 绑定；Python 源码操作限 Root。
- [阶段 5 迁移/恢复](./scripts/migrate_deregister.py) 与 [阶段 6 清理/恢复](./scripts/finalize_deregister.py)：停写证据、逐租户 scope、不可覆盖备份、记录数/checksum、重复执行/中断恢复和新写入保护。
- [独立发布构建器](./scripts/build_deregister_phase6.py)：生成 source、cleanup.patch 和 manifest.json；不访问生产数据。
- [隔离 HTTP 冒烟脚本](./scripts/smoke_deregister.py)：临时目录、随机空闲端口、本地存储、无外部模型/存储请求，结束后释放端口。

发布产物位于同级目录 `teamevolver-deregistration-releases-0917/phase6/`。`source/` 可单独安装或打包；`cleanup.patch` 用于查看兼容版到清理版的差异；`manifest.json` 和 `SHA256SUMS` 用于校验。重新生成时选择新的仓库外目录，避免模块布局测试扫描到重复源码。

## 验证

兼容版和独立清理版均执行完整后端测试、Python 编译、文档引用检查；清理版另做运行时未定义名称检查和前端生产构建。HTTP 冒烟覆盖健康检查、控制台静态入口、Context v2、Skill pull/304、匿名请求拒绝、租户覆盖拒绝、Session 实际存储和 v1 拒绝；清理版额外验证注册接口返回 404。最终结果：

| 检查 | 兼容版 | 独立清理版 |
|---|---|---|
| 后端完整测试 | 358 passed | 347 passed |
| Python 编译 | 通过 | 通过 |
| 文档引用 | 0 错误、0 警告 | 0 错误、0 警告 |
| 前端生产构建 | 通过 | 通过 |
| 隔离 HTTP 冒烟 | 通过，43975 端口已释放 | 通过，33125 端口已释放 |
| 运行时未定义名称 | — | 0 问题 |
| cleanup.patch dry-run | 可应用 | 42 个变更文件 |

清理版删除了 18 项旧注册/双读/push 专属测试，并增加 7 项最终契约测试，因此总数减少 11 项。兼容版保留原测试。

新增清理测试覆盖：dry-run 无写入、独立观察期、所有租户 preflight、备份损坏、Context 写失败不删注册表、阶段 5 后出现新注册时拒绝、重复执行、文件物理删除、恢复、恢复时拒绝覆盖新增数据。

测试使用本地存储和模拟 PG scope；未连接生产 PG/RLS 或真实 OpenViking。控制台已完成生产构建及 HTTP 静态入口验证，未进行浏览器逐屏人工验收。已有 Starlette TestClient 弃用警告和 Vite 大 chunk 提示不阻断验证。

## 运行环境限制

已实际启动 LocalHermes systemd worker。启动后的网络隔离自检发现 worker 与宿主的网络 namespace inode 相同，返回：

```text
PrivateNetwork isolation is unavailable on this host
```

worker 按 fail-closed 退出并清理资源，未绕过隔离，也未连接外部模型。因此本机未完成 LocalHermes 真实 Agent 执行；TurnBased、Mapped HTTP、DEAP、Legacy bridge 及 Replay engine 的隔离/连续会话/裁判门禁通过契约测试。生产仍须在支持网络 namespace 隔离的宿主，或客户隔离运行时上完成真实 Replay 验收。

## 残留项分类

清理版运行时代码不再导入或调用 agent_registry、注册函数、subject mapping、legacy identity、push delivery worker。

- `_register_agent_context_routes` 的两处匹配是 FastAPI 路由挂载，不是 Agent 实体注册。
- Session v2 normalizer 的两处 `integration_id` / `external_subject` 字符串仅用于丢弃客户端旧字段，不解析身份。
- 迁移/恢复/构建脚本中的旧字段、备份名和注册表名是离线迁移用途；负向测试验证这些接口已删除。
- docs 中 v1 Schema、旧发布链路保留为明确标记的历史资料。Replay transport v1 与 Agent 注册协议独立，继续用于适配客户 HTTP 运行时。
- OpenViking 的 `viking_agent_id` 是存储 API 参数；Hermes `producer_id` 是本地队列幂等分区，都不作为 Agent 注册身份。
- Skill → OpenViking 镜像保留内部 durable spool；Agent Skill 分发固定 pull，发布状态为 published，不伪造 synced。

## 生产发布门禁

1. 部署兼容版并升级全部客户端；监控 `/api/agent-protocol/metrics`，一个完整发布周期 legacy 增量为 0。
2. 逐租户确认 Skill token 隔离与 Replay 绑定，切 `tenant_user/pull` 后观察完整业务周期。
3. 停写，用阶段 5 工具 dry-run/apply；恢复业务后再稳定观察。
4. 再次停写，使用阶段 6 工具验证备份并清理数据，单独发布阶段 6 源码。
5. 回滚须同时回退应用和恢复数据；若观察期内有新增业务数据，脚本会拒绝旧快照直接覆盖，需要离线合并。

具体命令见 [升级与恢复指南](./docs/zh/guides/11-agent-deregistration.md)。测试结果不代替生产观察证据。
