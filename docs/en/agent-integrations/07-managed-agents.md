# Managed Agents Integration

Configure Volcengine Managed Agents (MA) under Console Settings and Integration → MA Integration. An entry Agent may be a single Agent or a coordinator with arbitrary roles. TE reads, edits, publishes and rolls back Skills using native MA cloud endpoints. The connection Worker discovers members and native versions and collects Sessions. Business applications need no Session SDK.

## Configure a Connection

Provide the MA API URL, entry Agent ID, Environment ID, business Vaults and current tenant key; default attribution is optional. MA and tenant keys are encrypted as Secret Refs after entry; responses only report presence. Production MA URLs use HTTPS.

Discovery recursively reads members and custom Skills while retaining preset references. Native `latest_version` supplies current contents; `bound_version` / `bound_versions` separately record actual member references. Discovery imports no team-library versions and uploads no old package. Verified observation sets `ready`. Same-name Skills across connections require `connection_id`. Names, roles and models are independent of CodingVideo.

Send the Session tag as `{"key":"te-user","value":"<user_id>"}`; it takes precedence over channel identity and the default user and is normalized to `te-user:<user_id>` on read. MA rejects arrays of tag strings. Without a tag, the first main user message's `ChannelContext` establishes the initiating sender; later participants remain attached to their turns. OpenIDs are stored as `provider.app_id.sender_open_id`. Names come only from channel headers for the same identity; otherwise the identity ID is displayed. Channel messages missing a sender are marked unknown rather than assigned to the connection operator. Ordinary messages retain recorded default ownership. Titles use the actual request body without channel headers, reading policies or an initial Bot routing mention; original messages are retained. The next collection repairs existing attribution and titles as metadata, preserving analysis, artifacts and timestamps without replay. The console shows business event and pull timestamps. Multiple ranges or semantic splits from one parent Session still count as one independent Evidence source.

Collection waits for the parent and all child threads, including child events after parent idle. Each turn's `agent_threads` retains child identity, delegated requirements, responses, all tool calls and results, actual Skill usage and event IDs. Session analysis and separate Trace analysis consume this evidence. Child findings carry `parent_trace_id` and `session_thread_id`; used Skills join the parent Session's evolution groups without increasing the number of independent Sessions. Native `skill({name: ...})` calls and actual tool paths under `/skills/<id-or-name>/`, `/workspace/skills/<name>/` and `runtime_binding.paths` establish usage; mounts and catalog descriptions alone do not. Previously collected projections re-enter mandatory analysis on the next pull after upgrading and retain their formal Session IDs.

Long Sessions use bounded evidence summaries while retaining every tool entry and child identity. Parent projections reference child tool bodies instead of resending them. Every batch must return all coverage IDs before final Session judging; a failed batch prevents archival. Original events and full content remain in PostgreSQL. Trace analysis also batches by byte budget and requires an explicit verdict for every parent and child Trace. Dataset synthesis and oversized evolution Agent contexts share this evidence protocol.

Invalid summary format, coverage IDs or output length retries only that chunk, up to three attempts with increasing reasoning output budgets. Trace judging uses an explicit `expected_trace_identities` manifest; parent turn numbers cannot replace child analysis indices. Invalid verdicts retry only the current batch, and three failed attempts fail the entire analysis without partial archival or invented normal findings. The Trace stage reads the current tenant's Prompt Studio settings. Successful Session analysis is saved as an immutable stage output and reused after a Trace failure or Worker restart; all mandatory stages must still succeed before archival.

Session lists distinguish actual usage from the available catalog: “实际使用 N / 可用 M”. Available Skills come from the configured runtime snapshot; actual use requires parent or child tool evidence. A catalog of 15 Skills does not establish 15 used Skills in each Session.

## Enable Isolated Replay

`default_user_id` is optional. Sessions without a `te-user` tag, channel identity or explicit default attribution receive a stable `unknown.*` identity and display “未知提交人” (unknown submitter). Do not infer a name from conversation text or treat the connection operator as the submitter. Explicit defaults remain supported and are labelled as default attribution in the UI. After changing the default, collect to repair existing Session and ingestion attribution while preserving raw events, analysis stages and timestamps; this metadata correction does not require importing Skills again.

Skill management uses MA Secret Refs to call cloud endpoints. Connector identities are never used as business Session submitters.

Each Worker reserves a separate execution slot for MA discovery, collection and synchronization, so long analysis or Replay work cannot exhaust connection capacity. Slots rotate tenants and aggregate health. A database lease still serializes operations on one connection; waiting for that lease defers work without consuming execution attempts. A Session GET returning 404 preserves accepted history, records `unavailable` and allows the batch to continue. Authentication and service errors retain connection-level failure handling.

Enable isolated MA True Replay in the console without entering Replay Environment or Vault IDs. Saving queues a Worker to prepare separate resources and reuse them on subsequent discoveries; the console shows status and read-only IDs. API callers can likewise omit `replay_environment_id` / `replay_vault_ids`. Existing explicitly configured separate IDs remain compatible. Production Environment/Vault IDs cannot be used for Replay.

Automatic Environments retain business dependencies and setup without copying production TOS output storage. Automatic Vaults can inject the trusted MA Key as `ARK_API_KEY`. Other Vault secrets are write-only: the console lists missing credentials, including `OPENVIKING_API_KEY` for an independent OpenViking runtime. Add them to the Replay Vault and run discover again. Keys for different services are never substituted. Separate resources do not establish isolation of every external tool; MCP and custom tool hosts still require confirmation. Cloud environments support frozen complete configuration. Persistent Memory without a reproducible snapshot, shared production writable directories and unisolated tools block Replay. Collection remains available; comparability is not claimed.

Baseline and Candidate use the same pinned entry Agent, Environment configuration, Runtime and materials in separate Sessions/sandboxes. A single Agent only overrides `agent_with_overrides.skills`; other fields inherit the pinned version. Multi-Agent replay clones only affected child members and ancestors, retaining unaffected pinned references. Session readback records the actual Agent, Environment, override fields and configuration/runtime hashes for inspection. Each branch keeps its Session across interactions and preserves creative confirmation. Tool confirmation requiring human intervention fails explicitly. Actual usage, tool events, materials and artifacts are saved in PostgreSQL. ffprobe/ffmpeg inspect media and sampled images reach an independent judge. Missing metrics or key artifacts yield unavailable / inconclusive.

Target Skill replacement covers all original references in the historical graph and current explicitly associated members, preventing conflicting versions of the same Skill across parent and child Agents. Skill Replay fixes Runtime to the recorded connection revision and historical runtime Skill bytes; Candidate runtime files are not installed. Mounted runtimes use xz embedding of the same bytes and tree hash in both branches. Independent OpenViking runtimes use the recorded artifact directly without downloading a historical Skill. Automatic evolution cannot revise Candidates during validation. Explicit administrator edits still advance the revision and invalidate older results.

MA root-thread replies also carry `session_thread_id`. Replay uses the actual root identity to extract the main response and project evaluation messages, retaining child-thread messages separately. Archived protocol events preserve their original thread identity.

Input materials are checked for size and SHA-256, then mounted with their original bytes and paths in the isolated Session. The first task message provides the mounted paths for native file/media tools. Images and audio are not coerced into model `document` blocks, avoiding PDF-only restrictions on pinned models. Replay preserves upstream error codes from model `session.error` events for troubleshooting.

MA may register output files after tool completion. Replay reads declared outputs from native Write/Edit paths and final `<display>` references, waits up to 60 seconds for downloadable bytes before judging and cleanup, and fails explicitly if they remain unavailable. A successful Write message alone does not establish artifact delivery.

Temporary resources have external intent records before creation. Unknown outcomes are reconciled first, and cleanup uses precise ledger ownership. Original business Sessions and resources remain available.

Changing only the display name or collection/synchronization intervals reuses discovery evidence; credential, runtime and isolation changes still require discovery. Human confirmation is detected on root and child threads, and `session.error` is reported without waiting for idle. Failed final-history archival still runs cleanup while preserving the archival error. Retired ledger entries do not consume the cleanup query window.

If the last tool in the runtime readiness probe explicitly fails, Replay reports that failure instead of continuing to wait for proof. Even when the first `send` fails, the branch retains the actual Session snapshot and `runtime_diagnostics` with observed usage, child-thread status and the final event archive reference. Diagnostic usage does not replace comparable Replay metrics or establish task completion.

`runtime_tree_sha256` is the pinned expected tree. `runtime_ready_verified` becomes true only after downloading and checking the actual ready file, with its `runtime_ready_proof` archive reference. A configuration hash alone does not establish a successful runtime mount.

## Publishing and Runtime

Administrators review validation results before Publish. Version history, edits, imports, creation, publication, rollback and deletion use native MA endpoints. New Skills can be created unattached before selecting members. Writes return `202` and a `ma.skill` Job; the Worker uploads a native version, updates associated members and ancestors and verifies readback before marking published. Rollback uploads historical contents as a new version. Deletion detaches, preserves checked historical packages, then deletes native versions and the Skill resource with individual effect receipts. Business Sessions and TE validation snapshots remain.

Candidates freeze the cloud resource identity and baseline files. Changed cloud heads reject stale publication. MA uploads expose no CAS; a raced upload remains in cloud history and publication pauses for review. Unknown network outcomes are reconciled before resending. TE retains immutable validation snapshots, Candidates, tasks and audit, without a second current MA version chain. Reference updates preserve unrelated System, model and tool edits and historical business Sessions.

`GET /sync/ma/{connection_id}?user_id=...` uses a tenant key to return the last fully applied release and ETag. Partial synchronization retains the previous release; initial unavailability returns 503. Applications with specialized runtimes create new Sessions with the fixed entry version and matching Environment; existing Sessions keep their snapshots.

Optional `runtime_binding` selects a runtime Skill, Bundle-to-workspace paths, working directory, bootstrap and ready file for a pinned isolated Replay runtime. Replay verifies the ready file before sending the business task. Production Environments use their existing MA configuration; Skill discovery does not overwrite it. See the [MA API](../api/24-managed-agents.md).

Release records expose a stable business name as `environment_alias` alongside the `environment_id` and actual MA name. New release Environments use `<business-alias>-<12-character-digest>` as their actual names; unchanged configurations reuse the current Environment. Existing Environments are neither renamed nor deleted, and historical Sessions and Replays retain their original snapshots.

`archive_compression` defaults to `gzip`; explicitly select `xz` to reduce setup script size. Both formats extract identical Bundle bytes and preserve the tree hash. The `xz` decoder uses Python's standard-library `lzma`, with no external xz command required. Some MA environments return 500 before the documented script limit, so Environment creation still requires verified readback.

`source: "openviking"` pins an independent runtime artifact so the Skill can contain only instructions and contracts. Its `artifact` includes an HTTPS `base_url`, raw-download `uri`, archive `sha256`, `bytes`, and a `files` manifest with `path/sha256/bytes` per file. `paths` maps artifact files into the workspace, and the fixed manifest determines the runtime tree hash. MA downloads tar.xz using Vault-injected `OPENVIKING_API_KEY`, rejects authenticated redirects, bounds download bytes/time, and verifies every regular file's path, size and hash in staging before installation and bootstrap. The sandbox cache is keyed by archive hash and verified on each reuse; valid cache works offline, corrupt cache triggers a fresh download. Ready proof includes the artifact URI, archive hash and cache status. Synchronization and Replay use the same installer.

`pre_setup` installs fixed application prerequisites before the original setup. `replace_setup_script:true` explicitly initializes from the Bundle instead of embedding the old runtime twice; the default preserves the original script. CodingVideo installs Node 22.23.3 with official checksums because MA currently defaults to Node 20. Real creation returned 500 with a 66 KiB script and succeeded with the deduplicated 33 KiB script; the documented maximum remains 256 KiB.

Optional `runtime_binding.packages` merges runtime dependencies into MA `packages.apt/pip`, preserving source packages and deduplicating complete specifications. MA prepares them before running setup. CodingVideo declares pinned NumPy, SciPy, librosa and imageio-ffmpeg versions to avoid resolving and downloading large packages from the public network in every setup script. The pre-setup script retains bounded fallback installation and actual version/binary hash checks. Declaration changes create a new connection revision and Environment; historical Replay keeps its original declarations.

Outputs register after tool execution; registered append-only logs may be stale, so inspect current startup progress through a real tool read of sandbox files. Optional `ready_probe_message` reads runtime files in the same isolated Session, then the adapter downloads the actual ready proof before starting the business task. Both branches use the same probe and include its measured usage. Acceptance freezes current connection and Replay infrastructure while source Agent graph, model and execution Environment remain historical facts.

## CodingVideo Example

`examples/managed-agents/codingvideo.json` reads the single entry Agent and core Skill resources from the deployment file; actual counts come from cloud discovery. First run `python -m scripts.publish_runtime` in CodingVideo to produce the verified artifact descriptor, then configure a team-evolver Root Key or console login. Replay Environment/Vault IDs are prepared automatically; missing runtime credentials are listed in connection status.

```bash
python scripts/connect_managed_agent.py plan --config examples/managed-agents/codingvideo.json
python scripts/connect_managed_agent.py apply --config examples/managed-agents/codingvideo.json
python scripts/connect_managed_agent.py status --config examples/managed-agents/codingvideo.json
```

`apply` creates `codingvideo-ma` idempotently and keeps existing keys. New keys are written only to the explicit 0600 `credential_output` file. Credentials come from variables named by `secret_env`, never CLI arguments or example JSON. Existing connections use revision updates and queued discover operations. Jobs show imports and model tests.

The example changes only its own tenant: two real Sessions and one user can provide team Evidence; Evolution / Evaluate are automatic and Publish remains manual. Replay has a 1800-second guard, a 20-interaction guard and concurrency one. Efficiency is compared only after both Checklists complete; hitting a guard while incomplete remains inconclusive. Apply the example configuration to update existing tenants; already accepted tasks retain their frozen limits. CodingVideo media preapproval and code-rendered formal visuals remain in force.

With `CODINGVIDEO_TEAMEVOLVER_URL`, `CODINGVIDEO_TEAMEVOLVER_CONNECTION_ID`, `CODINGVIDEO_TEAMEVOLVER_USER_ID` and private `CODINGVIDEO_TEAMEVOLVER_TENANT_KEY` configured, CodingVideo deployment adopts release mappings and new Sessions pin the entry and Environment. See CodingVideo's `docs/team-evolver-integration.md`.

## Connect Another MA

`examples/managed-agents/generic.json` has no CodingVideo paths or specialized runtime. Set `MA_TENANT_ID`, `MA_CONNECTION_ID`, `MA_ENTRY_AGENT_ID`, `MA_ENVIRONMENT_ID`, `MA_USER_ID`, `MA_MODEL_ID` and `MA_API_KEY` in a private environment file. Existing tenants also need `MA_TENANT_KEY`; newly issued keys are written to `.env.ma` in the current directory with mode 0600. Control-plane authentication uses the existing Root Key or console login environment variables.

```bash
python scripts/connect_managed_agent.py plan --config examples/managed-agents/generic.json --env-file .env.ma
python scripts/connect_managed_agent.py apply --config examples/managed-agents/generic.json --env-file .env.ma
python scripts/connect_managed_agent.py status --config examples/managed-agents/generic.json --env-file .env.ma
```

The template starts collection and Candidate generation. Evidence-based experience review is available
without isolated Replay. Candidate execution and full A/B require `replay_enabled`; the Worker prepares
separate Environment/Vault resources, while missing credentials and external tool isolation still require attention. Keep private environment files out of version control.
Alternatively, copy the CodingVideo example and replace tenant, connection, entry, Environment/Vault,
default user and credential variable names. Remove `runtime_binding` when no specialized runtime exists.
`$env` reads environment variables; `$json` with `pointer` reads existing deployment files; `$text` reads
a nonsecret setup script. Plain fields accept nonsecret IDs. Omitting `skills`, `replay` or `model`
preserves the tenant's existing policies.

Pausing stops new collection and synchronization while accepted tasks keep their fixed revisions. Current remote versions remain. Inspect connection errors, drift and cleanup records before enqueueing reconcile. Supported providers are limited to the same Volcengine MA API.
