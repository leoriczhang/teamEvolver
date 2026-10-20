# Replay and Experiment Workbench API

True Replay uses the tenant-bound Replay Adapter to drive the customer's real Agent through the same task on Baseline and Candidate branches; TeamEvolver handles the Checklist judge, simulated user feedback and the efficiency comparison. Concepts are in [True Replay](../concepts/06-true-replay.md).

Code entry points:

- Adapter control plane: `team_replay/api/adapters.py:create_router`
- Datasets: `team_replay/api/datasets.py:create_router`
- Replay runs: `team_replay/api/runs.py:create_router`
- Experiment workbench: `team_skills/api/workbench.py:create_router`
- Runtime interface: `team_replay/hooks.py`; source validation and loading: `team_replay/adapters/loader.py`

All endpoints require a tenant administrator; source read/write and tests require the service Root Key.

## Replay Adapter

A Replay Adapter is a tenant-supplied Python file whose source is versioned in PostgreSQL, with the binding in `adapter_bindings` (`binding_key='replay'`). Each run pins the revision current at acceptance.

### File Requirements

- The file name matches `[A-Za-z0-9][A-Za-z0-9_-]*.py` and the content is 1 byte to 256 KiB.
- Exactly one top-level `build_replay_adapter(config)` function, returning a factory that implements `open(context)`.
- A top-level literal dict `REPLAY_ADAPTER`: `label` (text, ≤160 characters), `enabled` (bool, default true).

```python
from team_replay.hooks import AgentObservation

REPLAY_ADAPTER = {"label": "Customer Agent", "enabled": True}


def build_replay_adapter(config):
    return CustomerFactory(config)


class CustomerFactory:
    def __init__(self, config):
        self.config = config

    def open(self, context):
        # Create an independent session and workspace for this branch, apply context.treatment,
        # and return an object implementing send(user_message) -> AgentObservation and close().
        raise NotImplementedError
```

Adapters run inside the Worker process. AST validation only checks structure and is not a sandbox, so source can only be written with the Root Key.

`config` is the frozen `replay` module configuration, passed to the factory as a `SimpleNamespace`; use `vars(config)` to obtain a dictionary. Connection settings belong in that module configuration. The binding endpoint only selects a source file and revision.

### Runtime Interface

| Object | Fields / methods |
| --- | --- |
| `ReplayContext` | `request_id`, `runtime_type`, `treatment` (`branch` is `baseline` or `candidate`, `skill` is the branch's Skill bundle), `materials`, `context_snapshot`, `timeout_seconds`, `source_session` (frozen source Session), `source_turn_num` (source turn, default 1) |
| `ReplaySession` | `send(user_message) -> AgentObservation`, `close()` |
| `AgentObservation` | `response` (required), `messages`, `artifacts`, `metrics`, `trace_id`, `metrics_incomplete_reason` |

The first task and later user feedback are both passed through `send()`, and the Adapter keeps the conversation continuous. Each branch must use its own session and workspace with identical model, tools, initial materials, context and limits; the only difference is the treatment. When isolation cannot be guaranteed, raise `team_replay.hooks.ReplayUnsupported`.

`metrics` only contains values actually observed by the runtime (such as `tool_call_count`, `total_tokens`); missing items are recorded as unavailable and never filled with zero. No turn ever passes the Checklist or judge verdict to the Adapter.

The repository provides two per-turn transports that expose `call_turn(request) -> result` (protocol `teamevolver.replay-turn-request.v1` / `-result.v1`, see `team_replay/protocol.py`); an Adapter's `send()` can delegate to them:

| Transport | Location | Purpose |
| --- | --- | --- |
| `TurnBasedReplayAdapter` | `team_replay/adapters/__init__.py` | Calls an HTTP endpoint implementing the per-turn protocol |
| `MappedHttpAdapter` | `team_replay/adapters/__init__.py` | Calls a customer's own HTTP Agent through a request template and response mapping |

Transport API keys are read from `TEAMEVOLVER_AGENT_<PROFILE>_REPLAY_API_KEY` (`<PROFILE>` is the upper-cased `auth_profile`). `scripts/replay_turn_server.py` is an Agent-side example server for the per-turn protocol: `python scripts/replay_turn_server.py --port 8010 --api-key <secret>`, with endpoint `POST /turn/<runtime_type>`.

### Single-file AgentsHub Integration

Upload `team_replay/adapters/examples/agentshub.py`, bind `agentshub.py`, and set the tenant's `replay` configuration:

```json
{
  "timeout_seconds": 600,
  "max_interactions": 4,
  "agentshub": {
    "backend_root": "/opt/AgentsHub/backend",
    "tenant_id": "demo",
    "agent_id": "agent-example",
    "user_id": "user-example",
    "systemd_scope": "system"
  }
}
```

Every execution Worker needs an accessible AgentsHub backend and Python environment; `python` can override `backend_root/.venv/bin/python`, and `pi_command` can provide an absolute Pi launcher path instead of relying on the Worker's PATH. Recorded Session identity takes priority over configuration defaults. Using the installed AgentsHub Harness and isolation utilities, the Adapter starts one private SQLite database, model broker, systemd worker and workspace per branch. Later `send()` calls resume the same Session, and `close()` removes the isolated process group and temporary directory. AgentsHub needs no new HTTP endpoint.

`systemd_scope` defaults to user systemd. Setting it to `system` requires permission to run `sudo -n systemd-run/systemctl`; the isolated Agent still runs as the current ordinary user. Isolation failure stops Replay. UTF-8 Skill bundle files are preserved; unsupported binary Skill files produce an error. TeamEvolver owns judging and user feedback. Tool traces and Token usage are reported as turn deltas; missing usage remains unavailable.

### Endpoints

| Method | Path | Permission | Description |
| --- | --- | --- | --- |
| GET | `/api/replay-adapter` | Tenant administrator | Current binding, available Adapters and `source_editable` |
| PUT | `/api/replay-adapter` | Tenant administrator | `{"file": "customer.py", "revision": 2}` binds; an empty `file` unbinds; omitting revision takes the latest |
| GET | `/api/replay-adapter/code?file=customer.py` | Root Key | Read source; omitting `file` reads the current binding |
| PUT | `/api/replay-adapter/code` | Root Key | `{"file", "code", "expected_revision"}`; `expected_revision` is `null` for a new file, conflicts return 409 |
| POST | `/api/replay-adapter/code/test` | Root Key | `{"file", "code", "context", "query"}`, returns 202 |

In test requests `context` only allows `runtime_type` (required), `skill` (object or null), `materials` (array), `context_snapshot` (object) and `timeout_seconds` (1–120, default 30); `query` is 1–32000 characters. Test drafts are saved as separate Adapters named `test-<sha256>.py`, do not affect the current binding, and read no published data.

## Datasets

The dataset document format is `team-replay.dataset`:

| Field | Description |
| --- | --- |
| `dataset_id`, `name`, `description` | Identity and description |
| `skills[]` | Related Skill set, each `{"skill_id", "revision"?, "content_hash"?}`, with unique `skill_id` |
| `cases[]` | Replay Cases: `case_id`, `query` (required), `skill_ids`, `checks`, `materials`, `provenance`, `replay`, `metadata` |
| `source`, `metadata` | Origin (manual or Session selection) and extra information |

Baseline and Candidate must resolve to the same Skill set; an experiment can replace one or more of those Skills.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/datasets?search=&limit=100&offset=0` | List |
| POST | `/api/datasets` | Create (201). Send a full document, or `{"name", "description", "session_ids" / "filters"}` to generate Cases from Sessions |
| GET | `/api/datasets/{dataset_id}?search=&limit=&offset=` | Detail, Cases paged up to 500 |
| PATCH | `/api/datasets/{dataset_id}` | Change `name`, `description`, `skills`, `cases`, optionally with `expected_revision` |
| DELETE | `/api/datasets/{dataset_id}` | Delete |
| POST | `/api/datasets/{dataset_id}/cases` | Append Cases from Sessions |
| GET / PATCH / DELETE | `/api/datasets/{dataset_id}/cases/{case_id}` | Single Case |
| GET | `/api/datasets/{dataset_id}/export` | Export the document |
| POST | `/api/datasets/{dataset_id}/runs` | Run Replay with the dataset's current Skill versions, returns 202 |
| GET | `/api/datasets/{dataset_id}/runs` | Run list |
| GET | `/api/datasets/{dataset_id}/runs/{run_id}` | Run detail |
| GET | `/api/datasets/{dataset_id}/runs/{run_id}/cases/{case_id}` | Case as pinned by the run |
| GET | `/api/datasets/{dataset_id}/runs/{run_id}/results/{case_id}` | Single Case result |
| POST | `/api/datasets/{dataset_id}/runs/{run_id}/cancel` | Cancel the run |

## Replay Runs

`POST /api/replays/{domain}`, where `domain` is `skill` or `memory`, returns 202:

| Field | Required | Description |
| --- | --- | --- |
| `subject_id` | Yes | Identifier of the Skill or Memory under evaluation |
| `dataset` | Yes | Inline dataset document |
| `baseline` / `candidate` | Yes | Skill: `{"revision", "payload"}`; Memory: `{"revision", "uri", "content"}`, and both branch URIs must match |
| `runtime` | Yes | `{"type": "<runtime_type>", ...}` |
| `options` | No | `concurrency` (1–2), `timeout_seconds` (30–1800), `max_interactions` (1–20) |

```json
{"status": "queued", "run_id": "skill:...", "job_id": "skill:...", "status_url": "/api/jobs/skill:..."}
```

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/replays/{domain}/{run_id}` | Run status and results |
| GET | `/api/replays/{domain}/{run_id}/input` | `spec`, `job` and `input_hash` pinned at acceptance |

Case materials are read and pinned at acceptance. Repeated requests with the same `Idempotency-Key` return the same `run_id`.

## Experiment Workbench

The experiment workbench edits team Skills in the console and runs Replay against datasets. Skill reads and writes go directly to the team Skill library in PostgreSQL.

| Method | Path | Description |
| --- | --- | --- |
| GET | `/api/replay-lab/workspace`, `/api/replay-lab/skills` | Team Skill index |
| GET | `/api/replay-lab/skills/{name}` | Current `SKILL.md`, file list and sha256 |
| POST | `/api/replay-lab/skills/{name}/save` | `{"candidate_skill_md", "expected_sha256"}` saves a new version; a sha mismatch returns 409 |
| GET | `/api/replay-lab/workspace/{name}/file?path=` | Read a bundle file |
| PUT | `/api/replay-lab/workspace/{name}/file` | `{"path", "content", "expected_sha256"}` writes a file (≤2 MiB); `expected_sha256` is `null` for new files |
| GET / POST | `/api/replay-lab/datasets` | List (filterable by `skill_id`) / save datasets |
| POST | `/api/replay-lab/datasets/{dataset_id}/clone` | Copy a dataset; the body must be empty |
| DELETE | `/api/replay-lab/datasets/{dataset_id}` | Delete a dataset |
| POST | `/api/replay-lab/datasets/synthesize` | `{"skill_id", "session_ids"?}` synthesizes Cases from up to 30 Sessions, returns 202 |
| GET | `/api/replay-lab/jobs/{job_id}/result` | Synthesis result |
| POST | `/api/replay-lab/runs` | `{"skill_id", "dataset_id", "candidate_skill_md", "candidate_files"?, "timeout_seconds"?, "max_interactions"?}`, returns 202 |
| GET | `/api/replay-lab/runs?skill_id=&limit=` | Run history |
| GET | `/api/replay-lab/runs/{run_id}` | Run detail |

A workbench run uses the Skill versions declared by the dataset as Baseline, and as Candidate replaces `skill_id` with `candidate_skill_md` and `candidate_files`. When a `content_hash` in the dataset does not match the version content, the run is refused.

## Errors

| Status | Cause |
| --- | --- |
| 400 | Missing or extra fields, invalid file name or source, options out of range |
| 403 | Not a tenant administrator, or a source endpoint called without the Root Key (`service Root Key required for Python source access`) |
| 404 | Adapter, dataset, Case or run does not exist |
| 409 | revision / sha256 conflict |
| 413 | Body exceeds 96 MiB |
| 503 | Tenant module configuration missing |
