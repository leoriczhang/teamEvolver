# KEE R6 部署、操作、迁移与恢复

[接口](../api/20-kee-r6.md) · [交付与验收](../../kee-r6/HANDOFF.md) · [架构说明](../../kee-r6/architecture.html)

## 部署材料

本轮不部署。保留原 TE PG、objects/RLS、租户和服务凭据配置；OV 通用服务继续使用其原 PG 与认证。无需 Ontology 独立库、签名密钥或独立 Runtime。

```yaml
compile:
  executor: internal
ontology:
  enabled: true
  model: {} # 继承 TE 租户有效 llm；可按需覆盖 model/base_url/api_key
  allow_fixture: false
  kee_query_enabled: false
  kee_max_model_calls: 64
  kee_compile_deadline_seconds: 480
  state_dir: /app/deploy/logs/ontology
```

`TE_ONTOLOGY_ENABLED` 仍可覆盖 enabled。compile 是服务级选择，不允许租户改执行器。根目录来源预算仍1000文件、20层、10000条目、20MiB总量、2MiB单文件；冻结及一次权限核对约110秒期限。文件失败404有界重试后记录缺口，身份／服务／预算错误中止。

模型调用每批最多80000字符、总计最多64批、整体480秒，每个隔离进程最多70秒；全部 TE 模型调用单进程上限8。不自动配置真实模型额度。隔离子进程依赖与 TE 使用同一 Python 环境，需包含 openai。

工作目录自动创建；不是日志目录。日志仍由服务级 logging 配置控制，SIT／PRD `/app/deploy/logs`，敏感内容不打印。配置或代码变更后重启，先排空任务并确认端口释放。

OV 账户必须真正启用 ACL，运营主体具有 manage。发布器先设置 restricted，并用另一普通身份 stat 探测403；若 ACL 被关闭，不允许上传私有方法正文。编译在 PG 私有来源上运行，不需要 OV Ontology Skill。

## 操作步骤

1. 知识与方法 → Ontology 与方法 Wiki：填写 Viking 文件／目录或上传一个文档，指定可读 TE 成员。所有成员须能读取每个原件。
2. 输入方法包标识与澄清要求，启动内部 Compile。来源、方法、准确引文及 Schema 同屏查看。
3. 修改结构后保存；确认 Schema。确认不再调用模型，覆盖缺口须显式审核。
4. 填写 Wiki 发布 resources 目标；Prepare 核验当前权限、证据、真实 Skill／个案引用与基线。
5. 人工批准并填写意见；五分钟内 Commit。等待状态 published 及回执，不能将 queued 或索引等待报告为发布成功。
6. 只读区读取固定版本或让 Agent 通过 OV 原入口读取。反馈进入审计，不改变批准结构。
7. 回滚创建新候选，重新审核；撤回整个版本先收紧ACL、递归清理目录，再核验404。来源撤销会围栏旧任务并派发清理。

公开 Wiki、原件、候选并非相同可见范围。反链只从同一批准包生成；主页面完整保留否定、例外和完成标准。OV Admin／Root 的特权读取不构成普通 Agent ACL 测试。

## 恢复与限制

已有 ontology_jobs/outbox 的租约660秒、attempt围栏复用。running 检查点保留 pending，重启可重新领取；取消后迟到结果不能覆盖新状态。支持模式到期时恢复需重新认证，人工重试保存当前有效支持上下文。

发布正文用固定不可变 URI；504、响应丢失先回读摘要，不盲目写第二遍。索引未就绪只继续wait／consistency。目标 pending intent 保留防止另一任务覆盖；30分钟后转人工对账。不要手工删除 pending 或清同步库。

跨 TE/OV 没有分布式／包级原子提交：目录ACL开放、索引与入口切换可能有窗口。权限收紧失败、OV 离线时撤回不能报告成功；保留任务，恢复后继续对账。OV 递归 rm 的检索清理仍须真实部署验收，不把模拟 HTTP 通过视为真实索引验证。

Memory 两阶段流程和 SkillMutationService 不变。内部 Memory Port 保存模型输出到既有 AggregationState，索引恢复不重调模型；只支持受限正文输出，不执行工具型 Skill，不新增自动物理删除保证。通用知识库挖掘旧执行器仍保留兼容入口。

## 迁移干跑与回滚

先暂停旧 Worker／受理，排空或取消在途任务，导出并备份旧表。工具默认干跑，DSN 只从指定环境变量读取，禁止写入命令或文档。

```bash
python scripts/kee_migrate.py export --dsn-env OV_MIGRATION_DSN \
  --account product_agent --output /secure/ov-history.ndjson --receipt /secure/export.json
python scripts/kee_migrate.py import --dsn-env TE_MIGRATION_DSN \
  --account product_agent --tenant '<TE租户ID>' --schema teamevolver \
  --input /secure/ov-history.ndjson --receipt /secure/import-dry-run.json
```

检查 counts、账户、摘要及每条映射后才显式加 `--apply`。导入前缀 `kee/r6/history/ov/<account摘要>/...`，不重新发布、不删原表；旧事实缺少方法／证据时 requires_enrichment。rollback 使用相同输入先干跑，再 apply，仅删除摘要完全相同的本轮导入对象。历史版本、批准、撤回记录保留在导出与映射中。

OV 新安装默认不注册本体扩展（旧环境变量也不触发）。已有表、模块、历史 MCP 源码不自动删除。回滚代码前须停止 KEE 新任务、核对公开 Wiki 与入口；旧 OV 专用 Ontology 版本不会因回滚自动成为最新方法版本。

## 外部算法团队离线导出

```bash
python scripts/kee_wiki.py export procedure-pack.json --output /tmp/wiki-release --sources frozen-sources.json
python scripts/kee_wiki.py validate /tmp/wiki-release --sources frozen-sources.json
```

校验契约、双向链接、精确引用和页面摘要；未提供来源时 evidence_checked=false。额外文件／软链不允许。外部团队可自行审核并通过 OV 通用接口上传；这不产生 KEE 管理回执，management=NOT_MANAGED。
