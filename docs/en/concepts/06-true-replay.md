# True Replay

True Replay is teamEvolver's core validation mechanism: **executing in parallel** Baseline and Candidate branches in isolated real Agent Runtimes, performing completion judgment and efficiency comparison based on real tool-loop execution results—not simulated tests, text review, or A/B scoring.

True Replay's fundamental principle: **Candidate must prove itself no worse than Baseline in a real runtime**.

## Baseline and Candidate Branches

Each Replay Case launches two independent branches simultaneously; the only variable is whether the Skill Candidate under validation is loaded:

| Branch | Loaded Content | Purpose |
|--------|---------------|---------|
| **Baseline** | Currently published team Skills (or no Candidate loaded) | Control group, establishes real execution baseline for current version |
| **Candidate** | Skill Candidate version under validation | Experimental group, tests execution effect after change |

Both branches share exactly the same:
- User instruction (query/instruction)
- Frozen Context projection (consistency guaranteed via snapshot hash)
- Session Materials (user-uploaded input files)
- Checklist completion conditions
- Execution timeout (timeout_seconds) and maximum interaction turns (max_interactions)
- Model configuration (injected via Replay Model Broker)

Branches do not interfere with each other; each runs complete tool loop independently in its isolated environment.

## Isolated Runtime Execution

True Replay does not execute Agents in main service process; creates one-time isolated runtime environment per branch.

### Local Hermes Sandbox

For local Hermes runtime, each branch gets independent temporary HOME directory:

- `HOME` and `HERMES_HOME` redirected to temporary directory (`/tmp/teamevolver-replay-*/{branch}/`), real `~/.hermes` never touched
- Candidate branch installs Skill Bundle under its private `~/.hermes/skills/{name}/`
- workspace directory is only writable host path
- Config file (`config.yaml`) written to sandbox `.hermes/`, mirrors real model config but API Key replaced with short-lived credentials
- Environment variables frozen: `TERMINAL_ENV=local`, `HERMES_YOLO_MODE=1` (auto-approve, no TTY), `HERMES_INTERACTIVE` and `HERMES_GATEWAY_SESSION` cleared

### systemd Isolation

Local sandbox launched via `systemd-run`, enabling system-level isolation:

| Isolation Dimension | systemd Directive | Description |
|--------------------|-------------------|-------------|
| Network namespace | `PrivateNetwork=yes` | Branch process has no independent network stack, can only connect Model Broker via Unix Socket |
| Filesystem | `ProtectSystem=strict`, `ProtectHome=yes` | System directories read-only, real HOME invisible |
| Writable paths | `ReadWritePaths={sandbox_home}` | Only sandbox directory writable |
| Read-only bind | `BindReadOnlyPaths=...` | Only bind necessary read-only paths (Python interpreter, teamEvolver source, Hermes source, referenced files) |
| Permissions | `NoNewPrivileges=yes`, `RestrictSUIDSGID=yes`, `LockPersonality=yes` | Privilege escalation and SUID prohibited |
| Process | `ProtectProc=invisible`, `ProcSubset=pid` | Process view isolation |
| Address families | `RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6` | Restricted Socket types |
| Timeout | `RuntimeMaxSec={timeout}s` | Forced timeout termination |

> **Note**: Candidate Skill may write to `~/.hermes` and modify config via shell commands; its `~` expands to `$HOME`, so each branch must use independent temporary HOME directory. Referenced source paths (read-only) must exist locally, otherwise the Case marked unrunnable.

### Remote Agent Runtime

For external Agents registering `replay.branch.v1` capability (like Pi Agent), True Replay sends Replay requests to Agent's replay endpoint via HTTP adapter, with Agent Runtime responsible for isolated execution. External Runtimes must implement isolation requirements specified by protocol.

## Frozen Context & Snapshot Hash

True Replay uses Context Snapshot to ensure both branches see identical context view:

1. **Snapshot loading**: Loads frozen Context projection from source Session's `context_usage.context_snapshot_id`
2. **Treatment replacement**: For Skill Replay, removes existing version of validated Skill, injects respective Baseline/Candidate Skill content; for Memory Replay, removes changed Memory entry, injects before/after content
3. **Hash verification**: Computes `shared_context_hash` and `context_input_hash` for each branch's context_snapshot, ensuring shared context consistent between branches, only treatment part differs
4. **Runtime injection**: Frozen Snapshot passed to Agent Runtime via Replay request; Agent must use received Snapshot instead of live context pull

Protocol V1 requires Agent Runtime return `context_input_hash`; mismatch with server-computed hash results in fail-closed.

## Progressive Disclosure Protocol

To avoid overwhelming Agent with requirements causing task deviation, True Replay uses progressive disclosure protocol:

1. **Initial visibility**: First round gives Agent only user's original query (`initial_visibility: query_only`), no Checklist items exposed
2. **Batch disclosure**: After each interaction turn, Checklist Judge evaluates completion, exposes unsatisfied Checklist items in batches (`batch_size=4`) to Agent
3. **Follow-up prompts**: Generates follow-up hints on disclosure (e.g., "Round N Checklist check still has unsatisfied items. Preserve completed content, only address following requirements: …")
4. **Termination conditions**: Stops when all Checklist items satisfied (`all_satisfied=true`); also stops when no more undisclosed items
5. **Disclosure content**: Each item contains `[id]` and specific requirement text

Progressive disclosure ensures Agent first completes task autonomously, then corrects against specific unsatisfied requirements, avoiding initial prompt overload.

## Efficiency Comparison

After Checklist gate passes, efficiency comparison performed between Baseline and Candidate. Comparison strictly follows priority order (lexicographic comparison):

| Priority | Metric | Description |
|----------|--------|-------------|
| 1 (primary) | `interaction_turns` | Interaction turns—fewer turns means more efficient |
| 2 (secondary) | `tool_call_count` | Tool calls—fewer tool calls means less trial-and-error |
| 3 (secondary) | `total_tokens` | Total Token consumption—fewer Tokens means more concise reasoning |

Decision rules:
- **Turns reduced (improved)**: Candidate turns < Baseline → **Accept**
- **Turns increased (regressed)**: Candidate turns > Baseline → **Reject**
- **Turns equal**: Look at tool call count
  - Tool calls reduced and no other metric regression → **Accept**
  - Tool calls increased → **Reject**
- **All metrics tied (inconclusive)**: Neither accept nor reject, marked indeterminate

Efficiency comparison strategy ID is `true_replay_turn_priority_v2`.

> **Note**: This is a conservative strategy—Candidate must clearly outperform Baseline on primary metric, or show net improvement on secondary metrics when primary tied. Metric ties do not count as pass.

## Checklist Gate

Before efficiency comparison, Checklist completion is hard gate:

- **Candidate fails Checklist** (`candidate_checklist_incomplete`): Reject immediately, no efficiency check
- **Candidate passes but Baseline fails** (`candidate_only_completed_checklist`): Accept immediately
- **Both pass**: Enter efficiency comparison
- **Both fail**: Reject

See [Checklist Gate](./07-checklist) for detailed Checklist explanation.

## Team Memory True Replay

DreamCycle-produced team Memory Changes (merged, deduplicated, cleaned memory entry changes) also go through True Replay validation. The validation logic is identical to Skill True Replay, but the Treatment is Memory content instead of a Skill Bundle.

### Differences from Skill True Replay

| Dimension | Skill True Replay | Memory True Replay |
|-----------|-------------------|-------------------|
| Treatment variable | Load Baseline Skill vs Candidate Skill Bundle | Inject before vs after Memory content |
| Baseline source | Currently published team Skill version | Memory Change's `before_content` (pre-change snapshot) |
| Candidate source | Skill Candidate pending release | Memory Change's `after_content` (DreamCycle merge/dedup output) |
| Snapshot replacement | Replace target Skill entry in `skill_bundles[]` | Replace changed Memory entry in `memory_entries[]` |
| Trigger | Skill Evolution pipeline Validate stage | Triggered manually/automatically after DreamCycle completes merge |
| Entry API | `POST /api/validation/candidates/{id}/replay` | `POST /api/dreamcycle/memory-replay` |

### Execution Flow

1. **Load Memory Change**: Read before/after content and associated source Session from `MemoryChangeLedger` for the given `change_id`
2. **Select source Session**: Prefer specified `source_session_id`, otherwise pick a Session with replay capability from the Memory's historical Sessions
3. **Build shared Snapshot**: Load the source Session's frozen Context projection, **exclude** the changed Memory entry (avoid double-write), compute `shared_context_hash`
4. **Inject Treatment**:
   - Baseline branch: inject before_content, compute `before_treatment_hash`
   - Candidate branch: inject after_content, compute `after_treatment_hash`
   - Reject execution if before/after hashes are identical (no actual change)
5. **Parallel execution**: Use `ThreadPoolExecutor(max_workers=2)` to launch both branches simultaneously, reusing Skill True Replay's `spawn_native_agent_branch` runner, Hermes sandbox, systemd isolation, and Model Broker
6. **Progressive disclosure + Checklist gate**: Use identical progressive disclosure protocol and Checklist completion gate as Skill Replay
7. **Efficiency comparison**: `compare_efficiency` performs lexicographic comparison by turns → tool calls → tokens, producing `accepted/rejected/inconclusive` decision
8. **Persist results**: Replay results (baseline/candidate trajectories, Checklist reports, efficiency metrics, final decision) written back to the Memory Change record

### Safety and Consistency Guarantees

- **Hash verification**: Each branch returns `context_input_hash`; mismatch with server-computed `shared_context_hash + treatment_hash` fails closed
- **before/after non-empty check**: If before and after content are identical (same hash), reject meaningless Replay
- **Checklist minimization**: Memory Replay requires at least 1 Checklist item (max 50), query length capped at 32000 characters
- **Runtime selection**: Resolve available Replay endpoint from source Session's `runtime_type`; local Hermes uses sandbox execution, remote Agents (e.g., Pi Agent) use HTTP Replay adapter

Code entry point: `MemoryTrueReplayRunner` in [dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py).

## External Tool Replay (Fail-Closed Strategy)

Real Sessions may call external tools (network requests, database writes, third-party APIs, etc.). True Replay uses **fail-closed** strategy for external tools:

- **Deterministic injectable**: When external tool calls can be injectively matched via normalized tool names, standardized parameter signatures, same-signature call sequences, and result SHA-256, replay recorded tool result
- **Not replayable**: When external tool side effects cannot be deterministically injected into current Runtime, return `REPLAY_EXTERNAL_TOOL_UNSUPPORTED` error, Case marked unrunnable, **does not** fall back to real external calls
- **Workspace-local tools**: File read/write, terminal commands, etc. within workspace executed realistically in sandbox

> **Note**: Matching by tool name alone does not constitute Protocol V1 compliant replay. Must simultaneously match parameter signatures and call sequences. Pi Agent currently declares `external_tool_replay=fail-closed`: workspace-local tools execute in branch sandbox, network capability or external tools make Case unrunnable rather than falling back to live side effects.

## Replay Model Broker

Agents in isolated sandbox need to call LLM but cannot hold model API Key directly. Replay Model Broker provides short-lived credential proxying:

### Architecture

```
┌─────────────────────────────────────────────────┐
│  Sandbox Worker (PrivateNetwork=yes)            │
│                                                 │
│  ┌───────────────┐    ┌──────────────────────┐  │
│  │ Hermes Agent  │───►│ ReplayModelSidecar   │  │
│  │ (config.yaml  │    │ (127.0.0.1:43128,    │  │
│  │  → sidecar)   │    │  loopback only)      │  │
│  └───────────────┘    └──────────┬───────────┘  │
│                                  │ Unix Socket   │
└──────────────────────────────────┼───────────────┘
                                   │ (Unix Stream,
                                   │  0o600 perms,
                                   │  Bearer token)
                                   ▼
                        ┌──────────────────────┐
                        │ ReplayModelBroker    │
                        │ (parent process,     │
                        │  Unix domain server) │
                        └──────────┬───────────┘
                                   │ httpx.stream
                                   │ (with real API key)
                                   ▼
                        ┌──────────────────────┐
                        │ Upstream LLM API     │
                        │ (base_url + api_key) │
                        └──────────────────────┘
```

### Security Properties

- **Key isolation**: Real API Key exists only in parent process (teamEvolver main process); one-time Bearer token (`secrets.token_urlsafe(32)`) used in sandbox Hermes config
- **Unix Socket communication**: Sidecar connects to Broker via Unix Domain Socket, Socket file permissions 0o600, network namespace isolation blocks direct external network access
- **Short-lived lifecycle**: Broker and Sidecar start with Replay branch, close immediately and delete Socket file after branch ends
- **Token authentication**: Sidecar-to-Broker requests must carry correct Bearer token; Broker validates then forwards to upstream LLM with real API Key
- **Streaming pass-through**: Broker passes through upstream responses in streaming mode, does not cache complete responses; Sidecar receives on loopback interface (127.0.0.1)

Broker's worker_base_url returns `http://127.0.0.1:{port}/upstream`; Sidecar listens on that port and forwards to parent Broker via UDS.

## Path Grounding

If instructions reference file paths (absolute paths or repo-relative paths), True Replay verifies path existence before execution:

1. Extract tokens resembling file paths from instructions
2. Check existence directly for absolute paths; search relative paths under search root
3. Uploaded files (Session Materials) mapped to `uploaded://{path}` virtual paths
4. All referenced paths exist → Case marked `runnable`; missing paths → Case marked unrunnable

Referenced paths used to configure systemd `BindReadOnlyPaths`, ensuring sandbox can read these files.

## Branch Result Collection

Each branch outputs upon completion:
- `ok`: Whether successful
- `final_response`: Final response text
- `messages`: Complete message sequence
- `interaction_turns`: Interaction turns
- `tool_call_count`: Tool calls
- `total_tokens`/`input_tokens`/`output_tokens`: Token consumption
- `interactions`: Per-turn interaction details (prompt, response, tool_call_count, checklist_report)
- `checklist_report`: Final Checklist evaluation result
- `workspace_artifacts`: Artifact file list in workspace
- `context_input_hash`: Context hash actually used
- `error`/`error_code`: Failure information

Results aggregated and passed to `progressive_replay_decision` for final decision.

## Code Entry Points

| Module | Path |
|--------|------|
| True Replay core | [true_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/true_replay.py) |
| Progressive disclosure & Checklist decisions | [progressive_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/progressive_replay.py) |
| Replay Model Broker | [integrations/replay_model_broker.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/replay_model_broker.py) |
| Replay HTTP adapters | [integrations/replay_adapters.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/replay_adapters.py) |
| Efficiency metric comparison | [replay_metrics.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/replay_metrics.py) |
| Memory True Replay | [dreamcycle/memory_replay.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/dreamcycle/memory_replay.py) |
| Agent registration & runtime resolution | [integrations/agent_registry.py](file:///home/zhangpengkun/teamEvolver/teamEvolver/integrations/agent_registry.py) |
| Protocol V1 Replay Branch specification | [Protocol V1 Specification](../agent-integrations/02-protocol-v1) |

## Related Documentation

- [Evolution Loop](./02-evolution-loop): True Replay in Validate stage of evolution loop
- [Checklist Gate](./07-checklist): Detailed rules for Checklist as completion gate
- [Session System](./05-sessions): How Sessions provide Replay Cases and Materials
- [Skill System](./03-skills): Skill Candidate vs Baseline relationship
- [Memory System](./04-memory): True Replay validation for Memory Changes
