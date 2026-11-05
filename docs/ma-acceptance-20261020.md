# MA Skill 自进化接入验收

日期：2026-10-20。实施与真实云端验收完成；质量获证与执行失败分别记录。

## 已接入能力

`team_ma` 是独立业务模块；MA 客户端位于平台包 `teamEvolver.managed_agents`。控制台「设置与接入 → MA 接入」支持连接配置、资源发现、采集、成员关联、版本历史和重试。接口长任务使用 Worker 受理，连接与资源账本使用 PostgreSQL、revision CAS 与 FORCE RLS，凭证存为加密 Secret Ref。

正式链路为：发现完整 Agent 图和 Skill Bundle → 采集真实 Session → normalize / split / analyze / trace_analyze / archive → Skill Evolution 生成 Candidate 与同源 Test Dataset → 隔离 MA True Replay → 管理员 Candidate Review / Publish → 已发布 Skill pull → MA Skill / 成员 / Environment 完整 release。新 Session 固定最后完整 release，既有 Session 保持历史快照。

无需固定 MA 角色名、模型或 CodingVideo 路径。通用模板为 `examples/managed-agents/generic.json`；[接入说明](./zh/agent-integrations/07-managed-agents.md)列出了配置与执行命令。范围是火山方舟 MA API。可写工具、Vault 或长期 Memory 无法隔离时，仍可采集，回放和自动应用保持阻塞。

## CodingVideo 真实接入

| 项目 | 实际结果 |
| --- | --- |
| 租户 / 连接 | `codingvideo-ma` / `codingvideo` |
| 发现与导入 | 3 个 Agent、15 个自定义 Skill；完整 Bundle 已导入 |
| Evidence | 3 个真实主 Session 已聚合为 3 个正式 Session（5 / 3 / 3 轮），根与子 Agent trace 保持在同一会话；历史 7 个增量范围及 35 个阶段结果继续保留为审计 Evidence |
| 自动 Candidate | `20261019233346-codingvideo-production-core-7f3fcfea`，当前 r3，修订 `codingvideo-production-core` |
| 同源 Dataset | `evolution-73f3e0ca89fac79966a77933529eda6f` |
| 发布状态 | Candidate 未 Publish，生产 Skill 不变 |
| 已应用 release | `ma-release:9f803b39c9134df4dc045f5bca57d9e3d43bbfd4` |
| 运行环境 | `env-20261019235211-1xw36`，实际 Node 22.23.3 / Canvas / FFmpeg，ready 证明与树哈希已下载核验 |
| 部署兼容 | CodingVideo 连续两次 `deploy_ma apply` 保持同一完整 release、15 Skill 和 3 Agent v7 |

Candidate 只改 `SKILL.md`，配套 runtime/config 文件完整保留。真实发现与回放修正了 MA 标签对象、Files 的 JSON null、主线程初始 idle、子线程结束后不再发送主 SSE 事件、模型覆写权限、setup 依赖时序及 ETag 随连接 revision 的失效问题。独立裁判关闭 Ark 隐式长推理，使用有界调用；自然用户反馈可以在立意/分镜关卡选择合适方案。

## 真实发布与回滚

独立自有测试 MA：租户 `ma-protocol-acceptance`、连接 `owned-note-ma`，角色为 editor / writer，与 CodingVideo 无关。

| 操作 | 团队版本 | 远端 Skill 版本 | writer / editor 版本 | 新 Session 回读 |
| --- | --- | --- | --- | --- |
| 初始导入 | 1 | 1 | 1 / 1 | 一致 |
| 正式管理员 Publish → pull | 2 | 2 | 2 / 2 | 一致 |
| 正式管理员 Rollback → pull | 3 | 复用 1 | 3 / 3 | 一致，恢复原 Bundle 树哈希 |

发布、回滚均走正式 API，无直接改库替代发布。原树哈希为 `358aa7037382f10ea8597ed3b21fc644ec91cc2878006a0f9e196b043681eedb`。

通用 MA 的真实回放还发现：原生委派事件不能只作为助手文本丢弃。现保留 `session.thread_created`、`agent.thread_message_sent`、`agent.thread_message_received` 的方向、线程和远端身份，并归档原始事件；没有把委派伪造为工具调用。相同 Bundle 的协议回放只验证执行与证据通道，不构成“候选更优”的证明。

协议任务 `skill:8edf82176046687f86dddaf9563b50ba3b886cf46c3d82949cb499884ac3cda0` 已完成：Baseline / Candidate 分别创建独立临时图和 Session，均在一轮完成精确回复与原生委派条件；实际 total_tokens 为 30,292 / 30,287、工具调用均为 0，委派事件单独保存。独立 judge 全部条件通过，结果为 accept。两分支内容相同，5 token 差异是随机运行差异，不是 Skill 改进证据；6 次协议回放分支的 Skill / Agent / Session / 消息账本均已清理。

## 视频业务证据

| 成片 | 真实规格 | 独立结论 |
| --- | --- | --- |
| 数字信号脉冲 | 1280×720、24fps、192 帧、8.000 秒、H.264 + AAC；完整解码及三张采样帧通过 | 视觉通过；旁白主观听辨/“抵达”同步未验证，保持 revise |
| 缓存命中 | 同上；256,925 字节，数据库全片标注访问 0 次 | 独立 reviewer accept；读取真实运动帧与 ASR/静音/源句证据，允许用户初版确认 |

缓存成片 SHA-256：`2e2e8c2c1322b6bc8218a2d46aeff5876da3f19278329dc6627a9aab7933c2ac`。Vosk 与 faster-whisper base 两个模型家族支持旁白逐字一致；whisper small 有一个同音字分歧，未报告添漏字。第二句有声起点 4.157 秒晚于 4.0 秒 HIT。评审环境没有人耳实听能力，accept 为图像与 ASR 证据支持，非人工听审；视频发布仍保留用户初版关卡。

业务 Session、视频、QA、ASR 原始输出与评审已保存到 `codingvideo-ma` 的 PostgreSQL ObjectStore。主要对象：

- `ma/acceptance/business/sesn-20261019230133-t4kwv`
- `ma/acceptance/business/sesn-20261019232501-r37rf`
- `ma/acceptance/runtime-proof`
- `ma/acceptance/cache-review`
- `ma/acceptance/owned-sync`
- `ma/acceptance/file-20261020083134-547tm`（缓存 MP4）
- `ma/acceptance/file-20261020083201-p4dhn`（缓存 QA）
- `ma/acceptance/file-20261020085325-7j5cf`（ASR 证据包）

这些对象引用供管理员在租户环境检索，不含凭据或签名下载 URL。

## 回放与清理

CodingVideo Candidate 验证任务 `validate:61f46e96aa9048c4a83c0f2a73849fee` 已于 UTC 01:59:16 正常落库结束。两个双分支用例均超出各自 1800 秒，结果为 TIMEOUT / inconclusive；Candidate r3 回到 `open`，`recommended_publish=false`。所有 4 个分支均完成实际 runtime 探测、首轮交互和自然反馈，未在时限内完成后续要求。

| Case | Baseline 已完成轮次 tokens / 工具数 | Candidate 已完成轮次 tokens / 工具数 | 结果 |
| --- | --- | --- | --- |
| 二极管 9 秒 | 208,397 / 12 | 3,060,089 / 31 | inconclusive |
| 贝叶斯 60 秒 | 1,582,215 / 38 | 1,247,321 / 23 | inconclusive |

表内数值只统计成功返回的观察轮次。超时中的最新事件、usage 与工具过程另存为 `ma/acceptance/replay-final/<remote-session-id>`，不得把未完成轮次作为完成指标。完整候选判定保存在 `ma/acceptance/candidate-final`。

专门成片回放任务 `skill:429291690dbe7a2050084d4f4add4acb156fa87b4f7f853d2903abff236d01de` 于 UTC 02:11:49 正常落库结束：Baseline 完成 1 轮（1,625,101 tokens / 27 工具），Candidate 完成 2 轮（1,416,505 tokens / 45 工具），后续制作均超出 1800 秒。实际分镜文件可读取，未产生本次回放要求的最终 MP4，结论为 TIMEOUT / inconclusive；没有沿用业务成片来冒充回放产物。完整结果位于 `ma/acceptance/media-final`。

验收临时并发已恢复为 1（Replay 配置 revision 13），保留 1800 秒与 8 次交互。两个租户均无待清理的 Replay 资源：CodingVideo 累计清理 20 Skill / 44 Agent / 18 Session / 35 消息意图，自有通用 MA 清理 6 Skill / 6 Agent / 6 Session / 9 消息意图。两笔明确 HTTP 拒绝的 Session 创建保持 failed；没有远端 ID，不属于残留资源。

最终连接回读为 `ready`、revision 9、detail 为空；正式 reconcile `ma:cf9b7f394bb2405c827a1d4fe5d96f3b` 一次成功。已停止验收增加的 14 个空闲 Worker；另一个正在执行正常自动 Evolution，保留任务完成，默认 API / Worker 继续运行。最终状态证明存于 `ma/acceptance/final-connection-check`。

13 个自有运行时探测 Session、8 个探测 Environment、1 个探测 Agent 已精确删除，删除前已归档事件。业务 Session、历史已应用 Environment、自有通用测试 MA 及共享隔离 Replay Environment/Vault 保留。已删除 Session 的事件账本随 Session 退役；消息没有独立 DELETE API。

两笔早期 66 KiB setup Environment 创建返回 500，按精确名称查询无远端匹配；仍保留 `outcome_unknown`，不盲目重试，不标为清理成功。去重后的约 33 KiB setup 创建与实际 ready 核验成功。

## 自动检查

- 最终完整后端 482 pytest 通过，使用真实 PostgreSQL；另 66 项 MA / PG / 架构检查和 22 项 Replay 定向检查通过。
- 前端 107 测试通过，TypeScript + Vite 生产构建通过。
- ruff 通过；文档 strict 检查 0 错误、0 警告。
- wheel 构建通过，包含 `team_ma`、MA 客户端、Replay Adapter、017 SQL 与控制台资源。
- CodingVideo 35 pytest、ruff、mypy 及托管部署回读通过。

验收完成。通用 MA 接入、采集、自进化候选、隔离回放、管理员发布/pull、回滚和 CodingVideo 托管部署链路可用。CodingVideo Candidate r3 尚未获得质量改进证明，保持待管理员评审；其长流程在当前高推理模型与固定时限下未完成。增加时限或调整 MA 执行方式属于后续质量迭代，不作为本次自动发布理由。
