"""Candidate completion and full A/B assessment through the common gateway."""
from __future__ import annotations

import asyncio
import copy
from dataclasses import replace

from team_replay._util import stable_hash
from team_replay.completion import candidate_complete
from team_replay.config import DEFAULT_REPLAY_CONFIG
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
        method = job.get("validation_method", "full_ab")
        lightweight = method == "candidate_execution"

        async def execute(window, index):
            case = spec.cases[index]
            case_id = case["case_id"]
            if case_id in saved:
                result = saved[case_id]
                if any(pair.get(branch, {}).get("status") == "assessment_pending"
                       for pair in result.get("cases") or []
                       for branch in (("candidate",) if lightweight else ("baseline", "candidate"))):
                    rejudge = getattr(self.gateway, "rejudge", None)
                    if rejudge:
                        result = await asyncio.to_thread(rejudge, spec, case_index=index, result=result)
                if on_case is not None and (result.get("reused_from") or result.get("assessment_only")):
                    await on_case(case, result)
                return window, result
            async with semaphore:
                try:
                    result = await asyncio.to_thread(
                        self.gateway.evaluate, spec, case_index=index,
                        timeout_seconds=min(1800, max(30, int(job.get("execution_timeout_seconds") or config.get(
                            "timeout_seconds", config.get("replay_timeout_seconds", 600),
                        )))),
                        max_interactions=min(2 if lightweight else 20, max(1, int(config.get(
                            "max_interactions", config.get(
                                "replay_max_interactions", DEFAULT_REPLAY_CONFIG["max_interactions"],
                            ),
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
                target = (job.get("ma_records") or {}).get(case_id)
                if target:
                    completed = candidate_complete(result) if lightweight else result.get("accepted") is True
                    result["target_validation"] = {
                        target["connection_id"]: {
                            "status": result.get("status"), "accepted": completed,
                            "verdict": "accept" if completed and lightweight else result.get("verdict"),
                            "config_hash": target["config_hash"],
                            "connection_revision": target["connection_revision"],
                            "runtime_hash": target["runtime_hash"],
                            "binding_hash": target["binding_hash"],
                        },
                    }
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

        if lightweight:
            results = []
            # Reuse a matching completion before opening any new Session.
            ordered = sorted(selected, key=lambda x: not candidate_complete(saved.get(spec.cases[x[1]]["case_id"], {})))
            for window, index in ordered:
                result = await execute(window, index)
                results.append(result)
                if candidate_complete(result[1]):
                    break
        else:
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
        completed_candidates = sum(candidate_complete(result) for result in runtime_results)
        by_case = {result.get("case_id", ""): result for result in runtime_results}
        datasets = []
        for group in dataset_groups(spec.cases):
            ids = set(group["case_ids"])
            outputs = [(case_id, by_case[case_id]) for case_id in group["case_ids"] if case_id in by_case]
            dataset_result = aggregate_results(outputs)
            dataset_result["candidate_completed"] = sum(candidate_complete(r) for _, r in outputs)
            datasets.append({
                **group, "total": len(ids), "completed": len(outputs), "result": dataset_result,
                "inputs": [copy.deepcopy(dict(case)) for case in spec.cases if case["case_id"] in ids],
            })
        issues = [violation for result in runtime_results for pair in result.get("cases") or []
                  for violation in (pair.get("candidate", {}).get("checklist_report") or {}).get("rule_violations", [])]
        basic = {"status": "passed" if completed_candidates and not issues else "inconclusive",
                 "scope": job.get("validation_scope", "complete_task"), "issues": issues}
        if lightweight:
            verdict = "reject" if issues else "accept" if completed_candidates else "inconclusive"
            gate = {"status": "passed" if verdict == "accept" else verdict,
                    "reason": "Selected Candidate completion; no assertion about other Runtimes",
                    "target_matrix": {
                        ident: {**entry, "accepted": candidate_complete(result) and not issues}
                        for result in runtime_results
                        for ident, entry in (result.get("target_validation") or {}).items()
                    }}
        else:
            verdict = summary.get("verdict") or "inconclusive"
        return {
            **summary, "verdict": verdict, "accepted": verdict == "accept", "runtime_gate": gate,
            "total": len(selected), "completed": len(results), "succeeded": succeeded,
            "failed": len(results) - (completed_candidates if lightweight else succeeded),
            "datasets": datasets, "sampling": copy.deepcopy(job.get("replay_sampling") or {}),
            "validation_method": method, "validation_scope": job.get("validation_scope", "complete_task"),
            "validation_scope_kind": job.get("validation_scope_kind", "complete_task"),
            "basic_validation": basic, "candidate_completed": completed_candidates,
            "ab_completed": succeeded, "ab_verdict": summary.get("verdict") or "inconclusive",
            "assessment": job.get("assessment"), "issues": issues,
            "mode": "candidate_execution" if lightweight else "true_replay",
        }
