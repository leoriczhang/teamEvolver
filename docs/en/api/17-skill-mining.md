# Skill Mining API

These console-internal interfaces manage real OpenViking Compile rounds and human Evidence supplements. They require a valid console Session Cookie and existing mining administration permissions. Tenant machine credentials cannot call these management interfaces. The Agent data-plane protocol is unchanged.

Execution and synchronization live in the [main-service bridge](../../../team_miner/bridge.py); local jobs, rounds and supplements live in the [job repository](../../../team_miner/ov_compile_mining.py). See [Multi-round Skill Mining](../guides/17-multi-round-skill-mining.md) for the workflow.

## Interfaces

| Method | Path | Purpose |
| --- | --- | --- |
| POST | `/api/mining/jobs` | Create a job and submit its first Compile round |
| GET | `/api/mining/jobs` | List jobs and summary counts |
| GET | `/api/mining/jobs/{job_id}` | Read rounds, artifacts, logs and checkpoint state |
| POST | `/api/mining/jobs/{job_id}/supplements` | Stage attachments at the current checkpoint |
| POST | `/api/mining/jobs/{job_id}/continue` | Save supplements and start the next round |
| POST | `/api/mining/jobs/{job_id}/finish` | Save final review and finish mining |

## Create a job

The main fields of `POST /api/mining/jobs` are:

| Field | Type | Meaning |
| --- | --- | --- |
| `name` | string | Job name, at most 120 characters |
| `source_id` | string | Registered source reference tagged for Skill use |
| `input_dir` | string | Selected OpenViking Resources directory URI |
| `max_rounds` | integer | Actual round limit, 1–5, default 3 |
| `human_checkpoints` | boolean | Wait for human review after each round, default true |
| `runtime_timeout_seconds` | integer | Runtime limit for each round, 60–7200 seconds, default 3600 |

The first round freezes the original source. Subsequent rounds use that snapshot, accumulated human supplements and previous revision context. Jobs return `rounds_mode: "orchestrated"`; `current_round` represents an actual round.

The remote Compile request's `from` contains only this round's Evidence source directory. Previous candidate revision context is delivered directly through `instruction` rather than extracted as another source. Its JSON is limited to 30000 characters and contains the previous Skill body, case identifiers and current review information, with explicit excerpt or omission flags. The full previous artifacts remain in the round archive.

## Stage attachments

`POST /api/mining/jobs/{job_id}/supplements`:

```json
{
  "checkpoint_id": "<current pending_checkpoint.id>",
  "files": [
    {"name": "clarification.md", "content_base64": "IyBDbGFyaWZpY2F0aW9uCg=="}
  ]
}
```

Attachments are saved locally within the job and frozen into the next round's inputs on continuation. The response includes `ok`, `saved` and the updated `job`; `saved` lists names and references. Staging does not directly modify the original OpenViking source or generated artifacts.

Each upload accepts 1–10 files, with at most 20 staged attachments per round. Original files are limited to 10 MB each and 20 MB per upload. The round's converted Markdown attachments are limited to 8 MB in total.

## Continue or finish

`POST /api/mining/jobs/{job_id}/continue` and `/finish` accept the same review fields:

| Field | Type | Meaning |
| --- | --- | --- |
| `checkpoint_id` | string | Must match the current `pending_checkpoint.id` |
| `answers` | object | Mapping of question `qid` to answer text |
| `unresolved` | string[] | Question identifiers explicitly left unresolved |
| `references` | object | Mapping of question `qid` to arrays of OpenViking source file URIs, at most 20 per question |
| `feedback` | string | Review feedback for this round's artifacts, at most 20000 characters |
| `request_id` | string | Client request identifier; retain it when retrying the same operation |

Required questions need an answer or an explicit entry in `unresolved`; the same question cannot be answered and marked unresolved. Answers and feedback together are limited to 100000 characters. Source references must be readable Resources text files, at most 2 MB each. Previous artifacts and revision context cannot serve as Evidence sources. Unknown questions, stale checkpoints and conflicting parameters in duplicate requests are rejected.

Example using a Cookie file from an authenticated console session:

```bash
curl -X POST 'http://localhost:52010/api/mining/jobs/<job_id>/continue' \
  -b '<console-cookie-file>' \
  -H 'Content-Type: application/json' \
  -d '{
    "checkpoint_id": "<checkpoint-id>",
    "answers": {"q1": "Use the billing rule stated in the source."},
    "unresolved": [],
    "references": {"q1": ["viking://resources/team/source/rules.md"]},
    "feedback": "Correct this rule in the Skill Candidate and Test Dataset.",
    "request_id": "review-round-1-continue"
  }'
```

`continue` freezes supplements and submits another round; continuation is rejected after `max_rounds`. `finish` ends mining and retains review notes and unresolved questions. It does not automatically run Compile again or Publish.

## Response state and recovery

Job details include status, `current_round`, a `rounds` list, the current `pending_checkpoint`, artifacts and quality information. A checkpoint contains `id`, `round`, `questions` and `attachments`. Questions contain `qid`, `question`, `reason`, `source_refs` and `required`.

`remote_warnings` preserves OpenViking partial-output and source-processing warnings, which are also included in `artifact_quality.warnings`. Warning-bearing results have quality `caution`. Each round retains its warnings and `compiler_sha256`. Remote `completed` and seven structurally valid artifacts do not prove complete Evidence coverage.

`compiler_source_sha256` identifies the frozen compiler source body. `compiler_sha256` identifies the actual installed body after package-name substitution and native formatting normalization. `compiler_uri` and `skill_uri` point to its separate package, and round archives retain these fields. An existing package with different contents is rejected rather than overwritten with another version.

| Status | Meaning |
| --- | --- |
| `preparing` | Preparing or recovering this round's inputs and submission |
| `queued` | Compile was accepted and awaits execution |
| `running` | Executing, synchronizing or materializing this round |
| `waiting_human` | Round completed; awaiting human supplements or final review |
| `stopping` | Cancellation requested; awaiting the remote terminal state |
| `succeeded` | Mining finished; final artifacts can enter subsequent validation |
| `failed` / `stopped` / `interrupted` | Failed, stopped or interrupted; inspect job details |

Every round has its own deadline. `waiting_human` consumes neither runtime allowance nor an execution slot. Unknown acceptance is recovered using the same round's original idempotency key and request. A new round uses a new key. Restarting the service does not increment the round count or resubmit accepted tasks.

## Errors

Errors return an HTTP status and a `detail` description. Common cases are:

| Status | Situation | Action |
| --- | --- | --- |
| 400 | Invalid job creation or attachment request | Correct the request |
| 401 / 403 | Not authenticated or lacking mining administration permissions | Use an authorized console identity |
| 404 | Missing job or source | Query a valid reference |
| 409 | Not waiting for review, stale checkpoint, unknown or unanswered required questions, round limit reached or request conflict | Refresh details, correct answers or explicitly mark questions unresolved |
| 413 | Attachment exceeds the allowed size | Reduce upload content |
| 429 | All 8 execution slots are occupied | Retry after existing work finishes |
| 502 | OpenViking source reading is temporarily unavailable | Check existing jobs and retry once connectivity recovers |

Creation and continuation persist local state before preparing remote inputs and submitting Compile in the background. Inspect job `error`, `submission_error` or synchronization information for background failures. A successful HTTP response does not mean remote Compile execution has started.

Shared Test Dataset validation checks structure rather than business facts. Evidence gaps require review even when the format is valid. Final artifacts still require True Replay and Candidate Review.
