"""Auditable cumulative usage; unknown cost warns without blocking an uncapped budget."""
from __future__ import annotations

import copy

from team_replay.metrics import metric_number

from .reconciliation_contract import zero_dispatch_matches


def summarize_usage(snapshots):
    records, unknown_scopes, unknown_ids, incomplete, lower_bound = {}, {}, set(), False, 0
    for index, snapshot in enumerate(snapshots):
        if not isinstance(snapshot, dict):
            unknown_scopes[("missing", index)] = 1
            incomplete = True
            continue
        attempts = snapshot.get("attempts") or []
        scope = tuple(sorted(a.get("attempt_id", "") for a in attempts if isinstance(a, dict)))
        key = scope or ("unidentified", index)
        missing = {a.get("attempt_id") for a in attempts if isinstance(a, dict) and (
            a.get("status") != "succeeded" or a.get("usage_known") is not True
            or metric_number((a.get("usage") or {}).get("total_tokens")) is None
        )}
        unknown_ids.update(identity for identity in missing if identity)
        residual = max(0, int(snapshot.get("unknown_attempts") or 0) - len(missing))
        unknown_scopes[key] = max(unknown_scopes.get(key, 0), residual)
        incomplete |= snapshot.get("complete") is not True or bool(snapshot.get("truncated"))
        incomplete |= bool(snapshot.get("unreconciled_worker_attempts"))
        incomplete |= bool(snapshot.get("pending_dispatches")) or bool(snapshot.get("in_flight_attempts"))
        lower_bound = max(lower_bound, metric_number(snapshot.get("known_total_tokens")) or 0)
        for attempt in attempts:
            identity = attempt.get("attempt_id") if isinstance(attempt, dict) else None
            if not identity:
                incomplete = True
                continue
            old = records.get(identity)
            if old is not None and old != attempt:
                incomplete = True
                # Conflicting snapshots cannot prove an exact total. Preserve
                # the greatest observed lower bound without charging one ID twice.
                old_tokens = metric_number((old.get("usage") or {}).get("total_tokens")) or 0
                new_tokens = metric_number((attempt.get("usage") or {}).get("total_tokens")) or 0
                if old_tokens >= new_tokens:
                    continue
            records[identity] = copy.deepcopy(attempt)
        if snapshot.get("attempt_count", 0) != len(attempts):
            incomplete = True
    total = max(lower_bound, sum(
        metric_number((a.get("usage") or {}).get("total_tokens")) or 0 for a in records.values()
    ))
    unknown = len(unknown_ids) + sum(unknown_scopes.values())
    return {"channel": "te_owned_llm", "known_total_tokens": total, "unknown_attempts": unknown,
            "attempt_count": len(records), "complete": not incomplete and unknown == 0,
            "attempts": list(records.values())}


def budget_gate(detail, evaluation, *, worker_attempt=1, prior_evaluations=(), zero_dispatch_receipts=()):
    probes = evaluation.get("probe_gate") or {}
    snapshots = [detail.get("generation_cost"), evaluation.get("te_llm_usage")]
    snapshots.extend(detail.get("generation_cost_history") or [])
    if detail.get("generation_cost_history_truncated"):
        snapshots.append(None)
    snapshots.extend((r.get("result") or {}).get("te_llm_usage") for r in probes.get("runs") or [])
    outcomes = [(evaluation, None, ""), *((r.get("result") or {}, None, r.get("kind", ""))
                                         for r in probes.get("runs") or [])]
    for previous in prior_evaluations:
        snapshots.extend([previous.get("generation_cost"), previous.get("te_llm_usage")])
        context = previous.get("_cost_context")
        outputs = [(r.get("result") or {}, context, r.get("kind", ""))
                   for r in (previous.get("probe_gate") or {}).get("runs") or []]
        snapshots.extend(output.get("te_llm_usage") for output, _, _ in outputs)
        outcomes.extend([(previous, context, ""), *outputs])
    te = summarize_usage(snapshots)
    replay_accounting_known = all(
        output.get("replay_te_llm_accounting") == "platform-provider-attempts-v1" for output, _, _ in outcomes
    )
    dispatches, agent_known, reconciled = [], True, set()
    for output, context, probe_kind in outcomes:
        if output.get("cost_history_complete") is False:
            agent_known = False
        pairs = output.get("cases") or []
        if not pairs:
            agent_known = False
        for pair in pairs:
            for branch in ("baseline", "candidate"):
                observed = pair.get(branch) or {}
                tokens = metric_number(observed.get("total_tokens"))
                ids = [r.get("dispatch_id") for r in observed.get("observed_requests") or []]
                if tokens is None or not ids or any(not isinstance(v, str) or not v for v in ids):
                    matching = [r for r in zero_dispatch_receipts if zero_dispatch_matches(
                        r, context=context, probe_kind=probe_kind, output=output, pair=pair, branch=branch,
                    )]
                    if matching:
                        r = matching[0]
                        reconciled.add((r.run_id, r.case_id, r.branch, r.proof_hash, r.event_seq))
                    else:
                        agent_known = False
                else:
                    identity = set(ids)
                    overlap = [(keys, value) for keys, value in dispatches if keys & identity]
                    if overlap:
                        if len(overlap) != 1 or overlap[0][0] != identity or overlap[0][1] != tokens:
                            agent_known = False
                        for keys, value in overlap:
                            dispatches.remove((keys, value))
                        tokens = max(tokens, sum(value for _, value in overlap))
                        identity.update(set().union(*(keys for keys, _ in overlap)))
                    dispatches.append((identity, tokens))
    agent = sum(tokens for _, tokens in dispatches)
    budget = detail["validation_plan"]["probe_policy"]["max_total_tokens"]
    accounting_reasons = []
    if not te["complete"] or not agent_known or not replay_accounting_known:
        accounting_reasons.append("total_model_cost_unknown")
    if not replay_accounting_known:
        accounting_reasons.append("replay_te_llm_accounting_unverified")
    if worker_attempt > 1:
        accounting_reasons.append("worker_retry_cost_unreconciled")
    accounting_complete = not accounting_reasons
    warnings = accounting_reasons if budget is None else []
    reasons = [] if budget is None else list(accounting_reasons)
    total = te["known_total_tokens"] + (agent or 0)
    if budget is not None and total > budget:
        reasons.append("total_model_cost_exceeds_budget")
    return {"status": "passed" if not reasons else "inconclusive", "reasons": reasons,
            "warnings": warnings, "accounting_status": "complete" if accounting_complete else "incomplete",
            "total_tokens": total if accounting_complete else None,
            "known_total_tokens": total, "budget_tokens": budget, "te_llm_usage": te,
            "agent_replay_tokens": agent, "agent_cost_complete": agent_known,
            "replay_te_llm_accounting_complete": replay_accounting_known,
            "prior_validation_runs": len(prior_evaluations),
            "historical_zero_dispatch_reconciliations": [
                {"run_id": run, "case_id": case, "branch": branch, "proof_hash": proof, "event_seq": seq}
                for run, case, branch, proof, seq in sorted(reconciled)
            ],
            "scope": "generation_case_synthesis_replay_judges_feedback_provider_retries"}
