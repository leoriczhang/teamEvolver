# Skill Import and Export

## Implementation and authentication

Skill transfer endpoints remain available for API compatibility, but the console no longer exposes import or export actions. Transfers operate on the current tenant's team Skills. Every endpoint requires an administrator console session.

`SkillTransferService` in `team_skills/transfer/service.py` exposes `import_skills(channel, options, conflict=...)` and `export_skills(channel, names, options)`. A `TransferAdapter` transports complete `SkillPackage` objects (name, relative file paths mapped to bytes, origin metadata). ZIP, marketplace and Git adapters share validation, conflict handling and version recording. Embedded callers can register additional adapters through the constructor.

Imports validate the entire input before committing individual Skills through `SkillMutationService`, then replace local working copies. Unchanged content retains its version. Exports prefer the current recorded version and preserve binary attachments. Storage follows existing local/NAS or Viking settings; pull mode does not create a delivery outbox.

HTTP implementation: `teamEvolver/proxy/skill_transfer.py:register_skill_transfer_routes`.

## Endpoints and requests

| Endpoint | Purpose |
|---|---|
| `GET /api/skills/transfer/channels` | Available channels and capabilities |
| `GET /api/skills/transfer/skills` | Exportable team Skills and versions |
| `POST /api/skills/import` | `{channel, options, conflict?}` |
| `POST /api/skills/export` | `{channel, names, options?}`; ZIP bytes or remote result JSON |
| `POST /api/skills/import-zip` | Compatible single-Skill ZIP import |
| `POST /api/skills/import-zip-batch` | Compatible batch ZIP import |

Channels: `zip`, `marketplace`, `git`. Import conflict policy: `replace` (default), `skip`, or `error` (reject the whole batch before writing). Export requires explicit, unique `names`. Limits per operation: 256 Skills, 4096 files, 128 MiB content and 64 MiB ZIP. Traversal paths, duplicate names, encrypted ZIP entries and symlinks are rejected.

### ZIP

Import requires `options.zip_b64`; optional `name` applies only to a single Skill. Root, wrapped and nested multi-Skill archives are recognized. Description frontmatter is required; names default to frontmatter then directory names. Exports contain `<name>/SKILL.md` and attachments and can be reimported.

### Marketplace

`provider` is `clawhub` (default) or `http`. ClawHub uses `registry_url` (default `https://clawhub.ai`), `slug` (required for import, defaults to the Skill name for export), optional import `version`, and required semver export `version` and `token`. Export supports one Skill and optional `display_name` and `changelog`.

The [official v1 HTTP contract](https://github.com/openclaw/clawhub/blob/main/docs/http-api.md) uses `GET /api/v1/download?slug=&version=` and `POST /api/v1/skills` with multipart `payload` JSON and `files[]`. Only ZIP responses are consumed; Git source descriptors should be imported through the Git channel.

Custom HTTP imports require a `download_url` returning ZIP. Exports require an `upload_url` accepting multipart ZIP under `file_field` (default `file`), plus a JSON-encoded `names` form field, and returning a successful JSON object. Optional `token` is sent as Bearer authorization. Other marketplace protocols require a new adapter.

### Git

| Option | Description |
|---|---|
| `url` | Required for import/new-branch export; HTTP(S), SSH or scp-style URL, no local server paths |
| `branch` | Source/base branch; blank uses the remote default |
| `commit` | Optional import revision, 7–40 hex characters; full resolved commit is recorded |
| `path` | Skill directory, default `skills`; `.` means repository root |
| `name` | Optional single-Skill import name |
| `username`, `token` | Request-only HTTP credentials |
| `mode` | Export: `new_branch` (default) or `new_repository` |
| `new_branch` | New ref; generated unique name for existing repos, `main` for new repos |
| `message` | Optional commit message |
| `provider` | New repository: `github` (default) or `gitlab` |
| `api_url` | GitHub default `https://api.github.com`; GitLab default `https://gitlab.com/api/v4` |
| `repo_name` | Required for new repositories, together with `token` |
| `namespace` | GitHub organization or numeric GitLab namespace ID; blank uses current account |
| `private` | New repository visibility, boolean, defaults to true |

Sync is an explicit one-time pull, without scheduled jobs or deletion of unrelated local Skills. SSH uses the service account's keys and known_hosts. Exports replace selected Skill directories and preserve unrelated repository content. The target branch must not exist. If repository creation succeeds but pushing fails, the error includes the created repository URL for recovery.

## Examples

```bash
curl -b console.cookies http://localhost:52010/api/skills/import \
  -H 'Content-Type: application/json' \
  -d '{"channel":"git","conflict":"replace","options":{"url":"https://git.example.com/team/repo.git","branch":"main","path":"skills"}}'

curl -b console.cookies http://localhost:52010/api/skills/export \
  -H 'Content-Type: application/json' \
  -d '{"channel":"zip","names":["demo"]}' -o demo.zip
```

## Results and errors

Import results include `imported`, `skipped`, and `errors`. Imported entries include `name`, `created`, `status` (created/updated/unchanged), `version`, `files`, `tree_sha256`, and non-secret `origin` metadata. Inspect `errors` even on HTTP 200: storage commits are per Skill, not transactional across a batch. `errors[].stored=true` means the version was committed but the local cache needs retrying. Existing SkillHub partial-write semantics apply to backend failures.

ZIP export returns a download. Remote exports return `exported`; Git also returns `url`, `branch`, and `commit`. Remote writes are not automatically retried.

HTTP errors: 400/422 invalid input, 403 admin required, 404 missing Skill, 409 conflict, 413 size limits, 429 transfer capacity, 502 remote/storage error, 503 Git unavailable, 504 Git timeout. Blocking I/O runs in worker threads; requests wait for completion with two transfer slots per process. Credentials are not persisted in URLs, cache configuration or results. Live marketplace publishing and hosted repository creation require deployment network access and permissions.
