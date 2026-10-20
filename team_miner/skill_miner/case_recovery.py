"""One audited operator recovery for a Case coverage preference mistaken for a domain requirement."""

from __future__ import annotations

import copy
from pathlib import Path

from . import bounded_compile_plan as planning
from .case_context import CASE_NARRATIVE_PROTOCOL
from .ov_compile_mining import utc_now
from .ov_staged_orchestration import _json, _read, _save, _sha, _write


def _restore_texts(root: Path, state: dict, candidate: dict) -> tuple[str, str]:
    from .skill_body import iter_integrated_skill

    name = candidate["name"]
    ledger = _read(root / "artifact-batches/candidates" / (name + ".json"))
    scope = _read(root / "scopes" / (name + ".json"))
    metadata = [_read(root / "chunk-metadata" / f"{i:08d}.json") for i in scope["ordinals"]]
    sections = [_read(root / "sections" / name / f"{i:08d}.json") for i in range(ledger["section_count"])]
    if not ledger["skill_ready"] or not ledger["evaluation_ready"]:
        raise ValueError("Case recovery requires the complete previously generated method and evaluation")
    body = "".join(iter_integrated_skill(candidate, iter(sections),
                                       expected_input_ids=[row["id"] for row in metadata],
                                       source_refs=list(dict.fromkeys(row["source_uri"] for row in metadata))))
    evaluations = [row["value"] for path in (root / "artifact-batches/outcomes").glob(f"*-{name}-evaluation.md.json")
                   if (row := _read(path)).get("valid")]
    if len(evaluations) != 1 or not isinstance(evaluations[0], str):
        raise ValueError("Case recovery requires exactly one accepted evaluation")
    return body, evaluations[0]


def reopen_case_candidate(root: Path, workspace: Path, candidate: dict, *, expected_plan_sha256: str,
                          expected_deferred_sha256: str, explanation: str) -> dict:
    """Call only while the TE worker is stopped; keep the accepted sibling Native request intact.

    The operator must inspect the true source and deferred reason. This is no semantic auto-approval:
    pending Cases still run under the new protocol and all structural/source/business gates remain.
    """
    name = candidate["name"]
    if not explanation.strip() or candidate not in _read(root / "identity/original-plan.json")["skills"]:
        raise ValueError("Case recovery requires the frozen selected identity and an inspected reason")
    journal = root / "case-batches/recovery" / name / "source-bound-context-v1.json"
    if journal.exists():
        raise ValueError("Case coverage recovery is allowed once per protocol, never as an unlimited retry reset")
    plan, state = _read(root / "plan.json"), _read(root / "state.json")
    if state.get("complete") or state.get("phase") != "case":
        raise ValueError("Case recovery requires this still-active round at its Case stage")
    marker = root / "deferred-candidates" / (name + ".json")
    deferred = _read(marker)
    if _sha(_json(plan)) != expected_plan_sha256 or _sha(_json(deferred)) != expected_deferred_sha256:
        raise ValueError("Case recovery plan or deferral changed after inspection")
    if any(row["name"] == name for row in plan["skills"]) or name in state["candidate_names"]:
        raise ValueError("Case recovery cannot duplicate an active candidate")
    current = state.get("current") or {}
    if current.get("context", {}).get("skill_name") == name:
        raise ValueError("Case recovery cannot change an in-flight assignment")
    ledger_path = root / "case-batches" / name / "ledger.json"
    ledger = _read(ledger_path)
    pending = [slot for slot in ledger["slots"] if slot["status"] != "valid"]
    if len(ledger["slots"]) != 12 or not pending or not any(slot["status"] == "valid" for slot in ledger["slots"]):
        raise ValueError("Case recovery needs the original twelve slots and already accepted siblings")
    for slot in pending:
        outcomes = []
        for path in (ledger_path.parent / "outcomes").glob(f"*-{slot['index']:02d}.json"):
            row = _read(path)
            if row.get("status") != "deferred":
                continue
            child = path.name.rsplit("-", 1)[0]
            if _read(root / "children" / child / "metadata.json").get(
                "case_narrative_protocol"
            ) == CASE_NARRATIVE_PROTOCOL:
                raise ValueError("Case recovery cannot reset a deferral under the new protocol")
            outcomes.append(row)
        if not outcomes:
            raise ValueError("Case recovery needs preserved old-protocol deferrals")
    scope = _read(root / "scopes" / (name + ".json"))
    if not scope.get("method_lineage_verified"):
        raise ValueError("Case recovery cannot restore unverified workflow lineage")
    for slot in ledger["slots"]:
        if slot["status"] == "valid":
            planning.validate_case(_read(root / "cases" / name / f"{slot['index']:02d}.json"),
                                   case_id=slot["case_id"], skill_name=name, source_refs=deferred["source_refs"])
    skill_text, evaluation_text = _restore_texts(root, state, candidate)
    target = workspace / "compiled_skill" / name
    if target.exists():
        raise ValueError("Case recovery cannot overwrite existing generated output")
    _save(journal, {
        "protocol": CASE_NARRATIVE_PROTOCOL, "explanation": explanation,
        "reopened_at": utc_now(),
        "original_plan": plan, "original_state": state, "original_deferred": deferred,
        "original_case_ledger": ledger, "candidate": candidate,
        "restored_skill_sha256": _sha(skill_text), "restored_evaluation_sha256": _sha(evaluation_text),
        "accepted_siblings_preserved": True, "original_native_requests_preserved": True,
        "business_quality_passed": False,
    })
    plan = copy.deepcopy(plan)
    plan["skills"].append(candidate)
    plan["deferred"] = [row for row in plan["deferred"] if row != deferred]
    state["candidate_names"].append(name)  # Repair after the current accepted sibling; no cursor rewind.
    for slot in pending:
        slot.update(group_attempt=0, coverage_recovery_protocol=CASE_NARRATIVE_PROTOCOL)
        # Format attempt counts are deliberately unchanged.
    _write(target / "SKILL.md", skill_text)
    _write(target / "EVALUATION.md", evaluation_text)
    _save(ledger_path, ledger)
    _save(marker, {**deferred, "status": "reopened", "recovery_journal": str(journal.relative_to(root))})
    _save(root / "plan.json", plan)
    _save(root / "state.json", state)
    return _read(journal)
