# 从 skill-opt 切换到客户版 teamEvolver

这份说明对应本目录的客户定制版。先并行部署、核对真实样本，再切换入口；不覆盖旧项目、不删除旧数据。旧前端和旧 HTTP API 不作为新服务的兼容层。

## 控制台保持原版

本版以原 teamEvolver 控制台为基础，保留所有原导航及页面，不再采用精简客户导航。租户搜索位于原侧栏；数据源、converter 配置及预检是增量功能。
技能挖掘默认启用控制台，输入和任务产物保存在 `DATA_ROOT/.teamEvolver/skillminer`；真实挖掘需另行配置模型与 Hermes。全局模型、用户管理写入、运行状态和 SkillMiner 仍在 default 账号使用，其他账号的拒绝访问不代表模块被删除。
Agent 工作空间保留个人/团队、Skill、Memory、资源以及 Skill Lab、Memory Lab。未配置 OpenViking 时仍可打开两个 Lab，但 Memory 与资源读取需要连接；终端需 OpenViking CLI。PG 中的内部对象与 OpenViking 文件树不是同一份数据，不能以空文件树判定 PG 数据丢失。部署验收需分别核对两类存储。

## 先确认四件事

| 项目 | 必须具备 |
|---|---|
| 旧数据目录 | 客户实际 DATA_ROOT，里面有 `config/*.yaml`、`claw_workspaces/<项目>/workspace/skills/`；源码压缩包通常不含这些 |
| 旧代码目录 | `inc-aiagent-core-skill-opt/converters/`，与 DATA_ROOT 可以不在一起 |
| 数据库 | PostgreSQL 14+，业务角色不得是 SUPERUSER/BYPASSRLS，可在独立 teamevolver schema 建表 |
| 网络 | 新服务能访问客户 PG、Langfuse API、模型网关；需要 DEAP Replay 时还要能访问灰度 Agent |

使用普通部署用户运行下面的命令。先备份旧 DATA_ROOT、converters 和 PG；备份、运行时环境文件不要上传版本库。
本版生产配置限定 **一个实例、一个 ASGI worker**。实例内有界并发、多租户隔离和进化跨实例锁已实现，但不能据此认定所有管理操作已经支持多副本。

## 第一步：安装新服务

进入新 teamEvolver 目录，不要在旧目录里覆盖安装。Python 推荐 3.11。

```bash
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r docker/requirements.customer.lock
.venv/bin/pip install --no-build-isolation --no-deps -e .
export OLD_DATA=/CUSTOMER/DATA_ROOT
export OLD_CONVERTERS=/CUSTOMER/inc-aiagent-core-skill-opt/converters
export TEAMEVOLVER_PG_DSN='postgresql://APP_USER:URL_ENCODED_PASSWORD@PG_HOST:5432/DB_NAME'
```

密码包含 `#/@/:` 时必须 URL 编码。也可不用 DSN，改用 `OV_PG_HOST`、`OV_PG_PORT`、`OV_PG_DATABASE`、`OV_PG_USERNAME`、`OV_PG_PASSWORD` 分字段注入。
如果所有项目共用模型，可同时设置 `TEAMEVOLVER_LLM_BASE_URL/API_KEY/MODEL`；旧 YAML 中各项目已有的模型配置会随项目迁移。

离线部署：在联网构建机准备基础镜像和锁文件中的 wheel，或者构建客户镜像后 `docker save/load`。原 Dockerfile 的 Hermes/OpenViking CLI 构建链不用于本次交付。

## 第二步：预检，不导入业务数据

```bash
.venv/bin/python scripts/prepare_customer.py \
  --legacy-root "$OLD_DATA" --converters-dir "$OLD_CONVERTERS"
```

脚本会生成 `runtime/customer/config.yaml`、权限为 0600 的 `customer.env`、`migration-report.json`。第一次自动生成服务 Root Key，以后重跑复用原 Key。检查 PG 连接及 RLS 角色，并逐项目检查 converter；不修改旧系统，不创建新的 Skill 版本。

报告中 `blocked` 必须为空才能继续。常见原因：

| 结果 | 处理 |
|---|---|
| 没有项目 YAML | OLD_DATA 指错了；读取旧 `.env` 中的 DATA_ROOT，再指向实际目录 |
| missing converter | 检查 OLD_CONVERTERS；按 `langfuse.project_id.py` 优先、项目名 `.py` 次之查找 |
| unsupported import/symbol | 按本文“二开边界”改造，不要跳过 converter 直接导入 |
| Doris source requires explicit… | 旧项目启用了 Doris；必须确认 Langfuse API 同窗口、同过滤条件的数据一致，或先实现 Doris SourceAdapter |
| PG 检查失败 | 检查地址、端口、密码编码、schema 权限；不能用超级用户规避 RLS 校验 |

当前提供的 73 份 converter 均通过静态检查与合成样本转换测试，不代表客户环境所有真实 Trace 结构已验收。
单独检查代码目录、不连接数据库：

```bash
.venv/bin/python scripts/import_skillopt.py --scan-converters \
  --converters-dir "$OLD_CONVERTERS"
```

## 第三步：确认数据源，执行导入

先暂停目标项目的旧跑批与新服务的进化任务，避免导入与人工编辑竞争。

```bash
.venv/bin/python scripts/prepare_customer.py \
  --legacy-root "$OLD_DATA" --converters-dir "$OLD_CONVERTERS" --apply
```

**只有已经核对 Doris 与 Langfuse API 数据等价的项目**，才添加 `--allow-doris-api-fallback`。该参数不是启动修复开关，会明确选择 HTTP 数据源，而不会继续访问 Doris。

导入内容：项目名称、模型配置、Langfuse 连接/过滤条件、converter 源码、完整 Skill Bundle、已配置的 DEAP Replay 接入。未提供 Account ID 时按项目名称生成稳定 ID；重跑不会换 ID，相同 Skill 内容不会重复产生版本。

若必须绑定既有 OpenViking Account，按单项目导入：

```bash
source runtime/customer/customer.env
.venv/bin/python scripts/import_skillopt.py \
  --legacy-root "$OLD_DATA" --converters-dir "$OLD_CONVERTERS" \
  --project '旧项目名称' --account-id EXISTING_ACCOUNT_ID --apply
```

登记不会自动在 OpenViking 创建账号。迁移过程可重跑；部分项目因存储失败中断时先修复问题，再重跑，源目录保持不变。导入会更新目标项目配置，因此迁移完成后的日常升级不要反复运行 `--apply` 覆盖新环境的人工修改。
旧 `output/` 报表与旧审核记录保留只读，不直接当作已验证的新 Evidence；选择固定时间窗口重新拉取 Session。

## 第四步：启动与初始化管理员

容器方式：

```bash
source runtime/customer/customer.env
docker compose -f compose.customer.yaml up -d --build
curl --fail http://127.0.0.1:52010/readyz
```

准备脚本记录部署用户 UID/GID，Compose 挂载同一 DATA_ROOT 到 `/data`，配置与运行状态不会留在镜像层。可通过 `PYTHON_IMAGE` 和 `PIP_INDEX_URL` 使用客户可达的镜像仓库。
不使用 Docker 时，在独立终端运行：

```bash
source runtime/customer/customer.env
PORT=52010 bash scripts/start_customer.sh
```

另一个终端加载同一环境文件后初始化管理员，密码替换为至少 12 字符的强密码：

```bash
source runtime/customer/customer.env
curl --fail http://127.0.0.1:52010/api/auth/bootstrap \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"REPLACE_WITH_A_STRONG_PASSWORD"}'
```

已初始化时返回 409，不会覆盖管理员。后续用浏览器正常登录，不向浏览器配置 Root Key。
`/healthz` 是进程存活探针，`/readyz` 检查 PG。模型未配置时不会向公开模型端点发送 Session。

## 第五步：验收后切换入口

1. 侧栏租户搜索选择一个项目；在“数据源接入”统一编辑该项目的 Langfuse 连接、会话转换模式、converter 和 Mapper，不改动其他项目。
2. “会话转换模式”选择“兼容模式（导入旧版 converter.py）”后导入 `.py`，先兼容检查，再提供 `{trace, observations}` 原始 JSON 做离线试跑，比较用户文本、最终回复、工具 observationId 和空回复是否保留。保存后下一次拉取使用新源码，不需重启。
3. 在“会话拉取”选择固定时间窗口，先预览，再拉取 10 个 Session。核对新旧 Trace ID 集合、Session 分组、过滤数量、工号、工具参数与错误标记。
4. 两个项目导入同名 Skill、同名 Session，分别查看，确认不串数据。错误 Token 为 401，跨账号选择为 403。
5. 验证候选生成、“运行总览”下的“候选评审”、人工发布和回滚；在灰度 DEAP 检查两分支独立 Workspace、多轮 conversationId、文件增删、清理失败提示。
6. 重启新服务与 PG，检查项目、Skill、候选、Session、登录态；再做持续负载与 PG 断连恢复测试。
7. 停止旧跑批并保留旧实例，记录切换时间；把客户反向代理/发布平台入口切到新服务 52010。更新旧书签及直接调用旧 Flask API 的自动化脚本。

新旧平台不要同时向同一生产 Skill 目标发布。Root Key 仅用于可信服务调用，按 Account ID 选择项目：

```bash
curl --fail http://127.0.0.1:52010/langfuse/pull \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'X-Tenant-Id: ACCOUNT_ID' -H 'Content-Type: application/json' \
  -d '{"max_sessions":10,"from_timestamp":"2026-09-01T00:00:00Z","to_timestamp":"2026-09-02T00:00:00Z"}'
```

单次拉取上限 1000 个 Session，旧适配器元数据默认最多扫描 10000 条 Trace，超过时缩小时间窗口，不能静默截断。`datasource_legacy_options.max_traces` 最大 50000。
模型并发默认最多 8，待处理模型调用最多 64；周期默认 4 路、每周期 100 个 Session。HTTP 429/503 由采集端带退避重试。不要仅调大并发而忽略客户模型 QPM 和 PG 连接预算。

## 需要客户二开的边界

| 原有能力 | 本版处理与二开位置 |
|---|---|
| 73 份当前 converter | 支持 convert 和六钩子，旧 core.langfuse_client 纯解析函数按原代码保留；源码按项目存 PG，不依赖旧目录运行 |
| Doris / 自定义 SQL | 不直接迁移旧 engine 单例；实现 source_adapter.SourceAdapter 的 list_session_ids/fetch_session/convert_session/health，启动前 register_source_adapter 注册 |
| converter 第三方依赖 | 当前兼容层只映射已列出的 core 纯函数和常用标准库；新依赖、core.engine 全局状态、SDK `.api` 调用须改造后检查 |
| ctx 差异 | ctx.langfuse() 返回本版 REST 客户端而不是旧 SDK；ctx.config 对应 datasource_legacy_options；run_dir/out_dir 为 None。依赖旧磁盘 output/ 的钩子须迁移到 SessionStore/PG |
| CAS/项目成员权限 | 不冒充已接入 CAS。客户应对接受信任 SSO/网关，并增加账号授权映射；不能直接信任浏览器的 X-Tenant-Id 或工号头 |
| 旧 prompts/分析语义 | 在“进化链路”重新核对阶段模板、输出预算和判据，不能把旧 success/failure agent 模板原样塞进不同阶段 |
| 生产发布 | 默认不覆盖生产 Workspace。使用既有发布流程，或实现并验证 skill.sync.v1 回调；DEAP Replay 的文本接口不提供完整用量，需人工复核 |
| 旧 REST/CLI/SSE | 旧 `/api/project-config` 等调用需迁移为租户配置、`/langfuse/pull`、`/trigger`；请求结构和同步/异步语义不能直接混用 |
| 报表、钉钉、调度 | 旧报表保留只读；通知与定时调度对接客户平台，不在新服务中运行旧全局 scheduler |
| 多副本 | 需补全控制台会话、配额、管理写入与 outbox 的集群一致性、进程故障注入测试，再解除单 worker 限制 |

主要扩展点：`teamEvolver/integrations/legacy_converter.py`、`teamEvolver/integrations/source_adapter.py`、`teamEvolver/integrations/deap_replay.py`、`teamEvolver/integrations/skill_sync_adapters.py`。
兼容导入并不是不可信 Python 沙箱。只允许服务管理员维护 converter；离线试跑执行 convert，不执行取数钩子，限时 5 秒，新脚本仍须在隔离环境审查 I/O 与全局可变状态。

## 回退

发现数据映射、发布结果或权限异常时，先停止新服务跑批，禁止继续向生产发布；把反向代理入口改回旧服务并恢复旧调度。
不要删除新 PG 或回写旧目录。保留新服务的迁移报告、账号 ID、切换时间、失败 Trace ID 和候选版本，修复后用同一窗口复测。
仅平台入口回退不会自动回滚已经发布到外部 Agent 的 Skill；外部生产 Skill 必须通过原发布系统或 teamEvolver 的历史版本再发布回滚。
