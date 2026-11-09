"""Central True Replay adapter, using the public Replay contract and gateway."""
from __future__ import annotations

import asyncio
import copy
from contextlib import nullcontext
from dataclasses import replace

from team_replay._util import stable_hash
from team_replay.contracts import CHECKLIST_FIRST_POLICY, skill_replay_spec
from team_replay.datasets.candidate_selection import dataset_groups
from team_replay.evaluation import (
    EXTERNAL_AGENT_SCORE_POLICY,
    evaluation_policy_hash,
    external_evaluation_enabled,
    external_score_decision,
    normalize_evaluation,
    valid_external_result,
)
from team_replay.policy import select_replay_cases
from team_replay.runner import aggregate_results
from team_replay.runtime_matrix import evaluate_runtime_compatibility
from team_skills.execution.costs import summarize_usage
from teamEvolver.llm_usage import collect_llm_usage


def _external_runtime_results(results, evaluation):
    reports = {}
    for result in results:
        runtime = str(result.get("runtime_type") or "").strip().lower().replace("_", "-")
        if runtime:
            reports.setdefault(runtime, []).append(
                result.get("external_evaluation") if valid_external_result(result, evaluation) else None,
            )
    return [
        {"runtime_type": runtime, "status": "evaluated", **external_score_decision(values, evaluation)}
        for runtime, values in reports.items()
    ]


class TrueReplayValidator:
    def __init__(self, gateway):
        self.gateway = gateway

    async def __call__(self, job: dict, config, *, cached=None, on_case=None) -> dict:
        evaluation = normalize_evaluation(config.get("evaluation"))
        external = external_evaluation_enabled(evaluation)
        spec = skill_replay_spec({**job, "evaluation": evaluation}, account_id=str(config.get("account_id", "default")))
        spec = replace(spec, evaluation=evaluation,
                       policy_id=EXTERNAL_AGENT_SCORE_POLICY if external else CHECKLIST_FIRST_POLICY, metadata={
            **spec.metadata, "dataset_hash": stable_hash(job.get("replay_cases", [])),
            "baseline_revision": spec.baseline.revision, "candidate_hash": stable_hash(spec.candidate.payload),
        })
        if not spec.cases:
            return {"verdict": "inconclusive", "runtime_gate": {"status": "inconclusive"}}
        saved = dict(cached or {})
        semaphore = asyncio.Semaphore(min(2, max(1, int(config.get("concurrency", 2)))))
        selected = select_replay_cases(job.get("replay_cases") or [])

        async def execute(window, index):
            case = spec.cases[index]
            case_id = case["case_id"]
            if case_id in saved:
                result = saved[case_id]
                if (not external and not external_evaluation_enabled(result.get("evaluation"))) or (external and
                    valid_external_result(result, evaluation, case)
                    and (not job.get("candidate_execution_plan") or all(
                        result["cases"][0][branch].get("execution_verification") == "verified"
                        for branch in ("baseline", "candidate")
                    ))
                ):
                    return window, result
                # Checkpoints are immutable; a changed policy needs a new explicit run.
                return window, {
                    "status": "failed", "case_id": case_id, "case_count": 1,
                    "verdict": "inconclusive", "accepted": False, "cases": [],
                    "reason": "cached_evaluation_policy_unavailable",
                    "runtime_type": case.get("provenance", {}).get("source_runtime", {}).get("type", ""),
                    "evaluation": evaluation, "evaluation_policy_hash": evaluation_policy_hash(evaluation),
                }
            async with semaphore:
                budget = (job.get("validation_plan") or {}).get("probe_policy", {}).get("max_total_tokens")
                scope = collect_llm_usage("replay-case:" + case_id, budget) if job.get(
                    "candidate_execution_plan"
                ) else nullcontext(None)
                with scope as meter:
                    try:
                        result = await asyncio.to_thread(
                            self.gateway.evaluate, spec, case_index=index,
                            timeout_seconds=min(1800, max(30, int(config.get(
                                "timeout_seconds", config.get("replay_timeout_seconds", 600),
                            )))),
                            max_interactions=min(20, max(1, int(config.get(
                                "max_interactions", config.get("replay_max_interactions", 4),
                            )))),
                        )
                    except Exception as exc:
                        result = {
                            "status": "failed", "case_id": case_id, "case_count": 1,
                            "verdict": "inconclusive", "accepted": False, "cases": [],
                            "reason": f"{type(exc).__name__}: {exc}",
                            "runtime_type": case.get("provenance", {}).get("source_runtime", {}).get("type", ""),
                        }
                        if external:
                            result.update(
                                evaluation=evaluation, evaluation_policy_hash=evaluation_policy_hash(evaluation),
                            )
                if meter is not None:
                    result["te_llm_usage"] = meter.snapshot()
                    result["replay_te_llm_accounting"] = "platform-provider-attempts-v1"
                provenance = case.get("provenance") or {}
                for pair in result.get("cases") or []:
                    pair.update(
                        case_id=case_id, query=case["query"], provenance=copy.deepcopy(provenance),
                        dataset_id=provenance.get("dataset_id") or "current",
                        dataset_name=provenance.get("dataset_name") or "当次数据集",
                        selection=provenance.get("replay_selection") or "current",
                    )
                    for branch in ("baseline", "candidate"):
                        pair.setdefault(branch, {}).update(
                            instruction=case["query"], session_id=provenance.get("session_id", ""),
                            turn_num=provenance.get("turn_num"),
                        )
                if on_case is not None:
                    await on_case(case, result)
                return window, result

        results = await asyncio.gather(*(execute(window, index) for window, index in selected))
        runtime_results = [result for _, result in results]
        summary = aggregate_results(results)
        policy = copy.deepcopy(job.get("runtime_validation_policy") or {})
        if not policy.get("required_runtimes"):
            policy["required_runtimes"] = sorted({
                str(case.get("provenance", {}).get("source_runtime", {}).get("type") or "")
                for case in spec.cases
            } - {""})
        gate = evaluate_runtime_compatibility(
            policy, _external_runtime_results(runtime_results, evaluation) if external else runtime_results,
        )
        succeeded = sum(
            valid_external_result(result, evaluation) if external else (
                result.get("status") == "evaluated"
                and bool(result.get("cases"))
                and all(case.get(branch, {}).get("completed") is True
                        for case in result["cases"] for branch in ("baseline", "candidate"))
            )
            for result in runtime_results
        )
        by_case = {spec.cases[index]["case_id"]: result for (_, index), (_, result) in zip(selected, results)}
        datasets = []
        for group in dataset_groups(spec.cases):
            ids = set(group["case_ids"])
            outputs = [(case_id, by_case[case_id]) for case_id in group["case_ids"]]
            dataset_result = aggregate_results(outputs)
            datasets.append({
                **group, "total": len(ids), "completed": len(outputs), "result": dataset_result,
                "inputs": [copy.deepcopy(dict(case)) for case in spec.cases if case["case_id"] in ids],
            })
        return {
            **summary, "verdict": summary.get("verdict") or "inconclusive", "runtime_gate": gate,
            "total": len(selected), "completed": len(results), "succeeded": succeeded,
            "failed": len(results) - succeeded,
            "datasets": datasets, "sampling": copy.deepcopy(job.get("replay_sampling") or {}),
            **({"te_llm_usage": summarize_usage([r.get("te_llm_usage") for r in runtime_results]),
                "replay_te_llm_accounting": "platform-provider-attempts-v1" if all(
                    r.get("replay_te_llm_accounting") == "platform-provider-attempts-v1" for r in runtime_results
                ) else "unverified"}
               if job.get("candidate_execution_plan") else {}),
        }
