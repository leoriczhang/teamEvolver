# TE Ontology PostgreSQL schema 配置与受限权限启动修复

2026-09-21。故障日志显示 Ontology 在执行 `CREATE SCHEMA IF NOT EXISTS te_ontology` 时收到 `permission denied for database teamevolver`，导致整个 FastAPI lifespan 启动失败。主服务较早打印的 ready 日志不代表最终启动成功。

## 当前配置

Ontology 复用 TE 服务级有效配置，不再固定使用独立的 te_ontology schema：

```yaml
storage_pg:
  schema: teamevolver

ontology:
  enabled: true
```

`storage_pg.schema` 默认 teamevolver，可改为 DBA 已提供的自定义 schema。它同时也是 TE 原有 PG 存储使用的 schema，不是租户可任意切换的数据库命名空间。沿用现有 `storage_pg.dsn` 或 `TEAMEVOLVER_PG_*` 连接配置，没有新增 Ontology DSN 或 schema 环境变量。既有 `TE_ONTOLOGY_ENABLED=0` 会覆盖 YAML 开关，实际启用时设为 1。

新表采用 ontology_ 前缀，避免占用原有业务表名：

- teamevolver.ontology_jobs：构建、候选审核、提交回执引用与执行状态。
- teamevolver.ontology_outbox：持久化执行队列，与任务同事务写入。
- teamevolver.ontology_audit：审批、恢复与操作审计。
- teamevolver.ontology_migrations：初始化与迁移版本。

所有读写、外键、任务恢复与审计 SQL 都使用经过校验和引用的配置 schema，不依赖连接 search_path。表名不会由浏览器请求决定。

## 权限与启动行为

初始化持有事务级 advisory lock，先查询 schema 是否存在。已存在时完全不执行 CREATE SCHEMA，只创建缺失表；因此不需要数据库级 CREATE 权限。运行账号需要目标 schema 的 USAGE、CREATE 及既有本体表、序列的相应权限。

新 schema 不存在时，有数据库 CREATE 权限的账号可自动创建；受限账号会收到提示配置 storage_pg.schema 或请 DBA 预建的明确错误。启动失败后释放本体连接池，避免残留连接。

如果已有 teamevolver schema，且账号能在其中建表，本次故障只需更新代码并重启，不必手工建本体表，也不必扩大数据库级权限。若缺少 schema 权限，由 DBA 按实际角色授予，例如：

```sql
GRANT USAGE, CREATE ON SCHEMA teamevolver TO your_app_role;
```

以上 SQL 是运维参考，本次没有连接或修改 SIT/PRD 权限。

## 旧版本升级

升级前停止全部旧 TE 进程或 worker，不进行新旧版本并行的滚动运行；备份本体任务与审计数据。

首次启动发现旧 te_ontology 的 migrations/jobs/outbox/audit 四张表，且具有 v5-001 标记、目标不存在对应本体表时，会在同一事务内改名并移动到配置 schema。保留任务 ID、候选和回执引用、队列外键、审计行以及自增序列；记录 v5-002-configurable-schema。迁移要求当前账号有旧表的所有者权限及目标 schema 的 CREATE 权限。

若旧表不完整、版本标记未知或新旧本体表同时存在，拒绝覆盖或自动合并，保留数据并提示人工处理。其他对象（例如已有索引）造成迁移冲突时事务整体回滚。不会删除旧 schema 或无关表。首次没有旧本体表的环境直接初始化新表。

后续更改已经投入使用的自定义 schema，需要先安排数据迁移；自动兼容逻辑只识别旧版固定 te_ontology 四表，不会扫描并搬迁任意业务 schema。代码回退前也需要评估表名迁移，旧代码不能读取新表名。

## 验证记录

已在本机隔离 PostgreSQL 用临时数据库及受限角色验证：默认 schema、自定义 schema、幂等启动、任务队列与审计；无数据库 CREATE 权限但有 schema 权限时成功；schema 缺失时提示与连接池释放；旧任务迁移与审计序列延续；目标冲突及迁移中途失败时原数据完整保留。临时数据库、角色在测试后清理。

同时运行 Ontology 发布与查询回归、迁入引擎测试、TE 全量测试、Python 编译和文档引用检查。实际结果见 `results/te-schema-verification.json`。没有改动 OV 资产 schema，没有部署 SIT/PRD；不将本机通过等同于线上恢复。
