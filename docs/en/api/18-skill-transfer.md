# Skill Import and Export

Skill import and export move Skills between the team Skill library and ZIP files, Skill marketplaces and Git repositories. All operations act on the current tenant's team Skills and require a tenant administrator. The console has no entry for them; use the API.

Code entry points: `team_skills/api/transfer.py:create_router`, `team_skills/transfer/commands.py:TransferCommands`, `team_skills/transfer/handler.py:transfer`, `team_skills/transfer/service.py:SkillTransferService`.

Imports and exports are asynchronous: the API validates the request, encrypts credentials, writes `skill_transfers` and a `skill.transfer` task in one transaction and returns 202; the Worker performs the network, ZIP and Git work. Exports pin the current version of the selected Skills at acceptance.

## Endpoints

| Method and path | Description |
|---|---|
| `GET /api/skills/transfer/channels` | Channels and their import / export capabilities: `zip`, `marketplace`, `git` |
| `GET /api/skills/transfer/skills` | Team Skills and versions available for export |
| `POST /api/skills/import` | Accept an import, returns 202 |
| `POST /api/skills/export` | Accept an export, returns 202 |
| `GET /api/skills/transfer/jobs/{job_id}/result` | Task result; `result` is null until finished |
| `GET /api/skills/transfer/jobs/{job_id}/download` | Archive of a ZIP export (`application/zip`), sha256-verified before download |

Accept response:

```json
{"status": "accepted", "job_id": "transfer-export:...", "status_url": "/api/jobs/transfer-export:..."}
```

Repeated requests with the same `Idempotency-Key` return the same task; the same key with a different request returns 409.

## Requests

Import: `{"channel": "zip|marketplace|git", "options": {...}, "conflict": "replace"}`. `conflict` is `replace` (default, records a new version), `skip` (skips Skills with an existing name) or `error` (rejects the whole request if any name exists).

Export: `{"channel": "zip|marketplace|git", "names": ["demo"], "options": {...}}`. `names` is required, unique, 1–256 entries.

Limits: at most 256 Skills, 4096 files, 128 MiB of content and a 64 MiB ZIP per request. Path traversal, duplicate names, symlinks and encrypted ZIPs are rejected before anything is written.

Requests must be `application/json`; other media types return 415. ZIP content goes in `options.zip_b64`. `options` accepts only the fields listed below, strings are at most 8192 characters (except `zip_b64`), and `token` and `secret_ref` are mutually exclusive. `token` is encrypted into a Secret Ref at acceptance (needs `TEAMEVOLVER_SECRET_MASTER_KEY`) and never written to task records, results or logs.

### ZIP options

| Parameter | Direction | Description |
|---|---|---|
| `zip_b64` | Import, required | Base64 ZIP content; keeps the existing JSON upload contract |
| `name` | Import, optional | Name for a single-Skill import only; otherwise the SKILL.md name is used, falling back to the directory name |
| `single` | Import, optional | Boolean; when true the ZIP must contain exactly one Skill |

Root-level `SKILL.md`, a single wrapper directory, and multi-level layouts such as `workspace/skills/<name>/SKILL.md` are detected automatically. Each Skill's frontmatter must have a description. Exports are laid out as `<name>/SKILL.md` with attachments in the same directory.

### Marketplace options

| Parameter | Description |
|---|---|
| `provider` | `clawhub` (default) or `http` |
| `registry_url` | ClawHub service address, default `https://clawhub.ai` |
| `slug` | Required for ClawHub pulls; uploads default to the Skill name |
| `version` | Optional for pulls (empty uses the latest); required semantic version for uploads, such as `1.0.0` |
| `token` / `secret_ref` | Bearer token or an existing Secret Ref; required for ClawHub uploads |
| `name` | Optional single-Skill import name |
| `display_name`, `changelog` | Optional ClawHub upload display name and release notes |
| `download_url` | Required for `http` imports; GET must return a ZIP |
| `upload_url` | Required for `http` exports; receives the ZIP via POST multipart and returns a JSON object on success |
| `file_field` | `http` upload field name, default `file`; a `names` field carries a JSON array of names |

ClawHub uses the [official v1 HTTP protocol](https://github.com/openclaw/clawhub/blob/main/docs/http-api.md): `GET /api/v1/download?slug=&version=`; a single-Skill upload uses `POST /api/v1/skills` with a `payload` JSON and `files[]` in the multipart body. Marketplace rejections or rate limiting fail the task and uploads are not retried automatically. Only ZIPs returned by the marketplace are handled; if an entry returns a Git source descriptor, import that repository through the Git channel. Custom marketplaces must follow the ZIP contract in the table; other protocols can be added as new Adapters.

### Git options

| Parameter | Description |
|---|---|
| `url` | Required for imports and `new_branch` exports; HTTP(S), SSH or `git@host:path`; server-local paths are not accepted |
| `branch` | Import source branch, export base branch; empty uses the repository default branch |
| `commit` | Optional for imports, a 7–40 hex commit; the fully resolved commit is recorded |
| `path` | Skill directory, default `skills`; `.` means the repository root |
| `name` | Optional single-Skill import name |
| `username`, `token` | HTTP Git account and password/token; `token` is encrypted into a Secret Ref, or pass an existing `secret_ref` directly |
| `mode` | Export: `new_branch` (default) or `new_repository` |
| `new_branch` | New branch name; existing repositories get a unique generated name by default, new repositories default to `main` |
| `message` | Optional commit message |
| `provider` | New repository: `github` (default) or `gitlab` |
| `api_url` | New repository API root, default GitHub `https://api.github.com` or GitLab `https://gitlab.com/api/v4` |
| `repo_name` | New repository name, required; creating a repository also requires a token |
| `namespace` | GitHub organization name or GitLab numeric namespace_id; empty uses the current account |
| `private` | Whether the new repository is private, boolean, default true |

A sync is one explicit pull; it registers no schedule and deletes no other local Skills. SSH uses the SSH key and known_hosts configured for the service account. Exports keep unselected content in the repository and replace only the selected Skill directories; the target branch must not exist yet. A failed repository creation never falls back to overwriting an existing repository; if the repository was created but the push failed, the response gives the repository address for a retry.

## Examples

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

The third command downloads the archive after the export task succeeds.

## Results and Errors

Import results contain three arrays: `imported`, `skipped`, `errors`. `imported` items contain `name`, `created`, `status` (`created`, `updated`, `unchanged`), `version`, `files`, `tree_sha256`, `origin`; Git sources record branch, commit and path, marketplace sources record provider, slug and version. Skills with identical content do not get a new version. Package format errors fail before anything is written; per-Skill commits can partially succeed, so callers must check `errors`.

Marketplace and Git export results contain `exported`; Git also includes the repository address, branch and commit. ZIP export results contain `download_url` and `sha256`.

An interrupted export is not pushed again automatically: the Worker first reconciles with the remote using its checkpoint, and when that cannot be confirmed the task enters `reconciling`.

| Status | Meaning |
|---|---|
| 400 | Invalid package format, channel parameters, path or request structure |
| 403 | Not a tenant administrator |
| 404 | Selected Skill does not exist, or task / archive does not exist |
| 409 | `Idempotency-Key` conflict, name conflict, target Git branch already exists, archive verification failed |
| 413 | File count or size over the limit |
| 415 | Request is not JSON |
| 503 | Credential encryption master key not configured, or tenant `skills` module configuration missing |

Network-class failures (408, 429, 502, 503, 504) are retried by the Worker automatically; other failures set the task `error_code` to `transfer_error_<status>`.
