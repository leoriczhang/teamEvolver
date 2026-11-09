"""Read persisted costs of interrupted historical Replay without completing it."""
from __future__ import annotations

import copy

from team_replay._util import stable_hash
from team_replay.datasets.repository import DatasetNotFound

from .costs import summarize_usage
from .probes import probe_jobs

_ACCOUNTING = "platform-provider-attempts-v1"


def _cost_pairs(pairs):
    """Keep actual cost identities; never manufacture completion or quality fields."""
    result = []
    for pair in pairs:
        value = {"case_id": pair.get("case_id")}
        for branch in ("baseline", "candidate"):
            observed = pair.get(branch) or {}
            value[branch] = {
                **({"total_tokens": observed["total_tokens"]} if "total_tokens" in observed else {}),
                "observed_requests": [{"dispatch_id": row.get("dispatch_id")}
                                      for row in observed.get("observed_requests") or []],
            }
        result.append(value)
    return result


def _finished_cost(output):
    output = output.get("replay", output)
    return {"cases": _cost_pairs(output.get("cases") or []), **{
        key: copy.deepcopy(output[key]) for key in ("te_llm_usage", "replay_te_llm_accounting") if key in output
    }}


async def _checkpoint_cost(conn, repository, run_id, *, expected_cases):
    checkpoints = await repository.cases(conn, "skill", run_id)
    usage = summarize_usage([case.get("te_llm_usage") for case in checkpoints.values()])
    # Complete checkpoints only account for their recorded attempts. A missing
    # aggregate result cannot prove that a cancelled attempt made no other calls.
    usage["complete"] = False
    value = {
        "cases": _cost_pairs([pair for case in checkpoints.values() for pair in case.get("cases") or []]),
        "te_llm_usage": usage,
        "cost_history_complete": False,
        "partial_cost_history": {
            "run_id": run_id,
            "checkpoint_hashes": {key: stable_hash(case) for key, case in checkpoints.items()},
            "missing_case_ids": sorted(set(expected_cases) - set(checkpoints)),
            "unpersisted_usage": "unknown",
        },
    }
    if checkpoints and all(case.get("replay_te_llm_accounting") == _ACCOUNTING for case in checkpoints.values()):
        value["replay_te_llm_accounting"] = _ACCOUNTING
    return value


def _expected_cases(job):
    return [case["case_id"] for case in job.get("replay_cases", []) if isinstance(case, dict) and "case_id" in case]


def _frozen_probes(job):
    policy = (job.get("validation_plan") or {}).get("probe_policy") or {}
    # These are the accepted policy bounds, not a suffix search across a tenant.
    for key, lower, upper in (("max_extra_runs", 2, 8), ("max_probe_cases", 1, 8), ("repetitions", 2, 3)):
        if type(policy.get(key)) is not int or not lower <= policy[key] <= upper:
            return []
    try:
        variants, reasons = probe_jobs(job)
    except (KeyError, TypeError, ValueError):
        return []
    return [] if reasons else variants


async def historical_cost_output(conn, repository, saved):
    """Return complete history unchanged, otherwise a read-only partial cost view.

    This result is an input to cost aggregation only. It must never be persisted
    as a Replay/domain result or used as a quality/verification decision.
    """
    if saved["result"] is not None:
        return saved["result"]
    job = (saved.get("input") or {}).get("job") or {}
    output = await _checkpoint_cost(conn, repository, saved["run_id"], expected_cases=_expected_cases(job))
    runs = []
    for variant in _frozen_probes(job):
        kind = variant["diagnostic_kind"]
        run_id = saved["run_id"] + ":" + kind
        try:
            probe = await repository.get(conn, "skill", run_id)
        except DatasetNotFound:
            # It might have dispatched without a durable result. Absence is not zero.
            value = {"cases": [], "te_llm_usage": summarize_usage([None]), "cost_history_complete": False}
        else:
            value = (_finished_cost(probe["result"]) if probe["result"] is not None else
                     await _checkpoint_cost(conn, repository, run_id, expected_cases=_expected_cases(variant)))
        runs.append({"kind": kind, "result": value})
    output["probe_gate"] = {"runs": runs}
    return output
