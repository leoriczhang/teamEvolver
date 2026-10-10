# 接驳 Wiki Ontology 真实联调记录（2026-09-23）

结论：前置流程已通过，完整闭环未通过。两次真实 Compile 都在抽取子任务的模型流等待阶段停留，
已主动取消并核对远端终态；product_agent 正式 generation 仍为 0，未发布本体。

## 环境与范围

- TE 基线 ad04bba，加本地未提交修复；本地 HTTP 服务 127.0.0.1:52319，独立 PostgreSQL。
- 使用真实 TE 路由、Operations、CompileFlow 与持久化 worker；认证为隔离联调入口，不代表完整控制台登录验收。
- 连接真实 SIT OV 的 product_agent，源目录为 `viking://resources/wiki-0920-3/PROCEDURE/operations/handover/`。
- 工作区为 `viking://resources/te_ontology_compile/local-handover-20260923` 下的任务目录。
- 本轮未修改 OV 代码、未部署、未 commit/push；保留已有 .gitignore 和 OV 环境文件改动。
- 用户已授权本次审批发布，但没有有效模型草稿，未执行后续审批发布。

## 已验证与修复

| 步骤 | 实际结果 |
|---|---|
| 目录发现与冻结 | 3 份 Markdown，6,444 字节，全部冻结成功，无来源缺口 |
| 工作区目录复用 | 原生 mkdir 409 经同身份 stat 确认类型后正常继续 |
| 账户 ACL | 用户开启 product_agent ACL 后回读 true；普通身份无法读取或检索任务输入 |
| ACL 初始化 | 新目录可出现“context record 尚未就绪”；TE 对精确错误有界重试，其他错误不放行 |
| 输入暂存 | 改为 wait=false；3 份原文 raw 回读一致，未把索引状态声称为完成 |
| 原生 Compile | 两次均受理并实际发起模型调用；未使用模型桩代替 |
| TE 重启 | 第一轮运行中重启，恢复同一 Compile ID，无重复提交 |
| 异步取消 | 修复提前报告 cancelled；第二轮真实观察到 cancelling，随后恢复循环核对终态 |

ACL 初始化原生错误已独立复现：`INVALID_ARGUMENT: ACL target has no context record; index it first`。
TE 只将该精确消息映射为 `COMPILE_ACL_TARGET_NOT_READY`，最多 6 次尝试，退避总计 11.5 秒。
任一前置权限检查失败时，不写来源正文、不提交 Compile。

## 模型阻塞证据

第一轮：`cmp_7381a8c0b37e4131b08821d27dff023b`，追踪 `272cbace319c7a9d84b4b59a9edb5356`。
子任务 00e1935d 在北京时间 17:35:46 发起模型调用，5.61 秒收到首片段，至 17:57:30 主动取消时
仍没有完整响应，持续约 21 分 45 秒。主任务等待 wait_subagents，不能解释为 TE 排队卡死。

第二轮：`cmp_0dc621ffa1dd4701aa403765dbb5c15b`，追踪 `b7b33228ce81213f0e8805c8fc0886e9`。
子任务在 18:01:37 发起模型调用，4.56 秒收到首片段，至 18:07:13 取消时仍未结束，持续约 5 分 36 秒。
两个远端任务均已确认 cancelled。另一个本地准备任务在 staging 阶段取消，没有提交 Compile。

OV 日志模型为 `tencentcloud/glm-5.3-flash`，追踪归一化名称为 glm-5.3-flash。
两轮已完成父模型调用分别记录 12,921、11,684 Token；未结束子调用的用量不可用，不能算作零或总成本。
追踪中的 environment 标签来自遥测配置，不能据此把本次 SIT 调用描述为生产验收。

尚不能仅凭这些证据区分模型生成过长、流结束事件缺失或网关行为。下一步需对上述调用核对网关请求、
流结束标记、输出/推理预算及整次调用期限，再恢复真实抽取；不宜只加长 TE 等待时间。

## 验证结果与未执行项

- 相关 Compile／日志测试：76 通过。
- 全量 Python 回归：859 通过、64 跳过、5 个既有失败，与此前失败项一致。
- Python 编译、相关 Ruff 检查和 git diff 检查通过；没有前端改动。
- 文档检查仍有一个既有引用错误：docs/en/api/06-skill-sync.md 引用了已不存在的 teamEvolver/skills/mutations.py。
- 未执行：Schema 确认、候选语义审核、Prepare、Approve、Commit、已发布 HTTP/MCP 查询与证据解释。

机器可读证据与来源 revision/digest 见 [验证记录](handover-real-validation-20260923.json)。
原始日志、冻结正文、追踪与本地数据库保留在私有目录 `/tmp/te-ontology-handover-20260923`，不把凭据或业务原文归档入仓库。
