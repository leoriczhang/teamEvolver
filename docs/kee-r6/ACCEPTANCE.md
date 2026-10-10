# KEE R6 本地验收与限制

日期：2026-10-08。实施前二开／上游版本见 [基线](BASELINE.md)。原始结果在 `results/`，本轮未部署 SIT／PRD，没有 push。结果不能扩大为生产效果或真实模型性能验收。

## 已通过的隔离验证

| 验证 | 环境与结果 | 证据 |
|---|---|---|
| 新方法治理测试 | 真实临时 PG、非超级用户／无 RLS 绕过；通用 OV HTTP 桩 | `results/kee-tests.log` |
| TE 全量 Python 回归 | 933 passed、66 skipped、4 failed；下表单列失败 | `results/python-regression.log` |
| 前端测试 | 21 文件、97 passed；含普通读者与写操作双击／JSON 请求 | `results/frontend-tests.log` |
| 前端生产构建 | 成功；保留既有大 chunk 提示 | `results/frontend-build.log` |
| HTTP 全链路 | 本地真实 TE + PG + 通用 OV HTTP 桩 + 独立模型子进程 HTTP 桩；冻结→编译→确认→批准→发布→读取→反馈，模型1次 | `results/http-smoke.json` |
| 迁移 | 非特权 PG 中导出／导入干跑、实际隔离导入、重复导入、摘要限定回滚；生产数据未操作 | 方法治理测试中的 migration 用例 |
| 离线 Wiki | 契约、来源证据、确定性页面、链接和摘要核验 | `results/offline-validation.json` |

治理测试覆盖审 A 发 B、来源读者范围收紧、跨租户、只读用户、索引等待不重写、响应丢失、基线竞争、取消迟到、租约恢复、回滚重新批准、静态三值检查、分页中文来源、真实 Skill 版本引用、模型调用预算和子进程凭据隔离。这里的故障是可控桩故障，不能据此证明真实网关、真实检索或生产备份恢复。

HTTP 测试中的来源为合成签收核验资料。负面条件、例外和完成标准保留在主方法页，反馈只写审计。没有调用任何实际业务工具，不报告业务动作或执行拦截成功。

## 既有失败与回归边界

TE 四项失败：

1. `test_dataset_collections.py::test_routes_accept_batch_and_preserve_historical_source_after_removal`：删除接口原预期200，实际400；本轮未修改该接口。
2. `test_sf_agent_adapter.py::test_envelope_only_prompt_title_comes_from_nested_query`：已有 nested query 标题预期不一致；本轮未修改该 Adapter。
3. `test_dreamcycle_integration.py::test_personal_source_sync_does_not_restart_evolve_or_dreamcycle[trio]`。
4. `test_dreamcycle_integration.py::test_team_target_sync_restarts_evolve_gracefully[trio]`：后两项原测试的 Trio backend 调用已有 asyncio.to_thread 路径，缺 running event loop。

最初 pytest 因当前入口引用的 `team_skills.evolution.runtime` 已在历史版本删除而出现6项收集错误；恢复唯一历史实现并适配当前契约后能够完整收集。该修复不构成第二套 Skill Evolution 或 Judge。

OV 相关宽回归100 passed／16 failed。扩展注册、原生认证及 ACL 锁的限定回归94 passed。另一次含 temp scope 的回归98 passed／6 failed：4项期待旧 PermissionError，实际为 PermissionDeniedError；2项 FakeAGFS 缺少 tree_directory。还原原注册表复跑 temp listing 用例，失败相同。详见 `results/ov-temp-scope.log`、`results/ov-temp-baseline-failure.log`。16项集中在 Account Auth 夹具缺少 `runtime_config_manager`；还原实施前扩展注册表对一个失败用例复跑，失败相同，见 `results/ov-baseline-failure.log`。本轮注册、原生认证与 ACL 的独立回归另存 `results/ov-focused.log`。没有运行 OV 整仓全量测试，不能报告全量兼容通过。

## 性能测量

| 合成来源数 | 导出页面 | 离线导出秒 | 单方法变化秒 | 变化页面 | Python peak bytes |
|---|---:|---:|---:|---:|---:|
| 100 | 202 | 0.0201 | 0.0027 | 2 | 889949 |
| 1000 | 2002 | 0.1978 | 0.0302 | 2 | 8823694 |
| 10000 | 20002 | 1.9524 | 0.3544 | 2 | 88776375 |

以上为单机 Python 离线结构导出与变化对比，原始值以 `results/scale.json` 为准。不是实际来源冻结、PG 任务吞吐、真实模型或 OV 索引性能。正常来源集合上限1000文件；10000的离线测量没有绕过线上预算。真实模型压测 `not_run`，未获明确额度。

## 阻塞／未执行

- 真实模型小样本：配置模型名称可读，但当前环境没有有效模型凭据，`blocked/MODEL_CREDENTIAL_MISSING`，模型请求次数0。见 `results/real-model-smoke.json`。
- 新版完整浏览器流程／截图：浏览器控制通道连续超时；内置浏览器再次报 ERR_BLOCKED_BY_CLIENT；旧缓存页面曾发现 JSON Content-Type 缺失并已修复、单元测试覆盖。新版本完整页面验收和截图 `blocked`，不能拿前端测试替代。
- 真实 OV 写入、索引确认、私有原件／反链检索拒绝、撤回后索引清理、普通 Agent 阅读：`not_run`。当前通用 OV HTTP 桩通过只证明本地请求／恢复契约。
- 外部 True Replay Runtime 对本次知识 Candidate 的实际调用、真实模型效果与业务效果：`not_run`；已有 True Replay 测试参加 TE 回归。
- 生产迁移、真实备份恢复、集群模型总配额及多副本发布故障演练：`not_run`。

## 尚待完善的设计能力

三类视角目前只重排菜单；复杂澄清分派、完整语义 Schema 检查、自动依赖补编译尚待实施。方法编制采用同屏结构 JSON、证据与审核意见。历史事实导入保留原记录，不自动编造 ProcedurePack。

Memory 内部 Port 是1 MiB输入／最多128页／两次模型尝试的受限正文模式，不执行工具型 Skill。通用知识库挖掘 Compile 仍保留原 OV 兼容执行器。以上差距需在下一轮明确解决，不能将本轮实现描述为全部 R6 蓝图完成。
