# TE 与 Ontology Skill 配套升级复测（2026-09-24）

## 范围

在 SIT 使用隔离租户／OV account `te-acceptance-20260924`，复用 115 字合成单篇资料。未修改 `product_agent` 的正式资产。代码基线 `7a3aff4`；尚未从运行服务取得精确 Git SHA。

## 已确认

- TE health、Ontology capabilities 返回 200，隔离账户 generation 为 0。
- 错误 multipart ZIP 请求返回 415，表明此次请求媒体类型修复已生效。
- 隔离账户安装配套 `ontology-extraction-v1/SKILL.md`，以 `te-ontology-compiler`、role=user 回读与仓库全文一致。
- Skill SHA-256：`ab6653f6a4e4f42b51e79f1f2500cffa10ec92991b2d1603d9a2a2af037dabce`。
- 已冻结的来源集合 `ont_5351c08d4f3acf3ab88d1b36` 保持 `sources_ready`。
- 新任务 `ont_18852756a580086a55bdb8a5` 于 15:46:25 受理，返回 202。
- 未登录注册请求返回 401 `login required`；不能仅凭此结果判定服务级注册开关的完整正反例通过。
- 隔离租户经验同步保持关闭。

## 调度阻塞（15:55 观察）

任务持续 queued，attempt=0，outbox.pending=true，尚未调用 OV Compile。只读 PG 查询确认：

- PID `583999` 从 15:46:02 起处于 `idle in transaction`，持有 advisory transaction lock `730211846`。
- 新实例连接等待同一锁，`pg_blocking_pids` 指向 `583999`。
- 查询时没有 running 的 Ontology 任务。
- 数据库 `idle_in_transaction_session_timeout` 为 `1h`，不是立即自动释放。

该锁正是 `ControlStore.claim()` 使用的全局领取锁。现有证据确认阻塞链；连接是否来自重启前实例尚未与容器日志交叉确认。用户随后授权终止连接；执行前重新核验时，该原始事务已不存在，因此没有终止任何连接。现有任务已自动恢复领取。未修改任务表或绕过调度。

## 验证范围

Schema 确认、候选、Prepare／Approve／Commit、发布查询、回滚及撤回均未执行。新 Skill 已通过真实 Compile 的原生草稿登记，但生成内容未通过 TE 契约校验。浏览器控制一度超时，随后补做来源和构建页操作，具体范围见下文；不以接口结果替代页面验收。

脱敏状态来自实际 HTTP 响应与只读 PG 查询；完整响应及凭据仅保存在本机私有临时目录，不随本文归档。

## 恢复后继续验证

16:01 原生 Compile `cmp_c25d4a68a7994c218e55a8fc3615d840` 已受理，进入 `compile: agent`，trace 为 `ac76b08bf70ac7ae1740135625ec89b1`。TE 记录 Skill 规范化摘要 `a152200cae2aeee9f8f083c3e0bee30716d592e552752a676e695dd081243e12`，证明线上 TE 与更新后的 Skill 通过配套校验。最终结果见下文。


## 第一轮 Compile 完成与后处理拒绝

原生任务 `cmp_c25d4a68a7994c218e55a8fc3615d840` 最终 completed，成功产出 `result-manifest.json`、`schema-proposal.json`、`coverage.json`、`drafts/batch-0001.jsonl` 及原生 Markdown 导航／草稿。原生草稿登记兼容修正得到真实验证。

TE 后处理返回 `COMPILE_OUTPUT_INVALID`，触发第 1 次补编译 `cmp_c9acffa8159b43449bbb9b52107a56b0`。回读第一轮草稿并用当前 Assertion 模型核验：6 条断言中 a-1、a-2、a-4、a-5、a-6 的 `valid_from=null`，产生 `datetime_type`；a-3 的日期字段合法。Schema 模型校验通过。没有修改线上草稿或填造日期。

第一轮 OV 任务记录累计输入 129,859、输出 28,084、总计 157,943 Token；约 10 分钟。Token 按调用累计，包含重复上下文，不代表独立资料量或计费结算。

负例接口：未审批提交返回 409 `APPROVAL_REQUIRED`；普通身份访问本轮 input 和 output 均返回 403。

## 页面补测

Chrome 已实际打开隔离租户 Ontology 工作台，来源页和构建页可见，无加载错误。默认 Skill URI 正确，未选择来源集合时构建按钮禁用。浏览器为 frank、接口测试任务为 admin；当前按主体隔离的任务列表为空，未将它当作接口任务的页面进度验证。


## 最终结果：失败，未发布

任务最终为 `failed / stage_inputs / COMPILE_ACL_TARGET_NOT_READY`，round=2。两次已经提交的原生 Compile 均为 completed；第三次尚未提交，未遗留本次运行中的 Compile。隔离账户与 `product_agent` 的正式 generation 均仍为 0。

第二轮输出 7 条断言，逐项 Assertion 模型核验发现：6 条 `valid_from=null`；剩余 a-4 的 `valid_to` 与 `valid_from` 同为 `2026-09-24T10:00:00+08:00`，被时间区间校验拒绝。没有人工修改线上草稿、伪造日期或绕过审批。

第二轮累计输入 240,438、输出 43,344 Token；两轮合计输入 370,297、输出 71,428 Token。真实抽取和补编译约 23 分钟，未达到单篇快速验收目标。

最终错误发生于新一轮目录 ACL 设置：TE 已对 `COMPILE_ACL_TARGET_NOT_READY` 做 6 次有界重试，仍未成功。此错误对应 OV 的 `ACL target has no context record; index it first`。随后只读检查 round=2 的目录、input、output 都存在（stat=200），ACL GET 也返回 200，但模式为 inherit，不能当作 TE 要求的 restricted 已成功。这与目录文件系统状态和上下文记录就绪时间不一致相符，底层原因仍需 OV 日志核验。

## 后续修复重点

1. 把结构化校验错误（字段路径、错误类型）安全地传给补编译，而非只有 `COMPILE_OUTPUT_INVALID`。明确 `valid_from` 不可空、`valid_to` 必须晚于起点；无法从来源确定时间的断言按既有规则处理，不能填造日期。
2. 目录 ACL 初始化应持久化等待状态并核对上下文记录就绪，恢复时复用当前执行目录；不能靠增加整轮模型重编译解决目录初始化问题。
3. 对调度锁持有者的事务空闲超时、容器重启断连与恢复增加故障测试；此次在实际终止前原连接已消失，没有执行连接终止或修改线上 PG 配置。

本轮只验证、诊断及更新验收材料，未修改业务代码、部署或提交推送。脱敏结果见 [配套复测证据](./sit-20260924-paired-evidence.json)。文档检查仍只有既有 `docs/en/api/06-skill-sync.md` 对 `teamEvolver/skills/mutations.py` 的失效引用。
