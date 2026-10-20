"""Central True Replay adapter, using the public Replay contract and gateway."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

from team_replay._util import stable_hash
from team_replay.contracts import skill_replay_spec
from team_replay.datasets.candidate_selection import dataset_groups
from team_replay.policy import select_replay_cases
from team_replay.runner import aggregate_results
from team_replay.runtime_matrix import evaluate_runtime_compatibility


class TrueReplayValidator:
    def __init__(self, gateway):
        self.gateway = gateway

    async def __call__(self, job: dict, config, *, cached=None, on_case=None) -> dict:
        spec = skill_replay_spec(job, account_id=str(config.get("account_id", "default")))
        spec = replace(spec, metadata={
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
                return window, saved[case_id]
            async with semaphore:
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
        gate = evaluate_runtime_compatibility(policy, runtime_results)
        succeeded = sum(
            result.get("status") == "evaluated"
            and bool(result.get("cases"))
            and all(case.get(branch, {}).get("completed") is True
                    for case in result["cases"] for branch in ("baseline", "candidate"))
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
        }
