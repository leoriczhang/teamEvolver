"""Synthetic frozen histories and literal source/log evidence for cost-only tests."""

from __future__ import annotations

import copy
import hashlib

from team_replay._util import stable_hash
from team_skills.execution.reconciliation import ASSERTIONS, draft_cost_reconciliation

ADAPTER_SOURCE = 'REPLAY_ADAPTER = {"enabled": True}\ndef build_replay_adapter(config):\n    return None\n'


def usage(identity=None, tokens=0):
    attempts = (
        []
        if identity is None
        else [{"attempt_id": identity, "status": "succeeded", "usage_known": True, "usage": {"total_tokens": tokens}}]
    )
    return {
        "complete": True,
        "unknown_attempts": 0,
        "attempts": attempts,
        "attempt_count": len(attempts),
        "known_total_tokens": tokens,
    }


def known_output(prefix="primary", *, left=10000, right=20103, judge=17076):
    return {
        "replay_te_llm_accounting": "platform-provider-attempts-v1",
        "te_llm_usage": usage(prefix + "-judge", judge),
        "verdict": "inconclusive",
        "runtime_gate": {"status": "inconclusive"},
        "cases": [
            {
                "case_id": "primary-case",
                "baseline": {"total_tokens": left, "observed_requests": [{"dispatch_id": prefix + "-left"}]},
                "candidate": {"total_tokens": right, "observed_requests": [{"dispatch_id": prefix + "-right"}]},
            }
        ],
    }


def fixture_context(*, tenant_id="alpha", candidate_id="candidate-prompt", run_id="validate-old"):
    adapter = {
        "adapter_id": "reconciliation.py",
        "revision": 1,
        "source": ADAPTER_SOURCE,
        "source_hash": stable_hash(ADAPTER_SOURCE),
    }
    pinned = {key: adapter[key] for key in ("adapter_id", "revision", "source_hash")}
    cases = [
        {"case_id": "aa-one", "query": "First observable task", "skill_ids": []},
        {"case_id": "aa-two", "query": "Second observable task", "skill_ids": []},
    ]
    job = {
        "job_id": candidate_id,
        "candidate_revision": 3,
        "replay_adapter": pinned,
        "baseline_execution_plan": {"binding": {"agent_id": "customer"}},
        "replay_cases": cases,
    }
    probe_job = {
        **copy.deepcopy(job),
        "job_id": candidate_id + ":aa",
        "diagnostic_kind": "aa",
        "diagnostic_run_id": run_id + ":aa",
    }
    error = "HTTP 429 open http://agent/api/agents/customer/replay-sessions"
    unsupported = {"ok": False, "status": "unsupported", "error_code": "REPLAY_UNSUPPORTED", "error": error}
    aa = {
        "cases": [
            {"case_id": c["case_id"], "baseline": copy.deepcopy(unsupported), "candidate": copy.deepcopy(unsupported)}
            for c in cases
        ],
        "te_llm_usage": usage(),
        "replay_te_llm_accounting": "platform-provider-attempts-v1",
        "verdict": "inconclusive",
        "runtime_gate": {"status": "unsupported"},
    }
    primary = known_output()
    primary["probe_gate"] = {
        "status": "inconclusive",
        "reasons": ["probe_unverified:aa"],
        "runs": [{"kind": "aa", "result": copy.deepcopy(aa)}],
    }
    main_input, probe_input = {"job": job}, {"job": probe_job}
    main = {
        "tenant_id": tenant_id,
        "run_id": run_id,
        "input": main_input,
        "input_hash": stable_hash(main_input),
        "result": {"replay": primary},
        "finished_at": "frozen",
    }
    probe = {
        "tenant_id": tenant_id,
        "run_id": run_id + ":aa",
        "input": probe_input,
        "input_hash": stable_hash(probe_input),
        "result": aa,
        "finished_at": "frozen",
    }
    checkpoints = {
        c["case_id"]: {"case_hash": stable_hash(c), "cases": [copy.deepcopy(pair)]}
        for c, pair in zip(cases, aa["cases"], strict=True)
    }
    return {
        "candidate": {"candidate_id": candidate_id},
        "main": main,
        "probe": probe,
        "adapter": adapter,
        "checkpoints": checkpoints,
    }


def reviewed_proof(context):
    proof = draft_cost_reconciliation(context)
    for branch in proof["branches"]:
        branch["phase"] = "pair_open_aborted"
    files = {
        "engine_before": (
            b"# fixture: open both branches before any send\nopen_baseline(); open_candidate()\nrun_branches()\n"
        ),
        "engine_after": (
            b"# fixture: open both branches before any send\nopen_baseline(); open_candidate()\nrun_branches()\n"
        ),
        "runtime_before": (
            b"# fixture: open is capacity-checked before construction; never sends\n"
            b"check_capacity()\ncreate_session()\n"
        ),
        "runtime_after": (
            b"# fixture: open is capacity-checked before construction; never sends\n"
            b"check_capacity()\ncreate_session()\n"
        ),
        "http_log": b"2026-10-28T15:21:24.630 POST /api/agents/customer/replay-sessions 429\n"
        b"2026-10-28T15:21:25.162 POST /api/agents/customer/replay-sessions 429\n",
    }
    proof["evidence"] = {
        role: {
            "ref": "fixture/" + role,
            "sha256": hashlib.sha256(raw).hexdigest(),
            "line_start": 1,
            "line_end": len(raw.decode().splitlines()),
        }
        for role, raw in files.items()
    }
    proof["http_events"] = [
        {
            "timestamp": line.split()[0],
            "phase": "open",
            "method": "POST",
            "path": "/api/agents/customer/replay-sessions",
            "status": 429,
            "log_line_start": index,
            "log_line_end": index,
            "excerpt_hash": stable_hash(line),
        }
        for index, line in enumerate(files["http_log"].decode().splitlines(), 1)
    ]
    proof["review"] = {
        "reviewer": "independent-admin",
        "verified": True,
        "assertions": dict.fromkeys(ASSERTIONS, True),
        "notes": "Synthetic source and full attempt logs reviewed; both opens precede all sends.",
    }
    return proof, files


def cost_context(context):
    main = context["main"]
    return {
        "tenant_id": main["tenant_id"],
        "candidate_id": main["input"]["job"]["job_id"],
        "run_id": main["run_id"],
        "input_hash": main["input_hash"],
        "result_hash": stable_hash(main["result"]),
    }


def approve(proof):
    approved = stable_hash(proof)
    return lambda actor, proof_hash: actor == "independent-admin" and proof_hash == approved
