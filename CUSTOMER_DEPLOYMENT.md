# 客户版部署与验收

本定制目录独立于原 `/path/to/teamEvolver`，旧 skill-opt 项目未修改。

首次切换请按 [逐步部署、迁移、验收及二开说明](docs/zh/guides/09-customer-migration.md) 操作。`scripts/prepare_customer.py` 可生成持久化环境文件、复用 Root Key，并完成预检和导入。

## 支持边界

- PostgreSQL 主存储：Session、归档、逐行索引和租户配置。Skill、候选及其版本历史独立使用本地/NAS 或 OpenViking；PG 不可用时不降级共享本地文件。
- Account ID 隔离，服务级 Root Key + `X-Tenant-Id` 选择账号；采集端可使用独立租户 Token。
- Session 写入事务化，消费时核对内容摘要，旧周期不会删除同 Session 后续新增的内容。
- 每进程模型调用最多 8 路、待处理最多 64；租户周期默认 4 路；HTTP 默认 256 路。过载返回 429/503，调用方需退避重试。
- 导入旧项目 YAML 的 Langfuse、模型配置、converter 和基线 Skill Bundle；DEAP 使用独立 Workspace 回放与清理。

**交付配置限定单实例、单 ASGI worker。** 跨实例进化互斥已验证，但控制台会话、配额和所有管理操作尚未完成多副本一致性验收，不能直接扩为多副本。
这不是旧 Flask API 的逐接口兼容层。旧报表、CAS、Doris 直连与回写、钉钉通知、旧 prompts、定时任务不会自动迁移；converter 的依赖或钩子不兼容时会阻止导入并提示二开。

## 启动

要求 Python 3.11+、PostgreSQL 14+。业务数据库角色必须是 `NOSUPERUSER NOBYPASSRLS`，可在指定 schema 建表；不要使用 postgres 超级用户。

```bash
python3.11 -m venv .venv
.venv/bin/pip install --require-hashes -r docker/requirements.customer.lock
.venv/bin/pip install --no-build-isolation --no-deps -e .
export TEAMEVOLVER_PG_DSN='postgresql://APP_USER:URL_ENCODED_PASSWORD@PG_HOST:5432/DB_NAME'
export TEAMEVOLVER_ROOT_API_KEY="$(openssl rand -hex 32)"
export TEAMEVOLVER_LLM_BASE_URL='https://MODEL_HOST/v1'
export TEAMEVOLVER_LLM_API_KEY='YOUR_MODEL_KEY'
export TEAMEVOLVER_LLM_MODEL='YOUR_MODEL_ID'
export TEAMEVOLVER_SKILL_STORAGE_BACKEND='local'
export TEAMEVOLVER_SKILL_STORAGE_ROOT='/srv/teamevolver/skill-storage'
PORT=52010 bash scripts/start_customer.sh
```

把生成的 Root Key 存入客户密钥管理系统，重启时注入同一值，不要每次重新生成。
旧模型 URL 可以包含 `/chat/completions`，Token 可以带 `Bearer ` 前缀。PG 密码含特殊字符时需 URL 编码，或使用 `OV_PG_HOST/PORT/DATABASE/USERNAME/PASSWORD` 分字段注入。
客户模式默认把各阶段输出限制在 8192 tokens；按实际模型调整 `TEAMEVOLVER_LLM_MAX_OUTPUT_TOKENS`。没有模型密钥时拒绝模型请求，不向公开默认端点发送 Session。
`DATA_ROOT` 默认为本目录 `runtime/customer`；首次启动从 `docker/customer.yaml` 初始化可写的 DATA_ROOT/config.yaml，容器中为 /data/config.yaml。升级不覆盖已保存配置。

容器部署使用独立配置，不要直接使用原来的 compose.yaml：

```bash
export SKILL_STORAGE_ROOT='/mnt/nas/teamevolver/skill-storage'
sudo install -d -m 700 -o "${TEAMEVOLVER_UID:-10001}" -g "${TEAMEVOLVER_GID:-10001}" "$SKILL_STORAGE_ROOT"
docker compose -f compose.customer.yaml up -d --build
curl --fail http://127.0.0.1:52010/readyz
```

## Skill 存储

客户配置默认使用 `local`，Compose 将宿主 `SKILL_STORAGE_ROOT` 挂载到容器内 `/var/lib/teamevolver/skill-storage`。该宿主目录可以是本地持久盘或已挂载的 NAS；容器内路径保持不变。上传、编辑、Evolution 发布和回滚都写入同一后端，每次内容变化会保留不可变版本：

```text
tenants/<account_id>/skills/<skill_name>/versions/v1/
tenants/<account_id>/skills/<skill_name>/versions/v2/
```

`default` 账号为兼容旧数据不带 `tenants/default/` 前缀。NAS 必须支持同目录原子 rename，并向容器运行 UID/GID 提供读写权限；本交付仍限定单实例、单 worker。备份时同时保存 PostgreSQL 与完整 `SKILL_STORAGE_ROOT`，恢复时保持二者来自同一备份时间点。

如需由 OpenViking 保存 Skill 及版本历史，切换为：

```bash
export TEAMEVOLVER_SKILL_STORAGE_BACKEND='viking'
export TEAMEVOLVER_OV_ENDPOINT='https://YOUR_OPENVIKING_ENDPOINT'
export TEAMEVOLVER_OV_ROOT_KEY='YOUR_OPENVIKING_ROOT_KEY'
docker compose -f compose.customer.yaml up -d
```

OpenViking 模式写入 `viking://resources/<root_prefix>/tenants/<account_id>/skills/...`。切换后必须重启服务；系统不会自动在本地/NAS 与 OpenViking 之间搬迁既有版本。迁移前应暂停上传和 Evolution，完整复制并核对 `manifest.json`、`evolve_skill_registry.json` 与每个 `versions/vN/`。

客户镜像仅安装 `.[pg]`，不强制安装 Hermes、OpenViking CLI、Node 或高版本 glibc。前端使用代码包中的已构建资源。
可用 `PYTHON_IMAGE` 指定客户基础镜像仓库、`PIP_INDEX_URL` 指定可达的软件包镜像。不能访问公网时，必须先准备基础镜像和锁文件对应的离线 wheel。
离线环境在联网构建机制作镜像，再 `docker save/load`；运行时只需要连接客户 PG、Langfuse、模型、DEAP。
`/healthz` 是存活探针，`/readyz` 检查 PG。配置无效或 PG 不可用时启动失败退出，不会虚假报告就绪。

## 控制台与租户

控制台保留技能挖掘、进化闭环、实验工作台、个人与团队资产、平台资产和治理入口。个人记忆与团队资源从实验工作台拆到侧栏独立页面，Skill Lab 与 Memory Lab 不再提供独立入口。
SkillMiner 控制台默认开启，可通过 `TEAMEVOLVER_SKILLMINER_ENABLED=0` 显式关闭；输入和任务产物位于 `DATA_ROOT/.teamEvolver/skillminer`，应一并备份。实际挖掘仍需可用模型及 Hermes，标准客户镜像未安装 Hermes。
全局模型、用户管理写入、运行状态和 SkillMiner 是服务级能力，在 default 账号使用；非 default 请求仍有后端访问限制，菜单不会因此消失。实验工作台可独立编辑和调试 Skill；个人记忆、团队资源及其终端仍需配置 OpenViking/CLI。平台资产独立读取 PostgreSQL 与 NAS，不依赖 OpenViking，也不显示 OV CLI。

首次管理员初始化必须带 Root Key，密码至少 12 字符：

```bash
curl --fail http://127.0.0.1:52010/api/auth/bootstrap \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"username":"admin","password":"REPLACE_WITH_A_STRONG_PASSWORD"}'

curl --fail http://127.0.0.1:52010/api/tenants \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"display_name":"项目 A","account_id":"account-a"}'
```

`agent_token`（租户机器凭证，前缀 `tevt_`）仅在创建/轮换时展示。它是 Agent 数据面**唯一**的机器凭证，覆盖 Agent 协议面：Session 上报（`/ingest_session`，含 Agent Protocol V1 / v2）、Context Workspace（`/internal/agents/context/*`）与 Skill pull；Session v2、Context 和 Skill pull 都携带 user_id，Account 使用租户有效配置，不查询全局用户或 Agent 注册。调用 Context Workspace 时必须在请求中声明 `integration_id`（声明即归属）；但它仍然不能访问管理员接口（`/api/*`）与 Agent 注册接口（`/internal/agents/register`）。注册接口不再签发任何按 Agent 的令牌（`tev1_` 已废弃，携带会被拒绝）。建议直接使用已开通的 OpenViking Account ID；登记不会自动在 OV 建账号。
Root Key 是服务管理员凭据，不应放在浏览器前端或交给不可信调用方；Agent 数据面统一使用上述租户机器凭证，不再有按 Agent 的最小权限令牌。
单租户部署（未启用 `storage_pg`）下不通过控制台签发凭证，而是由运维配置环境变量 `TEAMEVOLVER_TENANT_TOKEN`（必须以 `tevt_` 开头，服务端将其解析为 `default` 租户）；多租户由控制台签发。
需要 True Replay 时给租户绑定 `replay_adapter`；开关与 adapters_dir 为部署级配置。
服务级设置在 default 账号编辑；租户配置在租户管理页编辑。未完成租户化的服务级页面返回拒绝访问，不会展示其他账号数据。
已有部署先看 [迁移与回滚](./docs/zh/guides/11-agent-deregistration.md)。

```bash
curl --fail http://127.0.0.1:52010/api/tenants/account-a/config \
  -X PUT -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'Content-Type: application/json' \
  -d '{"overrides":{"langfuse_enabled":true,"langfuse_host":"https://LANGFUSE_HOST","langfuse_public_key":"YOUR_PK","langfuse_secret_key":"YOUR_SK"}}'

curl --fail http://127.0.0.1:52010/langfuse/pull \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" \
  -H 'X-Tenant-Id: account-a' -H 'Content-Type: application/json' \
  -d '{"max_sessions":10}'
```

租户配置中的 `null` 表示删除覆盖。账号、PG DSN 和物理存储路径不允许通过租户配置改绑。

## 旧系统导入

需要客户真实 DATA_ROOT。源码压缩包不包含运行中的项目 YAML、基线 Workspace 和历史产物。

```bash
.venv/bin/python scripts/import_skillopt.py --legacy-root /CUSTOMER/DATA_ROOT --converters-dir /CUSTOMER/SOURCE/converters
.venv/bin/python scripts/import_skillopt.py --legacy-root /CUSTOMER/DATA_ROOT \
  --converters-dir /CUSTOMER/SOURCE/converters \
  --project PROJECT_NAME --account-id account-a --apply
```

默认预览，`--apply` 才写 PG，执行期间暂停目标账号写入。脚本不修改旧系统、不输出密钥、不触发生产更新。
Skill 经既有 SkillMutationService 导入，相同内容重复执行不创建新版本。
历史运行目录与旧审核结论不直接当作新 Evidence；保留旧系统只读，按明确时间窗口重新拉取 Session。
`scripts/migrate_local_to_pg.py` 是 TeamEvolver 本地对象迁移工具，不是旧 skill-opt 项目迁移工具。

## DEAP 回放

旧 YAML 配置 `experiment.agent_host/emp_id` 时，导入器登记回放能力。客户需开启热加载、允许 Workspace 接口，并使用有权限的工号。
Baseline/Candidate 各用随机 Workspace，多轮维持同一 conversationId；候选只写临时 Workspace，正常或异常结束均清理。
二进制 Bundle 无法经文本写入接口回放，会明确失败。清理失败会返回 Workspace 名称，不会假装成功。
DEAP 的 answer 不含完整 Token/工具用量，结果标记指标不完整，需要人工复核，不能据此自动发布。
本版不会直接覆盖生产默认 Workspace。最终生产发布继续使用客户发布流程或经过验证的 skill.sync.v1 回调。

## 验收顺序

1. `/readyz` 成功，PG 断开后失败，恢复 PG 后重新可用。
2. 两账号写同名 Session、Skill，查询内容不串用；无凭据 401，伪造账号 403。
3. 突发写入后核对已接受 Session、归档、索引数量，采集端正确重试 429/503。
4. 重启后租户、Session、Skill、候选、历史存在；处理中任务按至少一次语义恢复。
5. 先拉取 10 条真实 Session，再加批量；核对模型网关 400/429、超时、输出预算。
6. 灰度 DEAP 验证 Bundle 增删文件、工号权限、多轮连续性、Workspace 清理。
7. 人工审核、发布、回滚通过后再切换，保留旧系统和 PG 备份用于回退。

本地测试不代表客户吞吐 SLA。生产需以真实 Session 大小、模型限额、连接预算做持续负载与重启/断网演练。

## 数据保护

- 切换前备份 PG 的 teamevolver schema、服务数据卷与 `SKILL_STORAGE_ROOT`，旧系统目录继续保留，不做原地覆盖。
- 原交付包的文档/测试包含硬编码数据库密码示例，本目录已替换为虚构值；如现网使用过原值，应在客户环境轮换。
- 对外暴露时使用 TLS 反向代理与网络访问控制；不要把 Root Key 写入前端、提交到仓库或输出到日志。
- 配额文件和可选后台质量评分队列不是多副本任务系统；本版明确限定单 worker，不承诺严格 exactly-once、集群配额或未确认 DEAP 用量的自动验收。
