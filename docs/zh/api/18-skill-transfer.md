# Skill 导入导出

## 实现与认证

Skill 迁移端点作为 API 兼容能力保留，控制台不再提供导入或导出入口。操作目标为当前租户的**团队 Skill**。所有端点要求管理员控制台 Cookie，并沿用控制台的租户选择机制。

统一 Module 为 `team_skills/transfer/service.py` 中的 `SkillTransferService`，Interface 为 `import_skills(channel, options, conflict=...)` 和 `export_skills(channel, names, options)`。`TransferAdapter` 的 `read` / `write` 使用完整 `SkillPackage`（名称、相对路径到 bytes 的映射、来源版本信息）。ZIP、市场和 Git Adapter 只处理传输；新增渠道可通过构造参数 `adapters` 注册，无须修改版本管理。

导入先校验整批内容，再逐项通过 `SkillMutationService` 记录完整版本，最后替换本地工作副本。相同内容不增加版本。存储使用现有 local/NAS 或 Viking 配置；pull 模式不创建 delivery outbox。导出优先读取当前已记录版本，包含 `SKILL.md`、脚本、参考文件和二进制附件，ZIP 可再次导入。

HTTP 入口：`teamEvolver/proxy/skill_transfer.py:register_skill_transfer_routes`。

## 端点

| 方法与路径 | 说明 |
|---|---|
| `GET /api/skills/transfer/channels` | 支持的渠道及导入、导出能力 |
| `GET /api/skills/transfer/skills` | 当前团队可导出的 Skill 与版本 |
| `POST /api/skills/import` | 统一导入 |
| `POST /api/skills/export` | 统一导出；ZIP 返回二进制，其他渠道返回 JSON |
| `POST /api/skills/import-zip` | 原单 Skill ZIP 接口，转入统一流程；多 Skill 包在写入前拒绝 |
| `POST /api/skills/import-zip-batch` | 原批量 ZIP 接口，转入统一流程 |

导入请求为 `{"channel":"zip|marketplace|git","options":{...},"conflict":"replace"}`。`conflict` 可为 `replace`（默认，记录新版本）、`skip`（跳过同名项）、`error`（任何名称冲突均在写入前拒绝）。

导出请求为 `{"channel":"zip|marketplace|git","names":["demo"],"options":{...}}`。`names` 必填，不会默认导出整个团队。每次最多 256 个 Skill、4096 个文件，内容总计不超过 128 MiB，ZIP 不超过 64 MiB。路径穿越、重复名称、符号链接、加密 ZIP 在导入前拒绝。

### ZIP options

| 参数 | 方向 | 说明 |
|---|---|---|
| `zip_b64` | 导入，必填 | ZIP 的 base64 内容；保持原有 JSON 上传契约 |
| `name` | 导入，可选 | 仅单 Skill 可指定名称；否则优先采用 SKILL.md 的 name，回退到目录名 |

自动识别根目录 `SKILL.md`、单层包装目录及 `workspace/skills/<name>/SKILL.md` 等多层布局。每个 Skill 的 frontmatter 必须有 description。导出目录为 `<name>/SKILL.md` 与同目录附件。

### 市场 options

| 参数 | 说明 |
|---|---|
| `provider` | `clawhub`（默认）或 `http` |
| `registry_url` | ClawHub 服务地址，默认 `https://clawhub.ai` |
| `slug` | ClawHub 拉取必填；上传默认使用 Skill 名称 |
| `version` | 拉取可选，留空使用最新版本；上传必填语义版本号，如 `1.0.0` |
| `token` | Bearer Token；ClawHub 上传必填 |
| `name` | 单 Skill 导入名称，可选 |
| `display_name`、`changelog` | ClawHub 上传显示名称、版本说明，可选 |
| `download_url` | `http` 模式导入必填，GET 需返回 ZIP |
| `upload_url` | `http` 模式导出必填，POST multipart 接收 ZIP，返回成功 JSON 对象 |
| `file_field` | `http` 上传字段名，默认 `file`；另有 `names` 字段，内容为 JSON 名称数组 |

ClawHub 对接 [官方 v1 HTTP 协议](https://github.com/openclaw/clawhub/blob/main/docs/http-api.md)：`GET /api/v1/download?slug=&version=`；上传单个 Skill 使用 `POST /api/v1/skills`，multipart 中包含 `payload` JSON 和 `files[]`。市场拒绝或限流会返回失败，不自动重试上传。仅处理市场返回的 ZIP；若条目返回 Git 来源描述，使用 Git 渠道导入对应仓库。自定义市场须符合表中 ZIP 契约，其他协议可新增 Adapter。

### Git options

| 参数 | 说明 |
|---|---|
| `url` | 导入及 `new_branch` 导出必填，HTTP(S)、SSH 或 `git@host:path`；不接受服务端本地路径 |
| `branch` | 导入来源分支、导出基础分支；留空使用仓库默认分支 |
| `commit` | 导入可选，7–40 位十六进制 commit；记录实际解析到的完整 commit |
| `path` | Skill 目录，默认 `skills`，`.` 表示仓库根目录 |
| `name` | 单 Skill 导入名称，可选 |
| `username`、`token` | HTTP Git 账号与密码/Token，只用于本次请求 |
| `mode` | 导出：`new_branch`（默认）或 `new_repository` |
| `new_branch` | 新分支名称；已有仓库默认生成唯一名称，新仓库默认 `main` |
| `message` | 可选提交说明 |
| `provider` | 新仓库：`github`（默认）或 `gitlab` |
| `api_url` | 新仓库 API 根地址，默认 GitHub `https://api.github.com` 或 GitLab `https://gitlab.com/api/v4` |
| `repo_name` | 新仓库名称，必填；新建仓库也必须提供 Token |
| `namespace` | GitHub 组织名称，或 GitLab 数字 namespace_id；留空使用当前账号 |
| `private` | 新仓库是否私有，布尔值，默认 true |

同步是一次显式拉取，不注册定时任务，也不删除本地其他 Skill。SSH 使用服务账号已配置的 SSH 密钥和 known_hosts。导出保留仓库中未选择的其他内容，仅替换所选 Skill 目录，目标分支必须尚不存在。新建仓库失败不会回退为覆盖已有仓库；若仓库已创建但推送失败，响应会给出仓库地址，便于重试。

## 示例

```bash
curl -b console.cookies http://localhost:52010/api/skills/import \
  -H 'Content-Type: application/json' \
  -d '{"channel":"git","conflict":"replace","options":{"url":"https://git.example.com/team/repo.git","branch":"main","path":"skills"}}'

curl -b console.cookies http://localhost:52010/api/skills/export \
  -H 'Content-Type: application/json' \
  -d '{"channel":"zip","names":["demo"]}' -o demo.zip
```

## 结果与错误

导入返回 `imported`、`skipped`、`errors` 三个数组。`imported` 项包含 `name`、`created`、`status`（created/updated/unchanged）、`version`、`files`、`tree_sha256`、`origin`。Git 来源保留分支、commit、路径，市场来源保留 provider、slug、version；凭据不写入版本记录。

整包格式错误在写入前失败。存储阶段逐 Skill 提交，可部分成功；调用方必须检查 `errors`，不能只根据 HTTP 200 判断全部成功。`errors[].stored=true` 表示版本已记录但工作副本更新失败，可重试恢复缓存。远端存储自身在异常中的部分写入语义沿用现有 SkillHub，跨多个 Skill 不提供事务回滚。市场和 Git 导出返回 `exported`，Git 另含仓库地址、分支和 commit。

| 状态 | 含义 |
|---|---|
| 400 / 422 | 包格式、参数、路径或请求结构无效 |
| 403 | 当前控制台用户不是管理员 |
| 404 | 选中的 Skill 不存在 |
| 409 | 同名冲突，或目标 Git 分支已存在 |
| 413 | 文件数量或大小超限 |
| 429 | 服务端两个传输槽位均被占用 |
| 502 / 503 / 504 | 远端/存储错误、缺少 Git、Git 超时 |

Git 与 HTTP I/O 在后台线程执行，HTTP 请求等待结果；每个服务进程最多两个传输操作同时运行。凭据不写入仓库 URL、缓存配置或响应。真实市场上传、新建托管仓库需要部署环境具备对应网络和权限。
