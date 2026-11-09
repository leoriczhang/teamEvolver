"""Append-only admin reconciliation of a proven historical open-before-send rejection.

An HTTP status or a caller's ``verified`` flag is insufficient. The administrator
must supply the actual source/log bytes, their bounded manifest, an explicit
review, and a trusted approval callback covering the complete proof hash. The
ledger is read only for a subsequent run's historical cost, never for quality.
"""

from __future__ import annotations

import copy
import hashlib
import re
from dataclasses import replace

from team_replay._util import stable_hash
from team_replay.adapters.repository import ReplayAdapterRepository
from team_replay.repository import ReplayRepository
from team_skills.candidates.repository import CandidateRepository
from team_skills.library.repository import SkillLibraryError

from .reconciliation_contract import VerifiedZeroDispatch

SCHEMA = "teamevolver.cost-reconciliation.v1"
EVIDENCE_ROLES = frozenset({"engine_before", "engine_after", "runtime_before", "runtime_after", "http_log"})
# before/after denotes reviewed source equivalence, not claimed collection times.
OPTIONAL_EVIDENCE_ROLES = frozenset({"host_runtime", "runtime_identity", "adapter_source", "independent_review"})
ASSERTIONS = frozenset(
    {
        "engine_opens_both_before_any_send",
        "open_endpoint_never_dispatches_model",
        "http_rejection_precedes_any_send",
        "no_branch_send_started",
        "complete_attempt_covered",
        "source_revisions_verified",
        "no_timeout_partial_or_unknown_origin",
    }
)


def _require(condition, message):
    if not condition:
        raise SkillLibraryError("cost reconciliation: " + message)


def _replay(record):
    result = record.get("result") or {}
    return result.get("replay", result)


def _binding(context):
    main, probe, adapter = context["main"], context["probe"], context["adapter"]
    job = main["input"]["job"]
    return {
        "tenant_id": main["tenant_id"],
        "candidate_id": job["job_id"],
        "candidate_revision": job["candidate_revision"],
        "run_id": main["run_id"],
        "input_hash": main["input_hash"],
        "result_hash": stable_hash(main["result"]),
        "probe_kind": "aa",
        "probe_run_id": probe["run_id"],
        "probe_input_hash": probe["input_hash"],
        "probe_result_hash": stable_hash(probe["result"]),
        "adapter_id": adapter["adapter_id"],
        "adapter_revision": adapter["revision"],
        "adapter_hash": stable_hash(adapter["source"]),
    }


async def historical_cost_context(conn, tx, *, candidate_id, run_id):
    """Public Query composition only; repositories never read each other's tables."""
    candidate = await CandidateRepository(tx).require(conn, candidate_id)
    replay = ReplayRepository(tx)
    main = await replay.get(conn, "skill", run_id)
    job = main["input"].get("job") or {}
    _require(job.get("job_id") == candidate_id, "old run belongs to another candidate")
    binding = job.get("replay_adapter") or {}
    _require(set(binding) >= {"adapter_id", "revision", "source_hash"}, "old pinned adapter is unavailable")
    adapter = await ReplayAdapterRepository(tx).source(conn, binding["adapter_id"], binding["revision"])
    probe = await replay.get(conn, "skill", run_id + ":aa")
    checkpoints = await replay.cases(conn, "skill", probe["run_id"])
    return {"candidate": candidate, "main": main, "probe": probe, "adapter": adapter, "checkpoints": checkpoints}


def draft_cost_reconciliation(context):
    """Return hashes to review, with deliberately incomplete evidence/phase assertions."""
    cases = context["probe"]["input"]["job"]["replay_cases"]
    pairs = {p["case_id"]: p for p in _replay(context["probe"]).get("cases") or []}
    return {
        "schema_version": SCHEMA,
        **_binding(context),
        "branches": [
            {
                "case_id": case["case_id"],
                "case_hash": stable_hash(case),
                "branch": branch,
                "observation_hash": stable_hash(pairs[case["case_id"]][branch]),
                "phase": "",
            }
            for case in cases
            for branch in ("baseline", "candidate")
        ],
        "evidence": {},
        "http_events": [],
        "review": {"reviewer": "", "verified": False, "assertions": {}, "notes": ""},
    }


def _verify_evidence(proof, files):
    evidence = proof.get("evidence")
    _require(
        isinstance(evidence, dict)
        and EVIDENCE_ROLES <= set(evidence)
        and set(evidence) <= EVIDENCE_ROLES | OPTIONAL_EVIDENCE_ROLES,
        "complete before/after source and log proof required",
    )
    decoded = {}
    for role, item in evidence.items():
        _require(
            isinstance(item, dict) and set(item) == {"ref", "sha256", "line_start", "line_end"},
            "invalid evidence manifest",
        )
        _require(isinstance(item["ref"], str) and 1 <= len(item["ref"]) <= 2000, "invalid evidence reference")
        _require(
            isinstance(item["sha256"], str) and re.fullmatch("[0-9a-f]{64}", item["sha256"]), "invalid evidence hash"
        )
        _require(
            type(item["line_start"]) is int
            and type(item["line_end"]) is int
            and 1 <= item["line_start"] <= item["line_end"],
            "invalid evidence line range",
        )
        if files is not None:
            raw = files.get(role)
            _require(isinstance(raw, bytes) and 1 <= len(raw) <= 4 * 1024 * 1024, "missing or oversized evidence bytes")
            _require(hashlib.sha256(raw).hexdigest() == item["sha256"], "evidence bytes changed")
            try:
                lines = raw.decode("utf-8").splitlines()
            except UnicodeDecodeError as exc:
                raise SkillLibraryError("cost reconciliation: evidence must be UTF-8") from exc
            _require(item["line_end"] <= len(lines), "evidence range is outside actual file")
            decoded[role] = lines
    return decoded


def verify_cost_reconciliation(proof, *, context, evidence_files=None, actor, approve_review=None, ledger=False):
    """Mint typed facts only after immutable binding, actual evidence and trusted review."""
    _require(isinstance(proof, dict) and len(str(proof)) <= 256 * 1024, "bounded proof object required")
    _require(proof.get("schema_version") == SCHEMA, "unsupported proof schema")
    binding = _binding(context)
    _require(all(proof.get(k) == v for k, v in binding.items()), "run/input/result/adapter binding mismatch")
    main, probe, adapter = context["main"], context["probe"], context["adapter"]
    for record in (main, probe):
        _require(bool(record.get("finished_at")) and bool(record.get("result")), "run is not frozen")
        _require(stable_hash(record["input"]) == record["input_hash"], "immutable Replay input hash mismatch")
    _require(main.get("tenant_id") == probe.get("tenant_id"), "cross-tenant probe binding")
    job, probe_job = main["input"]["job"], probe["input"].get("job") or {}
    _require(
        probe_job.get("diagnostic_kind") == "aa" and probe_job.get("diagnostic_run_id") == probe["run_id"],
        "not the original AA execution",
    )
    _require(
        probe["run_id"] == main["run_id"] + ":aa" and probe_job.get("job_id") == job["job_id"] + ":aa",
        "probe parent mismatch",
    )
    _require(job.get("candidate_revision") == probe_job.get("candidate_revision"), "old candidate revisions differ")
    pinned = job.get("replay_adapter") or {}
    _require(
        pinned == probe_job.get("replay_adapter")
        and pinned.get("source_hash") == binding["adapter_hash"]
        and adapter.get("source_hash") == binding["adapter_hash"],
        "pinned adapter source mismatch",
    )
    expected_probe = [
        r.get("result") for r in _replay(main).get("probe_gate", {}).get("runs", []) if r.get("kind") == "aa"
    ]
    _require(
        len(expected_probe) == 1 and expected_probe[0] == probe["result"], "parent and immutable AA results differ"
    )
    cases = {c["case_id"]: c for c in probe_job.get("replay_cases") or []}
    pairs = _replay(probe).get("cases") or []
    _require(
        bool(cases) and len(cases) == len(probe_job["replay_cases"]) == len(pairs), "partial or duplicate probe Cases"
    )
    pair_by_id = {p.get("case_id"): p for p in pairs}
    _require(set(pair_by_id) == set(cases) == set(context["checkpoints"]), "probe Case/checkpoint coverage differs")
    manifest = proof.get("branches") or []
    _require(isinstance(manifest, list) and len(manifest) == 2 * len(cases), "every old branch must be covered")
    branch_by_id = {(b.get("case_id"), b.get("branch")): b for b in manifest if isinstance(b, dict)}
    _require(
        set(branch_by_id) == {(cid, b) for cid in cases for b in ("baseline", "candidate")}, "duplicate or wrong branch"
    )
    events = proof.get("http_events") or []
    _require(isinstance(events, list) and len(events) == len(cases), "one actual open rejection per Case required")
    lines = _verify_evidence(proof, evidence_files)
    review = proof.get("review") or {}
    _require(
        isinstance(actor, str) and 1 <= len(actor) <= 200 and review.get("reviewer") == actor,
        "trusted reviewer identity mismatch",
    )
    _require(
        review.get("verified") is True
        and review.get("assertions") == dict.fromkeys(ASSERTIONS, True)
        and isinstance(review.get("notes"), str)
        and 10 <= len(review["notes"]) <= 8000,
        "explicit complete source/log review required",
    )
    proof_hash = stable_hash(proof)
    if not ledger:
        _require(
            evidence_files is not None and callable(approve_review) and approve_review(actor, proof_hash) is True,
            "independent trusted approval of complete proof hash required",
        )
    agent = (probe_job.get("baseline_execution_plan") or {}).get("binding", {}).get("agent_id")
    expected_path = f"/api/agents/{agent}/replay-sessions"
    _require(isinstance(agent, str) and bool(agent), "missing frozen Agent identity")
    identities = set()
    for event in events:
        _require(
            isinstance(event, dict)
            and set(event)
            == {
                "timestamp",
                "phase",
                "method",
                "path",
                "status",
                "log_line_start",
                "log_line_end",
                "excerpt_hash",
            },
            "HTTP evidence requires actual events without invented branch mapping",
        )
        timestamp = event.get("timestamp")
        _require(
            isinstance(timestamp, str) and re.fullmatch(r"[0-9T:.+Z -]{12,40}", timestamp),
            "actual HTTP timestamp required",
        )
        _require(
            event.get("phase") == "open"
            and event.get("method") == "POST"
            and type(event.get("status")) is int
            and event["status"] == 429
            and event.get("path") == expected_path,
            "SEND/timeout/mixed/unknown HTTP origin",
        )
        identity = (timestamp, event["method"], event["path"], event["status"])
        _require(identity not in identities, "duplicated HTTP logging is not another attempt")
        identities.add(identity)
        start, end = event.get("log_line_start"), event.get("log_line_end")
        reviewed = proof["evidence"]["http_log"]
        _require(
            type(start) is int and type(end) is int and reviewed["line_start"] <= start <= end <= reviewed["line_end"],
            "HTTP event is outside reviewed log range",
        )
        if evidence_files is not None:
            excerpt = "\n".join(lines["http_log"][start - 1 : end])
            _require(
                stable_hash(excerpt) == event.get("excerpt_hash")
                and all(
                    str(value) in excerpt for value in (timestamp, event["method"], event["path"], event["status"])
                ),
                "actual HTTP log does not support the rejected open",
            )
        else:
            _require(
                isinstance(event.get("excerpt_hash"), str) and re.fullmatch("[0-9a-f]{64}", event["excerpt_hash"]),
                "missing reviewed HTTP excerpt hash",
            )
    receipts = []
    for cid, case in cases.items():
        pair = pair_by_id[cid]
        checkpoint = context["checkpoints"][cid]
        checkpoint_pair = {key: value for key, value in pair.items() if key != "evidence_window"}
        if "evidence_window" in pair:
            window = (_replay(probe).get("window_results") or {}).get(pair["evidence_window"]) or {}
            _require(
                window.get("cases") == checkpoint.get("cases")
                and window.get("case_hash") == checkpoint.get("case_hash"),
                "aggregate evidence window differs from immutable checkpoint",
            )
        _require(
            checkpoint.get("case_hash") == stable_hash(case) and checkpoint.get("cases") == [checkpoint_pair],
            "immutable branch checkpoint differs",
        )
        for branch in ("baseline", "candidate"):
            observed, record = pair.get(branch) or {}, branch_by_id[cid, branch]
            phase = "pair_open_aborted"
            _require(
                record
                == {
                    "case_id": cid,
                    "case_hash": stable_hash(case),
                    "branch": branch,
                    "observation_hash": stable_hash(observed),
                    "phase": phase,
                },
                "branch/hash/phase proof mismatch",
            )
            _require(
                observed.get("status") == "unsupported"
                and observed.get("ok") is False
                and observed.get("error_code") == "REPLAY_UNSUPPORTED",
                "not a pre-execution unsupported branch",
            )
            _require(
                not any(
                    observed.get(key)
                    for key in (
                        "interactions",
                        "observed_requests",
                        "execution_receipts",
                        "messages",
                        "artifacts",
                        "trace_ids",
                        "completed",
                        "execution_verification",
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
                ),
                "known dispatch/partial execution cannot be reconciled to zero",
            )
            # Necessary consistency only. The actual source/log review above is the proof.
            error = str(observed.get("error") or "")
            _require(
                "429" in error and expected_path in error and "/messages" not in error,
                "stored failure is not consistent with the reviewed open rejection",
            )
            receipts.append(
                VerifiedZeroDispatch(
                    binding["tenant_id"],
                    binding["candidate_id"],
                    binding["run_id"],
                    binding["input_hash"],
                    binding["result_hash"],
                    binding["probe_run_id"],
                    binding["probe_result_hash"],
                    cid,
                    stable_hash(case),
                    branch,
                    stable_hash(observed),
                    phase,
                    binding["adapter_hash"],
                    proof_hash,
                    0,
                )
            )
    return receipts


async def record_cost_reconciliation(
    conn,
    tx,
    *,
    tenant_id,
    candidate_id,
    expected_revision,
    expected_state_hash,
    expected_event_seq,
    reconciliation_id,
    proof,
    evidence_files,
    actor,
    approve_review,
):
    """Admin script entry; caller owns the tenant transaction and supplies reviewed bytes."""
    repo = CandidateRepository(tx)
    await repo.require(conn, candidate_id, for_update=True)
    _require(
        proof.get("candidate_id") == candidate_id and proof.get("tenant_id") == tenant_id, "candidate/tenant mismatch"
    )
    context = await historical_cost_context(conn, tx, candidate_id=candidate_id, run_id=proof["run_id"])
    receipts = verify_cost_reconciliation(
        proof,
        context=context,
        evidence_files=evidence_files,
        actor=actor,
        approve_review=approve_review,
    )
    event = await repo.append_cost_reconciliation(
        conn,
        tenant_id=tenant_id,
        candidate_id=candidate_id,
        expected_revision=expected_revision,
        expected_state_hash=expected_state_hash,
        expected_event_seq=expected_event_seq,
        reconciliation_id=reconciliation_id,
        actor=actor,
        proof=copy.deepcopy(proof),
        proof_hash=stable_hash(proof),
        schema_version=SCHEMA,
    )
    return {
        "event_seq": event["seq"],
        "proof_hash": stable_hash(proof),
        "receipts": [replace(r, event_seq=event["seq"]) for r in receipts],
    }


async def historical_zero_receipts(conn, tx, *, candidate_id, run_id):
    """Recheck ledger bindings against immutable public Queries before cost consumption."""
    repo, result = CandidateRepository(tx), []
    events = await repo.cost_reconciliations(conn, candidate_id)
    matching = [e for e in events if (e["detail"].get("proof") or {}).get("run_id") == run_id]
    if not matching:
        return result
    context = await historical_cost_context(conn, tx, candidate_id=candidate_id, run_id=run_id)
    for event in matching:
        detail = event["detail"]
        proof = detail.get("proof") or {}
        try:
            _require(
                detail.get("schema_version") == SCHEMA and detail.get("proof_hash") == stable_hash(proof),
                "append-only ledger proof changed",
            )
            receipts = verify_cost_reconciliation(proof, context=context, actor=event["actor"], ledger=True)
        except (SkillLibraryError, KeyError, TypeError, ValueError):
            continue  # An invalid receipt never removes the old unknown cost.
        result.extend(replace(r, event_seq=event["seq"]) for r in receipts)
    return result
