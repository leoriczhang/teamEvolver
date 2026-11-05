# Managed Agents Integration

Configure Volcengine Managed Agents (MA) under Console Settings and Integration → MA Integration. An entry Agent may be a single Agent or a coordinator with arbitrary roles. The connection Worker discovers pinned members and custom Skills, imports the team library, collects Sessions, and updates MA by pull after administrator Publish. Business applications need no Session SDK.

## Configure a Connection

Provide the MA API URL, entry Agent ID, Environment ID, business Vaults, default user and current tenant key. MA and tenant keys are encrypted as Secret Refs after entry; responses only report presence. Production MA URLs use HTTPS.

Discovery recursively reads members and every custom Skill. Preset Skills remain attached. Identical names and tree hashes bind without creating a version; conflicting content stops takeover and requires a separate tenant. Status becomes `ready` after every import finishes. Names, roles and models are independent of CodingVideo.

Send the Session tag as `{"key":"te-user","value":"<user_id>"}`; it takes precedence over the default user and is normalized to `te-user:<user_id>` on read. MA rejects arrays of tag strings. Fallback ownership is recorded. The console shows business event and pull timestamps. Multiple ranges or semantic splits from one parent Session still count as one independent Evidence source.

Collection waits for the parent and all child threads, including child events after parent idle. Each turn's `agent_threads` retains child identity, delegated requirements, responses, all tool calls and results, actual Skill usage and event IDs. Session analysis and separate Trace analysis consume this evidence. Child findings carry `parent_trace_id` and `session_thread_id`; used Skills join the parent Session's evolution groups without increasing the number of independent Sessions. Native `skill({name: ...})` calls and actual tool paths under `/skills/<id-or-name>/`, `/workspace/skills/<name>/` and `runtime_binding.paths` establish usage; mounts and catalog descriptions alone do not. Previously collected projections re-enter mandatory analysis on the next pull after upgrading and retain their formal Session IDs.

Long Sessions use bounded evidence summaries while retaining every tool entry and child identity. Parent projections reference child tool bodies instead of resending them. Every batch must return all coverage IDs before final Session judging; a failed batch prevents archival. Original events and full content remain in PostgreSQL. Trace analysis also batches by byte budget and requires an explicit verdict for every parent and child Trace. Dataset synthesis and oversized evolution Agent contexts share this evidence protocol.

Invalid summary format, coverage IDs or output length retries only that chunk, up to three attempts with increasing reasoning output budgets. Trace judging uses an explicit `expected_trace_identities` manifest; parent turn numbers cannot replace child analysis indices. Invalid verdicts retry only the current batch, and three failed attempts fail the entire analysis without partial archival or invented normal findings. The Trace stage reads the current tenant's Prompt Studio settings. Successful Session analysis is saved as an immutable stage output and reused after a Trace failure or Worker restart; all mandatory stages must still succeed before archival.

Session lists distinguish actual usage from the available catalog: “实际使用 N / 可用 M”. Available Skills come from the configured runtime snapshot; actual use requires parent or child tool evidence. A catalog of 15 Skills does not establish 15 used Skills in each Session.

## Enable Isolated Replay

Explicitly configure `replay_enabled`, a separate `replay_environment_id` and `replay_vault_ids`, and confirm Vault, MCP and custom tool host isolation. Cloud environments support frozen complete configuration. Persistent Memory without a reproducible snapshot, shared production writable directories and unisolated tools block Replay. Collection remains available; comparability is not claimed.

Baseline and Candidate create separate complete temporary Agent graphs and Sessions; Candidate replaces only the target Bundle. Each branch keeps its Session across interactions and preserves creative confirmation. Tool confirmation requiring human intervention fails explicitly. Actual usage, tool events, materials and artifacts are saved in PostgreSQL. ffprobe/ffmpeg inspect media and sampled images reach an independent judge. Missing metrics or key artifacts yield unavailable / inconclusive.

Temporary resources have external intent records before creation. Unknown outcomes are reconciled first, and cleanup uses precise ledger ownership. Original business Sessions and resources remain available.

## Publishing and Runtime

Administrators review each managed connection's results in Candidate Review before Publish. New Skills require explicit target members; existing Skills retain their associations. Connections read complete `GET /sync/skills` manifests, reuse or upload versions by file tree hash, update associated members and ancestors, and verify readback. Managed reference drift stops writes; unrelated System, model and tool edits are retained.

`GET /sync/ma/{connection_id}?user_id=...` uses a tenant key to return the last fully applied release and ETag. Partial synchronization retains the previous release; initial unavailability returns 503. Applications with specialized runtimes create new Sessions with the fixed entry version and matching Environment; existing Sessions keep their snapshots.

Optional `runtime_binding` selects a runtime Skill, Bundle-to-workspace paths, working directory, bootstrap and ready file. The same Bundle produces deterministic setup content and a new Environment with a fixed tree hash, subject to a 256 KiB script limit. Replay verifies the ready file before sending the business task. See the [MA API](../api/24-managed-agents.md).

`archive_compression` defaults to `gzip`; explicitly select `xz` to reduce setup script size. Both formats extract identical Bundle bytes and preserve the tree hash. The `xz` decoder uses Python's standard-library `lzma`, with no external xz command required. Some MA environments return 500 before the documented script limit, so Environment creation still requires verified readback.

`pre_setup` installs fixed application prerequisites before the original setup. `replace_setup_script:true` explicitly initializes from the Bundle instead of embedding the old runtime twice; the default preserves the original script. CodingVideo installs Node 22.23.3 with official checksums because MA currently defaults to Node 20. Real creation returned 500 with a 66 KiB script and succeeded with the deduplicated 33 KiB script; the documented maximum remains 256 KiB.

Real MA setup runs before configured Python packages are visible, so the example installs fixed dependencies explicitly. Setup outputs register after tool execution. Optional `ready_probe_message` reads runtime files in the same isolated Session, then the adapter downloads the actual ready proof before starting the business task. Both branches use the same probe and include its measured usage. Acceptance freezes current connection and Replay infrastructure while source Agent graph, model and execution Environment remain historical facts.

## CodingVideo Example

`examples/managed-agents/codingvideo.json` reads resource IDs for 3 Agents and 15 custom Skills from the deployment file; actual counts come from cloud discovery. First configure environment variables for an isolated Replay Environment/Vault and a team-evolver Root Key or console login.

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

The template starts collection and Candidate generation; validation cannot pass without configured isolated Replay. Enable the complete loop by adding `replay_enabled`, a separate Environment/Vault and isolation declarations. Keep private environment files out of version control. Alternatively, copy the CodingVideo example and replace tenant, connection, entry, Environment/Vault, default user and credential variable names. Remove `runtime_binding` when no specialized runtime exists. `$env` reads environment variables; `$json` with `pointer` reads existing deployment files; `$text` reads a nonsecret setup script. Plain fields accept nonsecret IDs. Omitting `skills`, `replay` or `model` preserves the tenant's existing policies.

Pausing stops new collection and synchronization while accepted tasks keep their fixed revisions. Current remote versions remain. Inspect connection errors, drift and cleanup records before enqueueing reconcile. Supported providers are limited to the same Volcengine MA API.
