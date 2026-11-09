"""Verified historical zero-dispatch facts affect cost only, never Replay acceptance."""

from __future__ import annotations

from dataclasses import dataclass

from team_replay._util import stable_hash


@dataclass(frozen=True)
class VerifiedZeroDispatch:
    tenant_id: str
    candidate_id: str
    run_id: str
    input_hash: str
    result_hash: str
    probe_run_id: str
    probe_result_hash: str
    case_id: str
    case_hash: str
    branch: str
    observation_hash: str
    phase: str
    adapter_hash: str
    proof_hash: str
    event_seq: int


def zero_dispatch_matches(receipt, *, context, probe_kind, output, pair, branch):
    """Only a domain-verified, observation-bound receipt can explain an old unknown."""
    if not isinstance(receipt, VerifiedZeroDispatch) or probe_kind != "aa" or not context:
        return False
    observed = pair.get(branch) or {}
    return (
        receipt.event_seq > 0
        and receipt.phase == "pair_open_aborted"
        and receipt.tenant_id == context.get("tenant_id")
        and receipt.candidate_id == context.get("candidate_id")
        and receipt.run_id == context.get("run_id")
        and receipt.input_hash == context.get("input_hash")
        and receipt.result_hash == context.get("result_hash")
        and receipt.probe_run_id == context.get("run_id", "") + ":aa"
        and receipt.probe_result_hash == stable_hash(output)
        and receipt.case_id == pair.get("case_id")
        and receipt.branch == branch
        and receipt.observation_hash == stable_hash(observed)
        and observed.get("status") == "unsupported"
        and observed.get("error_code") == "REPLAY_UNSUPPORTED"
        and not any(
            observed.get(key)
            for key in (
                "interactions",
                "observed_requests",
                "execution_receipts",
                "messages",
                "artifacts",
                "trace_ids",
            )
        )
        and all(
            observed.get(key) is None
            for key in (
                "total_tokens",
                "input_tokens",
                "output_tokens",
                "api_calls",
                "interaction_turns",
            )
        )
    )
