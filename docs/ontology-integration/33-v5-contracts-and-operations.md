# V5 契约、迁移与部署运维

> 后续版本：[V6 原生 Compile Wiki 构建](45-native-compile-wiki-operations.md) 替代 TE 内置抽取路径；本文保留历史设计及仍有效的发布契约。

## 契约边界

OV `openviking/ontology/contracts.py` 为权威定义，通过 `python -m openviking.ontology.export_contracts` 生成 JSON Schema/OpenAPI 与摘要；TE 固定副本位于 `team_ontology/contracts`、`wire.py`，不导入 OV 内部模块。通用模型仍为 `sf.ontology.v1`，新增制品接口按 V5 增量能力管理，增强检索返回 `sf.ontology.search.v1`。升级必须比较生成物，不只比较 Python 类型名。

| TE 路径前缀 `/te/enterprise/v1` | 作用 |
|---|---|
| `jobs`、`jobs/{id}` | 幂等创建、状态与取消；构建权威记录在 TE |
| `documents`、`snapshots/freeze`、`sources` | 上传解析、按 Viking URI 冻结、登记合成来源 |
| `schemas`、`imports` | Schema 版本、旧 enhancer 历史导入 |
| `jobs/{id}/candidate`、`review`、`graph`、`export` | 候选编辑、逐项评审、图与领域包导出 |
| `jobs/{id}/prepare`、`approve`、`commit` | 准备、记录人工审批、提交与回执对账 |
| `jobs/{id}/rebase`、`rollback-candidates` | 新基线构建、按历史版本生成候选 |
| `versions`、`source-events`、`feedback`、`access` | 版本、撤回、反馈与 OV 授权管理 |

OV `/api/v1/enterprise` 保留 `assets/prepare`、`assets/commits`、`assets/commits/by-key` 和已发布查询；新增 `snapshots/freeze`、`snapshots/read`、`artifacts/sessions`、`artifacts/cancel`、`artifacts`、`assets/versions`、`assets/rollback-material/{generation}`。原来的 `compile-submissions`、`tasks/*`、`runtime/*`、`assets/approvals`、旧 rollback-candidates 不再由正常安装暴露。原生 `/api/v1/compile` 不变。

发布使用 `sf.ontology.commit.v2`：`{contract, prepared_id, commit_key, approval}`。`approval` 包含 `tenant/reviewer/note/prepared_id/candidate_digest/manifest_digest/expected_generation/epoch/expires_at/approval_id`，由 TE 在人工审核后生成并持久化，五分钟有效；不包含签名或公私钥。OV 只接受原生 trusted + 实际 Root Key 认证的发布请求，并在事务中重新检查权限、来源与摘要，消费审批 ID、CAS 切换 generation 并记录回执。普通 admin 角色或任意身份头不能代替 Root Key 认证。`postgres_ready` 仅表示 PG 事实投影可读。

## 配置与启用

TE 本体表现复用 `storage_pg.schema`（默认 `teamevolver`），表名加 `ontology_` 前缀；已存在的 schema 不再执行 CREATE SCHEMA。受限数据库账号、旧表迁移和完整配置见 [schema 修复说明](40-te-ontology-pg-schema.md)。

1. TE 使用自己的 `storage_pg_dsn` 或现有 `TEAMEVOLVER_PG_*`；OV 未设置或留空 `OV_ONTOLOGY_PG_DSN` 时，默认复用当前 `storage.vectordb.pgvector` 连接；未使用 pgvector 时复用已启用 metadata 的 DSN。该变量仅用于显式覆盖，无可用 PG 配置时启动报错。两者无需共库，TE 不直连 OV 数据库。
2. OV 配置 `server.auth_mode: trusted` 和现有 `server.root_api_key`；TE 在租户有效配置 `sharing.viking_api_key` 中使用同一后端 Root Key。TE 不再优先采用 frank 的个人 API Key，也不将 Root Key 返回浏览器。租户账户来自 `sharing.viking_account`，用户来自已登录管理员及受信用户映射。
3. TE 设置 `TE_ONTOLOGY_ENABLED=1`。`ontology` 配置只需 `enabled/state_dir/queue_limit/allow_fixture/model`；`model: {}` 继承租户模型配置，正式环境 `allow_fixture: false`。旧 YAML 中的 signing_key_file/signing_key_id 兼容读取但不使用。
4. OV 设置 `OV_ONTOLOGY_ENABLED=1`、资产 PG 和 `OV_ONTOLOGY_SIGNING_SECRET`（至少 32 字符；仅用于观察句柄，保留现有值）。无需生成 TE 发布私钥或 OV 公钥信任文件，无独立 Runtime。
5. 重启 OV、TE。原生 OV 账户管理员在 TE「使用反馈」入口为 frank 授予 `read/build/approve/publish/feedback`，为 Agent 授予 `read/feedback`。TE admin 与本体权限分别校验。
6. `.env_sit`、`.env_prd` 和 YAML 默认关闭。本次仅本地隔离验证，不代表 SIT 已部署。新发布只支持 trusted + Root Key；普通查询的原生认证保持不变。

## 从签名发布迁移到 Root Key

先暂停旧 TE worker，升级 OV 与 TE 后恢复；不要同时运行旧发布客户端。资产、历史授权消费记录和回执不删除，不重新发布历史版本。

- 旧 `publishing/commit_unknown` 必须先按原提交键查询 OV 回执。存在则恢复 published；OV 不可达则保持不确定状态并重试，不能转为失败或新发布。
- 确认没有回执的旧任务及尚未提交的旧 approved 任务退回 review_ready，清除旧 Prepare/签名/审批/提交键，记录迁移原因及原提交键，要求重新 Prepare 和人工批准。worker 对超过 120 秒未更新的任务处理，用户手动重试也遵循相同对账顺序。
- 新接口拒绝旧 `grant` 请求体。历史契约只为历史数据和回归保留，不开放旧签名发布旁路。
- 删除部署中的发布签名文件配置；原密钥可按环境备份策略归档，不在升级时自动删除文件。详细边界见 [Root Key 发布调整](38-trusted-root-publication.md)。

## 从 V4 迁移

- 先备份 TE 数据库、OV 资产库和 TE 状态目录，保存原分支、commit 和配置摘要。保留用户未提交改动，见基线文件。
- 关闭旧本体受理入口；排空旧 Runtime 任务，或按旧接口逐项取消并取得状态。确认旧 worker 停止后才启用 V5。保留 OV 原生 Compile 的正常调度。
- 查询 `ov_semantic.submissions WHERE origin='legacy'` 建立旧任务清单；不得批量重发已发布候选。V5 additive migration 只新增 artifact_sessions 和 origin 标记，不删除发布资产、撤回或回执。
- 旧 `TE_ONTOLOGY_CONNECTIONS` 文件不再自动作为运行时凭证来源。逐租户把 endpoint/account/key 映射到现有 TE sharing 配置，在可信租户配置中验证；不要把旧文件直接上传浏览器或复制到文档。
- `python scripts/ontology_import.py 旧项目目录 --pack-id xxx --output import.json` 后，在 TE「领域包与 Schema」上传。导入不激活 OV 版本。旧任务审计关联需保留旧 ID 与回执 ID，未自动迁移原生 ov_tasks 到 TE jobs。
- 开启隔离租户联调后再安排 SIT frank 验收。回滚代码前先停 TE worker；不要让新旧调度器同时拥有同一任务。

## 运行状态与故障处理

| 状态/故障 | 行为与处理 |
|---|---|
| queued/running | TE 租约与执行代次恢复；过期任务最多 3 次，不创建新业务任务 |
| 编辑中进程退出 | 120 秒后转 edit_unknown，清除旧批准；重试候选或取消，不能沿用旧授权 |
| preparing 退出 | 120 秒后恢复 review_ready，可重新 Prepare |
| publishing/commit_unknown | 自动按提交键查 OV；未找到再使用相同批准与键尝试，发现基线冲突需重建审核 |
| cancelling | 先持久化取消意图，OV 围栏成功后显示 cancelled；迟到制品被拒绝 |
| OV 不可用 | 不报告发布或撤回成功；操作错误保留审计，撤回用原事件键重试 |
| TE 不可用 | 已发布查询直接由 OV 处理；不依赖 TE 模型、工作目录或在线状态 |
| 来源撤回 | OV 同事务提升 epoch 并阻断证明读取；返回 affected_assertions 与 repair_required |
| 回滚 | 从历史内容构建新候选、重新审批；不得绕过当前来源撤回 |

当前来源事件返回重建影响清单，但跨来源自动修复调度尚未完成。原生文档索引仍由 OV 原有服务维护；本体候选和事实没有写入普通检索索引，增强检索会过滤已登记撤回来源的文档命中。完整历史重建与跨服务一致备份恢复仍需运维演练。

## 本地复现

准备两个独立数据库，设置 `ONTOLOGY_LAB_TE_DSN` 与 `ONTOLOGY_LAB_OV_DSN`，运行 `python scripts/ontology_lab.py start`，再运行 `python scripts/ontology_smoke.py`。默认实验端口 TE 52210、OV 52211，只绑定本机；PID 文件用于精确停止。此 harness 运行相同扩展路由，但不是原生 TE 控制台认证或原生 OV 文档检索的完整部署验收。

数据库测试使用另外两库的 `ONTOLOGY_V5_TE_DSN`、`ONTOLOGY_V5_OV_DSN`，避免与实验 worker 抢任务。模型冒烟显式指定 `scripts/ontology_model_smoke.py --config config/config_sit.yaml --env-file .env_sit --output 结果.json`，只发送合成资料，不输出密钥。
