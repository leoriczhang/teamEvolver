"""Case history projection preserves original source, exact identity and strict duplicate checks."""

from __future__ import annotations

import asyncio
import copy

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_case_batch_orchestration import _bounded_case_controls, case_source_roles
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.skill_miner.source_scope import EFFICIENCY_PROTOCOL, SOURCE_SCOPE_PROTOCOL
from tests.miner.skill_miner.support.candidate_identity import candidate
from tests.miner.skill_miner.support.ov_case_batch_orchestration import BatchOwner


def assignment():
    chunks = [planning.source_chunks(chr(97 + i) * size, "viking://source/" + str(i) + "x" * 150)[0]
              for i, size in enumerate([11000, 11000, 2710])]
    controls = {
        "candidate": {"name": "review-request", "task": "t" * 400, "trigger": "t" * 400, "boundary": "b" * 400},
        "skill_name": "review-request", "case_id": "review-request-case-12",
        "allowed_source_refs": [chunk["source_uri"] for chunk in chunks],
        "input_ids": [chunk["id"] for chunk in chunks], "coverage_dimension": planning.CASE_DIMENSIONS[11],
        "previous_queries": ["q" * 300 for _ in range(11)], "previous_queries_excerpted": False,
        "source_scope_rule": "Only assigned Evidence supports this Case.", "case_batch_size": 4,
        "repair_error": "e" * 1000,
    }
    return chunks, controls


def test_case_source_roles_distinguish_neighboring_workflows_without_altering_evidence():
    chunks, _ = assignment()
    for index, chunk in enumerate(chunks):
        chunk["ordinal"] = index
    original = copy.deepcopy(chunks)
    roles = case_source_roles(chunks, {"method_ordinals": [0], "ordinals": [0, 1, 2]})
    assert roles == {"method_input_ids": [chunks[0]["id"]],
                     "context_input_ids": [chunks[1]["id"], chunks[2]["id"]]}
    assert chunks == original
    with pytest.raises(ValueError, match="not only neighboring context"):
        case_source_roles(chunks[1:], {"method_ordinals": [0], "ordinals": [0, 1, 2]})


def test_exact_25998_source_packet_repair_projects_history_and_preserves_all_evidence():
    chunks, controls = assignment()
    original = copy.deepcopy(controls)
    assert len(planning._json({"evidence": chunks})) == 25998
    with pytest.raises(ValueError, match="bounded input"):
        planning.build_stage_packet("case", "case-output-11.json", evidence_chunks=chunks, controls=controls)
    projected = _bounded_case_controls(chunks, controls, {"language": "zh-CN"})
    packet = planning.build_stage_packet("case", "case-output-11.json", evidence_chunks=chunks,
                                        controls=projected, language_policy={"language": "zh-CN"})
    assert len(planning._json(packet)) <= 31488
    assert packet["evidence"] == chunks
    assert projected["previous_queries_excerpted"]
    for field in ("candidate", "case_id", "input_ids", "allowed_source_refs", "repair_error"):
        assert projected[field] == controls[field]
    assert controls == original


def test_control_character_diagnostics_are_bounded_as_json_and_full_error_is_not_mutated():
    chunks, controls = assignment()
    controls["repair_error"] = "\0" * 1000
    original = copy.deepcopy(controls)
    projected = _bounded_case_controls(chunks, controls, {"language": "en"})
    assert len(planning._json(projected["repair_error"])) <= 2000
    assert projected["repair_error_excerpted"]
    assert original == controls and len(controls["repair_error"]) == 1000
    packet = planning.build_stage_packet("case", "case-output-11.json", evidence_chunks=chunks,
                                        controls=projected, language_policy={"language": "en"})
    assert len(planning._json(packet)) <= 31488


def test_unfit_core_source_is_explicitly_rejected_without_removing_original_ranges():
    chunks, controls = assignment()
    controls["previous_queries"] = []
    controls["candidate"]["task"] = "x" * 30000
    original = copy.deepcopy(chunks)
    with pytest.raises(ValueError, match="core controls"):
        _bounded_case_controls(chunks, controls, {"language": "zh-CN"})
    assert chunks == original


@pytest.mark.parametrize("repair", ["", "\0" * 1000], ids=["initial", "format-repair"])
def test_production_fresh_batch_caller_constructs_bounded_packet_and_keeps_local_history(tmp_path, repair):
    class CapturingOwner(BatchOwner):
        async def _submit_case_batch(self, job, root, state, packets, contexts, **kwargs):
            return {"packets": packets, "contexts": contexts}

    async def run():
        owner = CapturingOwner(tmp_path / "owner")
        root = tmp_path / "staged"
        _save(root / "placeholder.json", {})
        (root / "compiler-base.md").write_text(SKILL_BODY)
        value = candidate("review-request")
        value.update(task="t" * 400, trigger="t" * 400, boundary="b" * 400)
        chunks, _ = assignment()
        # The production records include scalar ordinals; keep the full group within the same 26K raw limit.
        chunks[2] = planning.source_chunks("c" * 2674, chunks[2]["source_uri"])[0]
        for ordinal, chunk in enumerate(chunks):
            chunk["ordinal"] = ordinal
            _save(root / "chunks" / f"{ordinal:08d}.json", chunk)
        for index in range(11):
            _save(root / "cases/review-request" / f"{index:02d}.json", {
                "case_id": f"review-request-case-{index + 1:02d}", "query": str(index) + "q" * 1000,
            })
        if repair:
            _save(root / "case-batches/review-request/ledger.json", {
                "case_ids": [f"review-request-case-{i + 1:02d}" for i in range(12)],
                "slots": [{"index": i, "case_id": f"review-request-case-{i + 1:02d}",
                           "status": "valid" if i < 11 else "pending", "format_attempts": int(i == 11),
                           "group_attempt": 0, "error": repair if i == 11 else ""} for i in range(12)],
            })
        state = {"efficiency_protocol": EFFICIENCY_PROTOCOL,
                 "language_policy": {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"},
                 "case_index": 11, "base_sha256": _sha(SKILL_BODY)}
        scope = {"scope_protocol": SOURCE_SCOPE_PROTOCOL, "ordinals": [0, 1, 2],
                 "method_ordinals": [0, 1], "shared_context_ordinals": [2]}
        result = await owner._staged_case_batch_next({"job_id": "inert", "current_round": 1},
                                                   root, state, value, scope)
        assert len(result["packets"]) == 1
        packet = result["packets"][0]
        assert len(planning._json(packet)) <= 32000
        assert packet["evidence"] == chunks
        assert packet["derived_controls"]["case_id"] == "review-request-case-12"
        assert packet["derived_controls"]["previous_queries_omitted"]
        assert packet["derived_controls"]["previous_queries"] == []
        history = packet["derived_controls"]
        assert len(history["previous_query_fingerprints"]) == 11 or (
            history["previous_query_fingerprints_count"] == 11 and history["previous_query_fingerprints_omitted"]
        )
        assert len(_read(root / "cases/review-request/00.json")["query"]) == 1001
        if repair:
            assert packet["derived_controls"]["repair_error_excerpted"]
            ledger = _read(root / "case-batches/review-request/ledger.json")
            assert ledger["slots"][11]["error"] == repair
    asyncio.run(run())
