> 后续调整（2026-09-21）：发布改用 [trusted + Root Key](38-trusted-root-publication.md)，保留人工审批。下文保留当时设计与验收记录；独立发布签名不再是启用条件。

> 历史 V4 记录。2026-09-20 起部署职责调整为 TE 内置构建、OV 资产后端；后续见 [V5 实施说明](32-te-ov-v5-implementation.md)。本文测试结果不自动算作 V5 验收。

# Ontology 分阶段验收与当前状态

日期：2026-09-19。结果仅适用于隔离本机、合成租户与明确列出的测试；测试通过不等于生产验证。

## 已执行证据

- 历史研究包离线契约 40 项通过。requirements 已补 jsonschema[format]，否则无时区样本可能未被格式检查拒绝。
- 新增真实 PostgreSQL 集成测试覆盖发布幂等、并发受理/提交、候选不可见、证据 ACL、撤回、来源不可用恢复、取消围栏、候选防替换、双时间、预算、只读角色、观察句柄、回滚及重基准。本轮最终 19 项通过，见 results/integration-tests.txt。
- TE—OV—Runtime HTTP 冒烟通过：两个合成领域、构建/审核/发布/查询、回执重放、隐藏来源、非法查询、无业务写入口、撤回后 UNKNOWN。
- enhancer 全量（含新增 Runtime 用例）75 项通过；OV metadata / task 相关回归 149 项通过，1 项 pgvector 专项未纳入该命令。
- TE 全量回归 480 通过、19 跳过（全量命令未配置 DSN；单独 PG 命令全部通过）、5 项既有失败，并在未修改 HEAD 的隔离副本复现：Candidate Feedback 审核主体、3 项 Session Analyze 兼容行为、Skill 存储前缀。另一个旧包目录测试由遗留 Python 3.14 字节码导致；已只清理这些缓存。
- React TypeScript + Vite 生产构建通过。引用检查结果单独保存，不隐去仓库既有失效链接。

## 规模测试的正确解读

万文档测试通过真实 OV HTTP、独立 Runtime 和 PostgreSQL；100 批合成文档，fixture 抽取、自动测试批准。它验证吞吐和工作流，不验证 LLM 准确率或人工审核效率。

100K / 1M / 5M 为合成 PG 投影行敏感性测试：固定授权证据源，均匀单事实实体，10 并发，通过同一个语义服务读取。它绕过 ingestion/publication，只能说明该索引查询形态的延迟，不能证明真实 5M 断言构建可用。

## V3 验收矩阵映射原则

O02/O04/O05/O06/O07/O08/O09/O10/O11/O12/O13/O14/O16/O17/O18/O19/O20/O21/O23/O24/O25/O27/O28/O31/O33/O34 的部分不变量由新增集成/Runtime 测试覆盖；实际矩阵逐项标记 passed/partial/not_run，不用这一列表宣称全部通过。

O01 原生能力协商、O03 所有 native 搜索/导出通道的黑盒隔离、O15 独立 OR 证明、O22 全部侧信道、O29 Wiki 与 generation 协同、O30 混合在线生产负载、O35 业务金标来源家族划分、O36 真实模型和人工成本，仍需专项证据。O32 真实业务写操作本轮不适用；缺少写入口已测试。

## 后续完成门槛

P0/P1/P2/P3 已交付可验证的实验纵切；P4 已有增量版本、来源事件、撤回、取消、重基准、回滚与规模测试，但跨源自动补拉、自动修复编排和备份外部 IAM 授权恢复联动尚未完成。P5 第二合成领域已验证；不代表真实第二业务域验收。

真实模型抽取需要明确的模型名、兼容端点与本地凭证配置；当前未获得该配置，状态是 blocked，而不是 fixture 已替代通过。生产 IAM/连接器、真实业务金标及生产发布不在本轮授权范围内。

## 停机、恢复与原生接入补证

TE 独立进程停止、52110 端口关闭时，直接 OV HTTP 仍返回第二领域的授权事实。pg_dump/pg_restore 已在独立 ontology_restore 数据库完成：正式 generation、回执和来源撤回状态保留，撤回后的事实仍不可见。此结果不证明企业 IAM 水位恢复或 WAL 时间点恢复。

原生 OV 完整 app 工厂在 52113 启动，Ontology capabilities/context 与原生任务列表三个接口均返回 200；但已有 Rust RAGFSBindingClient 缺少 invalidate_cache，原生 metadata worker 报错。故原生接入状态为 partial/blocked，不能宣称完整原生联调通过。

新增边界：多实体逐实体求值；来源事件序列缺口不得延迟撤回；同分区同序号重复事件拒绝；统一错误体不回显原始输入；模型端点格式降级重试也计入 12 次真实 HTTP 调用上限。

结果入口：[运行汇总](results/verification-summary.json)、[O01—O36 状态](results/acceptance-matrix.json)、[依赖固定清单](results/environment-lock.txt)、[源码摘要](results/source-manifest.json)。

## O01—O36 逐项状态（知识库同步副本）

partial 表示仅部分条件有证据，不能算该项通过。

| 项目 | 状态 | 已验证或缺口 |
| --- | --- | --- |
| O01 原生Compile能力协商 | blocked | 原生 app/读取/任务列表可用；完整 Compile 协商未验，现有 Rust invalidate_cache 缺失阻塞 worker。 |
| O02 Runtime凭证越界读取 | partial | 任务专用 capability/Manifest 输入实现并测围栏；跨任务模糊测试未做。 |
| O03 staging候选普通检索 | partial | 新增语义查询不读候选；所有原生搜索/导出通道未逐一黑盒验证。 |
| O04 同submission键并发请求 | partial | 并发受理同任务及异请求冲突已测；事务中间杀进程未注入。 |
| O05 受理成功但响应丢失 | partial | HTTP 重复提交同任务、UI 保存原键；丢包代理场景未执行。 |
| O06 模型臆造类型或扩大Schema | partial | 批准 Schema 确定性校验；真实模型臆造测试缺配置。 |
| O07 同名不同地区网点／不同类型 | partial | 身份与 label 分离；完整别名歧义界面未实现。 |
| O08 缺产品／地区／时点等必需限定 | partial | 必要限定校验已实现；产品/地区业务金标未验收。 |
| O09 Wiki摘要没有原始证据 | partial | 旧 KG 缺片段进入隔离 helper；自动 Wiki 导入未接入。 |
| O10 循环生成来源互相证明 | partial | 证明 DAG 循环/深度校验；完整生成来源自支持场景未执行。 |
| O11 UserClaim伪装SystemFact | partial | 非可信来源不能声明 system_fact；真实业务连接器未接入。 |
| O12 工具结果过期／跨主体句柄 | partial | 观察 task/audience/subject/expiry 绑定测试；真实接口 TTL 场景未验。 |
| O13 业务有效期与记录时点交叉查询 | partial | valid/known-at 与历史撤回门禁测试；完整版本轨迹界面未实现。 |
| O14 多必要源中一项无权或撤回 | partial | 保守全引用 AND 与当前 ACL；完整复合证明矩阵待补。 |
| O15 多个独立证明 | not_run | 独立 OR 证明未实现；使用保守 AND 可能降低可用性。 |
| O16 候选摘要批准后被替换 | partial | 候选不可变与批准摘要校验；替换被拒。 |
| O17 两个候选同base并发commit | partial | 同基线竞争 Commit 只有一个成功；rebase 冻结新基线。 |
| O18 commit成功但回执丢失 | partial | 同键返回相同回执；提交后精确时刻崩溃未注入。 |
| O19 撤权与commit／读取竞态 | partial | 租户锁线性化授权/撤回，测试 epoch 围栏；穷举竞态未执行。 |
| O20 取消后迟到Runtime结果 | partial | 取消阻断迟到上传/发布；模型线程强制终止未实现。 |
| O21 老版本回滚／备份恢复 | partial | 回滚重审且查当前源；独立新库备份恢复通过；外部 IAM 联动未验。 |
| O22 隐藏节点与证明 | partial | 隐藏证明标识过滤；计数/时序等全部侧信道未穷举。 |
| O23 高出度节点／恶意QueryPlan | partial | 恶意查询与预算检查；高出度对抗图未压测。 |
| O24 上下文预算不足 | partial | 整条事实/观察裁剪且重算规则，缺证据仍 UNKNOWN。 |
| O25 原文提示注入／恶意工具URL | partial | 不执行源 URL、Broker 白名单；真实模型注入未测。 |
| O26 TE停机、授权服务正常 | passed | 精确 PID 停 TE、端口关闭后直接 OV HTTP 仍有授权事实。 |
| O27 权限服务不可用 | partial | 没有授权缓存回退；数据库故障 fail closed，完整故障注入待做。 |
| O28 Source503／同步暂时失败 | partial | Source unavailable 产生 degraded/UNKNOWN，恢复事件已测。 |
| O29 Wiki新版本、本体旧generation | not_run | 冻结来源版本与 generation；Wiki 增量补拉与 lag 对账未实现。 |
| O30 批量编译与在线查询并发 | partial | 万文档 fixture 构建与并发查询实测；模型公平性/生产混合负载未测。 |
| O31 人工改、AutoFix、回滚工具 | partial | rebase/rollback 走候选/真实回执；完整人审编辑与 AutoFix 未实现。 |
| O32 真实业务写动作绕过Skill | not_applicable | 本轮排除真实业务写；actions/execute 返回404。 |
| O33 工具Schema升级与旧Rule | partial | ToolContract 固定在 Schema、建议按权限过滤；外部升级协议未验。 |
| O34 不同用户同问句缓存 | partial | 无跨主体结果缓存、不同读者隔离已测；既有其他检索缓存不在新路由内。 |
| O35 同源工单改写进入评测两侧 | not_run | 无真实业务金标，未执行来源家族/时间泄漏检测。 |
| O36 文档首导／日更放大 | partial | 万文档时间/调用数已记录；真实 Token 与人审成本未测。 |
