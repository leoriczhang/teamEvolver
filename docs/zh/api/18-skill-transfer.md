# Skill 导入导出

Skill 导入导出在团队 Skill 库与 ZIP、Skill 市场、Git 仓库之间迁移 Skill。所有操作作用于当前租户的团队 Skill，要求租户管理员。控制台不提供入口，通过 API 调用。

代码入口：`team_skills/api/transfer.py:create_router`、`team_skills/transfer/commands.py:TransferCommands`、`team_skills/transfer/handler.py:transfer`、`team_skills/transfer/service.py:SkillTransferService`。

导入导出都是异步任务：API 校验请求、加密凭证、在一个事务内写入 `skill_transfers` 与 `skill.transfer` 任务后返回 202；Worker 执行网络、ZIP 与 Git 操作。导出在受理时固定所选 Skill 的当前版本。

## 端点

| 方法与路径 | 说明 |
|---|---|
| `GET /api/skills/transfer/channels` | 渠道及导入、导出能力：`zip`、`marketplace`、`git` |
| `GET /api/skills/transfer/skills` | 当前团队可导出的 Skill 与版本 |
| `POST /api/skills/import` | 受理导入，返回 202 |
| `POST /api/skills/export` | 受理导出，返回 202 |
| `GET /api/skills/transfer/jobs/{job_id}/result` | 任务结果，未完成时 `result` 为 null |
| `GET /api/skills/transfer/jobs/{job_id}/download` | ZIP 导出的归档（`application/zip`），下载前校验 sha256 |

受理响应：

```json
{"status": "accepted", "job_id": "transfer-export:...", "status_url": "/api/jobs/transfer-export:..."}
```

带 `Idempotency-Key` 的重复请求返回同一任务；同一 Key 携带不同请求返回 409。

## 请求

导入：`{"channel": "zip|marketplace|git", "options": {...}, "conflict": "replace"}`。`conflict` 为 `replace`（默认，记录新版本）、`skip`（跳过同名 Skill）或 `error`（存在同名即整体拒绝）。

导出：`{"channel": "zip|marketplace|git", "names": ["demo"], "options": {...}}`。`names` 必填且不重复，1–256 个。

上限：每次最多 256 个 Skill、4096 个文件，内容总计 128 MiB，ZIP 64 MiB。路径穿越、重复名称、符号链接、加密 ZIP 在写入前拒绝。

请求必须为 `application/json`，其他媒体类型返回 415；ZIP 内容放在 `options.zip_b64`。`options` 只接受下表列出的字段，字符串最长 8192（`zip_b64` 除外），`token` 与 `secret_ref` 只能二选一。`token` 在受理时以 Secret Ref 加密保存（需要 `TEAMEVOLVER_SECRET_MASTER_KEY`），不写入任务记录、结果或日志。

### ZIP options

| 参数 | 方向 | 说明 |
|---|---|---|
| `zip_b64` | 导入，必填 | ZIP 的 base64 内容；保持原有 JSON 上传契约 |
| `name` | 导入，可选 | 仅单 Skill 可指定名称；否则优先采用 SKILL.md 的 name，回退到目录名 |
| `single` | 导入，可选 | 布尔值；为 true 时 ZIP 必须只含一个 Skill |

自动识别根目录 `SKILL.md`、单层包装目录及 `workspace/skills/<name>/SKILL.md` 等多层布局。每个 Skill 的 frontmatter 必须有 description。导出目录为 `<name>/SKILL.md` 与同目录附件。

### 市场 options

| 参数 | 说明 |
|---|---|
| `provider` | `clawhub`（默认）或 `http` |
| `registry_url` | ClawHub 服务地址，默认 `https://clawhub.ai` |
| `slug` | ClawHub 拉取必填；上传默认使用 Skill 名称 |
| `version` | 拉取可选，留空使用最新版本；上传必填语义版本号，如 `1.0.0` |
| `token` / `secret_ref` | Bearer Token 或已有 Secret Ref；ClawHub 上传必填 |
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
| `username`、`token` | HTTP Git 账号与密码/Token；`token` 以 Secret Ref 加密保存，也可直接传已有的 `secret_ref` |
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
curl http://localhost:52010/api/skills/import \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" -H 'X-Tenant-Id: demo' \
  -H 'Content-Type: application/json' \
  -d '{"channel":"git","conflict":"replace","options":{"url":"https://git.example.com/team/repo.git","branch":"main","path":"skills"}}'

curl http://localhost:52010/api/skills/export \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" -H 'X-Tenant-Id: demo' \
  -H 'Content-Type: application/json' \
  -d '{"channel":"zip","names":["demo"]}'

curl -o demo.zip http://localhost:52010/api/skills/transfer/jobs/<job_id>/download \
  -H "Authorization: Bearer $TEAMEVOLVER_ROOT_API_KEY" -H 'X-Tenant-Id: demo'
```

第三条命令在导出任务成功后下载归档。

## 结果与错误

导入结果包含 `imported`、`skipped`、`errors` 三个数组。`imported` 项包含 `name`、`created`、`status`（`created`、`updated`、`unchanged`）、`version`、`files`、`tree_sha256`、`origin`；Git 来源记录分支、commit、路径，市场来源记录 provider、slug、version。内容相同的 Skill 不增加版本。整包格式错误在写入前失败；逐个 Skill 提交时可能部分成功，调用方需检查 `errors`。

市场与 Git 导出结果包含 `exported`，Git 另含仓库地址、分支和 commit。ZIP 导出结果包含 `download_url` 与 `sha256`。

导出过程中断后不会自动再次推送：Worker 先用检查点与远端对账，无法确认时任务进入 `reconciling`。

| 状态 | 含义 |
|---|---|
| 400 | 包格式、渠道参数、路径或请求结构无效 |
| 403 | 非租户管理员 |
| 404 | 选中的 Skill 不存在，或任务 / 归档不存在 |
| 409 | `Idempotency-Key` 冲突、同名冲突、目标 Git 分支已存在、归档校验失败 |
| 413 | 文件数量或大小超限 |
| 415 | 请求不是 JSON |
| 503 | 未配置凭证加密主密钥，或租户 `skills` 模块配置缺失 |

网络类失败（408、429、502、503、504）由 Worker 自动重试；其他失败以 `transfer_error_<status>` 作为任务 `error_code`。
