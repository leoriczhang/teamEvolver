"""New shared-template batches retain full current Evidence and freeze bounded history."""
from __future__ import annotations

import copy
import hashlib
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from team_skills.evolution import cycle
from team_skills.execution import batching, planning, writer_runtime
from team_skills.execution.contracts import plan_for, validate_profile
from team_skills.library.repository import SkillLibraryError
from teamEvolver.evidence_context import EvidenceContextError
from teamEvolver.runtime.tasks import HandlerOutcome
from tests.support.execution import profile_raw
from tests.support.execution_feedback import analyzed_source


def context():
    raw = profile_raw()
    raw["affected_skill_ids"] = []
    raw["environment"]["template_input_contract"] = {
        "schema_version": "teamevolver.template-input-contract.v1", "variable_names": ["sceneBackground"],
    }
    raw["environment"]["template_variables"] = {"sceneBackground": "Original source background"}
    profile = validate_profile(raw)
    return {"allowed_modules": ["prompt"], "execution_profile_id": profile["profile_id"],
            "base_release_id": "published", "baseline_execution_plan": plan_for(profile, profile["components"])}


def source(sid, *, extra=""):
    value = analyzed_source(sid)
    root, tid = "root-" + sid, "trace-" + sid
    value["content_hash"] = hashlib.sha256(sid.encode()).hexdigest()
    value["provenance"]["source_session_key"] = root
    for ref in (*value["provenance"]["trace_revisions"], *value["provenance"]["target_trace_refs"]):
        ref.update(source_session_key=root, trace_id=tid)
    value["turns"][0]["source_trace_id"] = tid
    value["_summary"] += extra
    value["turns"][0]["response_text"] += extra
    return value


def test_all_current_sources_are_complete_and_history_omissions_are_identifiable(monkeypatch):
    current = source("current")
    current["_skills_referenced"] = {"demo"}
    latest, large, older = source("latest"), source("large", extra="h" * 50_000), source("older")
    sessions, frozen = [current, latest, large, older], context()
    before = copy.deepcopy((sessions, frozen))
    budget = batching._measure([current, latest, older], frozen)["writer_input_bytes"]
    monkeypatch.setattr(batching, "_HISTORY_WRITER_BYTES", budget)
    selected, audit = batching.select_prompt_materials(sessions, frozen, ["current"])
    assert selected == [current, latest, older] and selected[0] is not current
    assert selected[0]["turns"] == current["turns"]
    assert audit["current_sources"][0]["content_hash"] == current["content_hash"]
    assert audit["selected_session_ids"] == ["current", "latest", "older"]
    assert [s["session_id"] for s in audit["selected_historical_sources"]] == ["latest", "older"]
    assert audit["omitted_historical_sources"] == [{
        **batching._source(large), "reason": "historical_editor_workspace",
    }]
    assert audit["all_current_sessions_preserved"] is True
    assert audit["historical_omissions_are_not_new_or_read_material"] is True
    assert audit["measurement"] == "native_writer_json_and_synthesis_content_bytes_not_tokens"
    assert audit["writer_input_bytes"] <= budget
    assert (sessions, frozen) == before


def test_current_sources_take_priority_even_when_input_history_precedes_them(monkeypatch):
    old, current = source("old", extra="h" * 5000), source("current")
    frozen = context()
    monkeypatch.setattr(batching, "_HISTORY_WRITER_BYTES", batching._measure([current], frozen)["writer_input_bytes"])
    selected, audit = batching.select_prompt_materials([old, current], frozen, ["current"])
    assert selected == [current]
    assert audit["omitted_historical_sources"][0]["session_id"] == "old"


def test_history_workspace_never_rejects_an_originally_valid_current_batch(monkeypatch):
    current, historical, frozen = source("current"), source("old"), context()
    actual = batching._measure([current], frozen)
    monkeypatch.setattr(batching, "_HISTORY_WRITER_BYTES", actual["writer_input_bytes"] - 1)
    selected, audit = batching.select_prompt_materials([current, historical], frozen, ["current"])
    assert selected == [current]
    assert audit["writer_input_bytes"] == actual["writer_input_bytes"]
    assert audit["omitted_historical_sources"][0]["reason"] == "historical_editor_workspace"


def test_more_than_fifty_current_sources_remain_complete_when_native_requests_fit():
    sessions = [source(str(index)) for index in range(52)]
    selected, audit = batching.select_prompt_materials(sessions, context(), [s["session_id"] for s in sessions])
    assert selected == sessions and len(audit["current_sources"]) == 52
    assert not audit["omitted_historical_sources"]


def test_single_current_source_can_reuse_history_before_the_native_two_root_case_check():
    current, historical = source("new-only"), source("historical-root")
    selected, audit = batching.select_prompt_materials([current, historical], context(), ["new-only"])
    assert selected == [current, historical]
    assert audit["selected_historical_sources"][0]["session_id"] == "historical-root"


def test_nonrequest_analysis_and_window_audit_do_not_create_an_extra_current_input_gate():
    current, frozen = source("current"), context()
    current["unused_analysis_debug"] = "unrelated derived analysis" * 50_000
    frozen["material_window"] = {"previous_audit": "not a business source" * 50_000}
    selected, audit = batching.select_prompt_materials([current], frozen, ["current"])
    assert selected == [current] and audit["within_budget"]


@pytest.mark.parametrize("current_ids", [None, [], ["missing"], ["current", "current"], [""]])
def test_ambiguous_or_missing_current_material_is_never_silently_dropped(current_ids):
    with pytest.raises(SkillLibraryError):
        batching.select_prompt_materials([source("current")], context(), current_ids)


def test_duplicate_session_projections_and_unopted_profiles_are_rejected():
    current, frozen = source("current"), context()
    with pytest.raises(SkillLibraryError, match="duplicated"):
        batching.select_prompt_materials([current, copy.deepcopy(current)], frozen, ["current"])
    del frozen["baseline_execution_plan"]["environment"]["template_input_contract"]
    assert batching.shared_template_batch(frozen) is False
    with pytest.raises(SkillLibraryError, match="explicit template input contract"):
        batching.select_prompt_materials([current], frozen, ["current"])


def test_oversized_current_batch_is_typed_failure_and_no_subset_is_returned(monkeypatch):
    current, frozen = source("current"), context()
    minimum = batching._measure([current], frozen)["writer_input_bytes"]
    monkeypatch.setattr(writer_runtime, "MAX_EDITOR_INPUT_BYTES", minimum - 1)
    with pytest.raises(batching.PromptBatchInputTooLarge, match="complete current") as failed:
        batching.select_prompt_materials([current, source("history")], frozen, ["current"])
    assert failed.value.generation_cost is None  # Preflight did not make any model calls.


def test_feedback_already_bound_to_context_counts_towards_material_budget(monkeypatch):
    current, frozen = source("current"), context()
    frozen.update(active_candidate={"revision": 3}, working_execution_plan=frozen["baseline_execution_plan"])
    minimum = batching._measure([current], frozen)["writer_input_bytes"]
    monkeypatch.setattr(writer_runtime, "MAX_EDITOR_INPUT_BYTES", minimum + 100)
    frozen["active_candidate_replay_feedback"] = {"candidate_revision": 3, "query": "large feedback" * 200}
    with pytest.raises(batching.PromptBatchInputTooLarge):
        batching.select_prompt_materials([current], frozen, ["current"])


def test_expanded_packet_overflow_is_size_but_integrity_error_is_never_history_selection(monkeypatch):
    current, history = source("current"), source("history")
    real_builder = batching.evolution_synthesis_input

    def oversized(**kwargs):
        if len(kwargs["sessions"]) > 1:
            raise batching.SynthesisInputTooLarge("Complete Case synthesis evidence exceeds the expanded byte limit")
        return real_builder(**kwargs)

    monkeypatch.setattr(batching, "evolution_synthesis_input", oversized)
    selected, audit = batching.select_prompt_materials([current, history], context(), ["current"])
    assert selected == [current] and len(audit["omitted_historical_sources"]) == 1
    monkeypatch.setattr(batching, "evolution_synthesis_input", real_builder)
    corrupt = copy.deepcopy(current)
    corrupt["session_id"] = "conflict"
    corrupt["turns"][0]["response_text"] = "Conflicting original response"
    with pytest.raises(EvidenceContextError, match="Conflicting"):
        batching.select_prompt_materials([current, corrupt], context(), ["current"])


@pytest.mark.asyncio
async def test_retry_restores_the_exact_window_without_reselecting_history(monkeypatch):
    frozen = context()
    repo = SimpleNamespace(branch=AsyncMock(return_value=None), save_branch=AsyncMock())
    task = SimpleNamespace(tenant_id="test", task_id="batch")
    current, old = source("current"), source("old")
    current["_skills_referenced"] = {"demo"}
    first = await planning.freeze_prompt_planning(
        None, repo, task=task, branch_key="execution:shared", context=frozen,
        candidate=None, sessions=[current, old], current_session_ids=["current"],
    )
    saved = copy.deepcopy(repo.save_branch.call_args.kwargs["result"])
    repo.branch.return_value = saved

    def unexpected(*args):
        pytest.fail("a frozen retry must not select a different material window")

    monkeypatch.setattr(batching, "select_prompt_materials", unexpected)
    second = await planning.freeze_prompt_planning(
        None, repo, task=task, branch_key="execution:shared", context=frozen,
        candidate=None, sessions=[source("later")], current_session_ids=["later"],
    )
    assert first == second
    assert first["sessions"][0]["_skills_referenced"] == {"demo"}
    assert first["execution_context"]["material_window"]["selected_session_ids"] == ["current", "old"]
    repo.save_branch.assert_awaited_once()
    assert saved == repo.branch.return_value


@pytest.mark.asyncio
async def test_legacy_freeze_does_not_activate_material_selection(monkeypatch):
    frozen = context()
    del frozen["baseline_execution_plan"]["environment"]["template_input_contract"]
    repo = SimpleNamespace(branch=AsyncMock(return_value=None), save_branch=AsyncMock())

    def unexpected(*args):
        pytest.fail("legacy planning must remain unchanged")

    monkeypatch.setattr(batching, "select_prompt_materials", unexpected)
    sessions = [source("current")]
    value = await planning.freeze_prompt_planning(
        None, repo, task=SimpleNamespace(tenant_id="test", task_id="legacy"), branch_key="execution:legacy",
        context=frozen, candidate=None, sessions=sessions,
    )
    assert value["sessions"] == sessions and "material_window" not in value["execution_context"]


@pytest.mark.asyncio
@pytest.mark.parametrize("opt_in", [False, True])
async def test_native_writer_size_skip_is_only_failed_for_shared_template_batches(monkeypatch, opt_in):
    frozen = context()
    if not opt_in:
        del frozen["baseline_execution_plan"]["environment"]["template_input_contract"]
    returned = ({"action": "skip", "rationale": "raw_prompt_evidence_exceeds_budget"}, None, {}, {}, [])
    monkeypatch.setattr(cycle, "_prepare_branch_work", AsyncMock(return_value=returned))
    task = SimpleNamespace(task_id="evolve", attempt=2)
    if opt_in:
        with pytest.raises(batching.PromptBatchInputTooLarge) as failed:
            await cycle._prepare_branch(task, None, context=frozen)
        assert failed.value.generation_cost["unreconciled_worker_attempts"] == 1
    else:
        value = await cycle._prepare_branch(task, None, context=frozen)
        assert value[0]["rationale"] == "raw_prompt_evidence_exceeds_budget"


@pytest.mark.asyncio
@pytest.mark.parametrize("measured_cost", [None, {"total_tokens": 91, "attempt_count": 1}])
async def test_failed_current_batch_uses_native_command_result_without_consuming(monkeypatch, measured_cost):
    @asynccontextmanager
    async def tenant_tx(tenant_id):
        yield None

    repo = SimpleNamespace(finish=AsyncMock(), consume=AsyncMock(), record_evidence=AsyncMock())
    guard = SimpleNamespace(acquire=AsyncMock(return_value="lease"), release=AsyncMock())
    services = SimpleNamespace(
        tx=SimpleNamespace(tenant_tx=tenant_tx), tasks=SimpleNamespace(require_current=AsyncMock()),
        leases=object(), extras={key: object() for key in
                                ("skill_planner", "replay_case_synthesizer", "session_payloads")},
    )
    monkeypatch.setattr(cycle, "load_command", AsyncMock(return_value={
        "result": None, "payload": {"config_revision": 1, "config": {}},
    }))
    monkeypatch.setattr(cycle, "EvolutionRepository", lambda tx: repo)
    monkeypatch.setattr(cycle, "LeaseGuard", lambda leases: guard)
    monkeypatch.setattr(cycle, "_cycle", AsyncMock(side_effect=batching.PromptBatchInputTooLarge(
        "repackage current batch", generation_cost=measured_cost,
    )))
    result = await cycle.run_cycle(SimpleNamespace(task_id="evolve", tenant_id="test", fence=1), services)
    assert result.outcome == HandlerOutcome.FAIL and result.error_code == "prompt_batch_input_too_large"
    record = repo.finish.call_args.args[2]
    assert record["status"] == "failed" and record["material_consumed"] is False
    assert record["generation_cost"] == measured_cost
    repo.consume.assert_not_awaited()
    repo.record_evidence.assert_not_awaited()
    services.tasks.require_current.assert_awaited_once()
    guard.release.assert_awaited_once_with(None, "lease")
