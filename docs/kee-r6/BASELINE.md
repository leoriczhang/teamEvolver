# KEE R6 源码基线与差距

日期：2026-10-08。该报告对应本轮本地实现与隔离验证，不代表 SIT／PRD 已部署。

| 仓库 | 实施前二开 SHA | 本轮固定的 upstream/main SHA |
|---|---|---|
| teamEvolver | 69eb1deef3c0c50e38a3b5cad46657e9582548bd | f6f6e430b51bc2277bb98389155c9d8d6e8bfb62 |
| OpenViking | 2b04991c0ddbdc4b78912d0e480c9ebf8aa28b46 | 961fb4639505cff3051cc8faf74c757d50e085f0 |

上游仅 fetch 到 `refs/remotes/kee-baseline/main`，没有合并上游，也没有 push。UpClaw 不修改。

## 保留的未提交资料

TE 的 `docs/acceptance/ontology-handover-sit-20260924.md` 及其 evidence JSON、OV 的 43 号多实例分析及知识库回执为进入任务前已有的未跟踪资料；本轮不覆盖、不纳入提交。

## 核验结果与实现选择

- 原控制台已有单登录、租户权限、Workspace、Skill Evolution、True Replay、Memory 与 SkillMiner；采用菜单映射，不重建身份或资产注册表。
- 原 Ontology 工作台依赖 OV 专用资产接口与原生 Compile。R6 方法任务改用 KEE 内部 Port 与隔离子进程；新结构使用现有 PG objects，原任务、审核、审计表复用。
- 初始 pytest 收集缺失 `team_skills.evolution.runtime`。该目录在历史提交 `e8a802b` 被删除，但当前 entry/mixins 仍引用它。本轮从该提交父版本恢复唯一实现，并适配当前 Evidence、pattern_refs 与候选描述契约；未新建第二套 Judge。
- OV 默认扩展注册移除 Ontology；历史代码、表与专门测试入口保留。通用 Compile 内部循环及 UpClaw 保持原样。
- 旧 Ontology 任务仍可按原执行器查询、取消。原生安装的新建旧本体 API 返回 410，指向 `/kee/v1/methods/build`；独立 legacy 适配测试仍支持旧契约。迁移时先停止旧受理，不能混用两个正式写者。

## 明确差距

三种视角首期只调整导航顺序，尚非三个完全定制首页。方法编制采用同屏 JSON 编辑、来源与证据对照及文本澄清；复杂逐项澄清分派、完整语义 Schema 校验、自动局部补编译不在本轮实现。

内部 Memory Compile 为受限 JSON 输出模式：1 MiB 输入、128 输出页、最多两次模型尝试，不执行 Skill 脚本或工具。既有大型／工具型 Skill 需单独兼容验收；不能将该适配报告为与 OV Agent 循环等价。

既有通用“知识库挖掘”的 OV Compile 接口保留其原执行器兼容路径，本轮迁入的是新方法构建与 Memory Port。SkillMiner 保留现有流水线，Skill 发布仍唯一走 SkillMutationService。

历史 Ontology 迁移工具保留结构、版本、证据、批准及撤回原记录，按账户映射到 KEE 历史前缀。缺方法定义的旧事实不会被猜测为 ProcedurePack；进入待补证历史区，需运营人员明确构造方法后重新审核。
