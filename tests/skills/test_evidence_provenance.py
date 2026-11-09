"""Source roots and factual target units cannot be inflated by pulls or splits."""

from __future__ import annotations

import copy
import json
from types import SimpleNamespace

import pytest

from session_ingestion.schemas import session_payload_hash
from team_skills.evolution.replay_case_synthesis import (
    CrossSessionCaseSynthesisError,
    LLMCrossSessionCaseSynthesizer,
    hydrate_cross_session_cases,
)
from team_skills.evolution.repository import EvolutionRepository
from team_skills.evolution.session_processing import (
    eligible_evidence,
    evidence_gap,
    evidence_source_key,
    groups,
    load_sessions,
    merge_evidence,
    replay_cases,
)
from team_skills.execution.planner import raw_evidence
from teamEvolver.evidence_context import build_evidence_context

CONFIG = {"min_group_sessions": 2, "min_group_users": 2}


@pytest.mark.asyncio
async def test_skill_history_keeps_original_prompt_material_without_changing_skill_summary_input():
    source = session("archived", "original-root")

    class Conn:
        async def executemany(self, sql, rows):
            self.row = {"projection": rows[0][-1], "content_hash": rows[0][-2]}

        async def fetch(self, *args):
            return [self.row]

    conn = Conn()
    repo = EvolutionRepository(SimpleNamespace(pool=SimpleNamespace(schema="test")))
    await repo.record_evidence(conn, tenant_id="default", name="demo", sessions=[source])
    stored = json.loads(conn.row["projection"])
    assert stored["prompt_source"]["turns"] == source["turns"]
    skill = (await repo.evidence(conn, "demo"))[0]
    assert skill["_summary"] == source["_summary"] and "turns" not in skill and "prompt_source" not in skill
    prompt = (await repo.evidence(conn, "demo", include_source=True))[0]
    merged = merge_evidence([], [prompt])
    assert raw_evidence(merged)[0]["turns"] == merged[0]["_evidence_turns"]
    packet = build_evidence_context(merged)
    assert len(packet["examples"]) == 1 and packet["coverage"]["truncated_fields"] == []
    assert packet["examples"][0]["recorded_query"] == "Task new"


def ref(root, identity, revision="r1"):
    return {"source_session_key": root, "trace_id": identity, "revision": revision,
            "context_revision": "context:" + identity + ":" + revision}


def session(identity, root, *, targets=("new",), user="user-1", revision="r1"):
    refs = [ref(root, "old"), *[ref(root, name, revision) for name in targets if name != "old"]]
    return {
        "session_id": identity, "user_alias": user, "source": "langfuse", "content_hash": "fixed",
        "_summary": "Mandatory ingest summary", "_judge_scores": {"overall_score": 4},
        "_skills_referenced": {"old-skill", "demo"},
        "runtime": {"type": "fake"},
        "turns": [
            {"turn_num": index + 1, "source_trace_id": item["trace_id"], "trace_id": "display:" + item["trace_id"],
             "prompt_text": "Task " + item["trace_id"], "response_text": "Done", "prm_score": 4,
             "used_skills": ["old-skill" if item["trace_id"] == "old" else "demo"]}
            for index, item in enumerate(refs)
        ],
        "provenance": {
            "schema_version": "session-source.v1", "identity_verified": True, "trace_identity_verified": True,
            "source_session_key": root, "snapshot_revision": identity + ":snapshot", "trace_revisions": refs,
            "target_trace_refs": [item for item in refs if item["trace_id"] in targets],
            "context_trace_refs": [item for item in refs if item["trace_id"] not in targets], "revision_conflicts": [],
        },
    }


def cases(ids):
    return [{"case_id": "case-" + str(index), "query": "A representative task", "checks": [
        {"id": "R" + str(number), "kind": "output", "text": "Condition " + str(number)} for number in range(7)
    ], "provenance": {"session_ids": ids}} for index in range(2)]


def test_snapshots_and_semantic_segments_from_one_root_do_not_satisfy_team_gate():
    original = session("snapshot-1", "root-1", targets=("turn-1",))
    growing = session("snapshot-2", "root-1", targets=("turn-2",), user="user-2")
    split = session("segment-3", "root-1", targets=("turn-3",))
    assert evidence_gap([original, growing, split], CONFIG) == "insufficient distinct Sessions for team evidence"
    another = session("another", "root-2", user="user-2")
    assert evidence_gap([original, growing, split, another], CONFIG) == ""


def test_identical_target_facts_are_kept_once_without_discarding_other_new_units():
    first = session("first", "root-1", targets=("t1",))
    duplicate_and_new = session("second", "root-1", targets=("t1", "t2"))
    before = copy.deepcopy([first, duplicate_and_new])
    selected = eligible_evidence([first, duplicate_and_new])
    refs = [item["trace_id"] for source in selected for item in source["provenance"]["target_trace_refs"]]
    assert refs == ["t1", "t2"]
    assert len(selected) == 2 and [first, duplicate_and_new] == before
    assert {evidence_source_key(source) for source in selected} == {"root-1"}


def test_newer_trace_revision_replaces_older_fact_even_across_new_formal_ids():
    first = session("first", "root-1", targets=("t1",), revision="old")
    later = session("later", "root-1", targets=("t1",), revision="new")
    selected = eligible_evidence([first, later])
    assert [source["session_id"] for source in selected] == ["later"]
    assert selected[0]["provenance"]["target_trace_refs"][0]["revision"] == "new"


def test_late_analysis_completion_does_not_replace_a_newer_upstream_fact():
    newer = session("archived-first", "root-1", targets=("t1",), revision="new")
    older = session("archived-later", "root-1", targets=("t1",), revision="old")
    newer["turns"][1]["source_updated_at"] = "2026-10-27T01:00:02Z"
    older["turns"][1]["source_updated_at"] = "2026-10-27T01:00:01Z"
    selected = eligible_evidence([newer, older])
    assert [source["session_id"] for source in selected] == ["archived-first"]


def test_split_segment_keeps_root_context_refs_outside_its_own_turn_inventory():
    value = session("segment", "root-1")
    outside = ref("root-1", "another-segment-context")
    value["provenance"]["context_trace_refs"].append(outside)
    projected = eligible_evidence([value])[0]
    assert outside in projected["provenance"]["context_trace_refs"]


def test_only_target_turns_group_skills_and_generate_cases_but_full_context_is_preserved():
    value = session("first", "root-1")
    selected = eligible_evidence([value])[0]
    assert selected["turns"] == value["turns"]
    assert selected["_skills_referenced"] == {"demo"}
    assert selected["_evidence_turns"][0]["source_trace_id"] == "new"
    assert "old-skill" not in selected["_trajectory"]
    assert set(groups([value])) == {"demo"}
    generated = replay_cases([value])
    assert len(generated) == 1
    assert generated[0]["query"] == "Task new"
    assert generated[0]["provenance"]["source_session_key"] == "root-1"
    assert generated[0]["provenance"]["target_trace_refs"][0]["trace_id"] == "new"


@pytest.mark.parametrize("change", [
    {"identity_verified": False}, {"trace_identity_verified": False}, {"identity_conflict": True},
    {"revision_conflicts": ["new"]}, {"target_trace_refs": []}, {"source_session_key": ""},
    {"schema_version": "client-invented"},
])
def test_uncertain_conflicting_or_context_only_sources_are_not_valid_new_evidence(change):
    value = session("first", "root-1")
    value["provenance"].update(change)
    assert evidence_source_key(value) == ""
    assert eligible_evidence([value]) == []
    assert replay_cases([value]) == []
    assert evidence_gap([value], CONFIG)


@pytest.mark.parametrize("change", [
    {"source": "synthetic"}, {"source": "replay"}, {"source": "managed_agent_candidate_audit"},
    {"runtime_context": {"candidate_job_id": "candidate", "candidate_sha256": "hash"}},
    {"runtime_context": {"replay_run_id": "run"}}, {"meta": {"is_synthetic": True}},
])
def test_synthetic_replay_and_candidate_audit_never_bypass_team_evidence_gate(change):
    value = session("first", "root-1")
    value.update(change)
    assert evidence_gap([value], CONFIG)
    assert eligible_evidence([value]) == []


def test_no_provenance_legacy_skill_only_still_works_but_explicit_unknown_does_not():
    first, second = session("first", "r1"), session("second", "r2", user="user-2")
    for value in (first, second):
        del value["provenance"]
    assert evidence_gap([first, second], CONFIG) == ""
    assert evidence_source_key(first) == "legacy:first"
    first["provenance"] = {"identity_verified": False}
    assert evidence_gap([first, second], CONFIG)


def test_legacy_semantic_split_metadata_counts_one_explicit_root():
    first, second = session("segment-1", "r1"), session("segment-2", "r2", user="user-2")
    for value in (first, second):
        del value["provenance"]
        value["metadata"] = {"split_from_session": "original-root"}
    assert evidence_gap([first, second], CONFIG)


def test_current_roots_replace_history_and_historical_roots_contribute_once():
    current = session("current", "r1", targets=("current",))
    same_root = session("historical-copy", "r1", targets=("old-copy",))
    old = session("history-1", "r2", targets=("h1",), user="user-2")
    older = session("history-2", "r2", targets=("h2",), user="user-2")
    result = merge_evidence([current], [same_root, old, older])
    assert [item["session_id"] for item in result] == ["current", "history-1"]
    assert evidence_gap(result, CONFIG) == ""


def test_historical_projection_retains_verified_identity_without_inventing_turns():
    historical = session("history", "r2", user="user-2")
    del historical["turns"]
    selected = merge_evidence([session("current", "r1")], [historical])
    assert evidence_gap(selected, CONFIG) == ""
    assert "turns" not in selected[1]


def test_hydrator_rejects_two_formal_ids_backed_by_one_upstream_root():
    sources = [session("a", "r1", targets=("a",)), session("b", "r1", targets=("b",), user="user-2")]
    with pytest.raises(CrossSessionCaseSynthesisError, match="independent"):
        hydrate_cross_session_cases(cases(["a", "b"]), sources, skill_name="demo")


def test_hydrator_tracks_root_identity_and_targets_while_using_the_correct_source_turn():
    sources = [session("a", "r1"), session("b", "r2", user="user-2")]
    result = hydrate_cross_session_cases(cases(["a", "b"]), sources, skill_name="demo")
    assert result[0]["provenance"]["source_session_keys"] == ["r1", "r2"]
    assert result[0]["provenance"]["turn_num"] == 2
    assert {ref["trace_id"] for ref in result[0]["provenance"]["target_trace_refs"]} == {"new"}
    assert len(result[0]["provenance"]["session_snapshot"]["turns"]) == 2


@pytest.mark.asyncio
async def test_synthesis_rejects_insufficient_roots_before_any_model_call():
    class Model:
        async def chat(self, *args, **kwargs):
            pytest.fail("must not call a model for duplicated source roots")

    with pytest.raises(CrossSessionCaseSynthesisError, match="independent"):
        await LLMCrossSessionCaseSynthesizer(Model())(
            skill_name="demo", sessions=[session("a", "r1", targets=("a",)), session("b", "r1", targets=("b",))],
            candidate_skill={}, evidence_context={},
        )


@pytest.mark.asyncio
async def test_synthesis_gives_model_independent_root_mapping_and_preserves_source_inputs():
    class Model:
        async def chat(self, messages, **kwargs):
            payload = json.loads(messages[1]["content"])
            context = payload["team_sop_evidence"]["context"]
            assert context["independent_source_sessions"] == 2
            assert context["source_session_keys"] == {"a": "r1", "b": "r2"}
            return json.dumps({"test_datasets": [{
                **case, "name": case["case_id"], "source_session_ids": ["a", "b"],
            } for case in cases(["a", "b"])]})

    sources = [session("a", "r1"), session("b", "r2", user="user-2")]
    frozen = copy.deepcopy(sources)
    generated = await LLMCrossSessionCaseSynthesizer(Model())(
        skill_name="demo", sessions=sources, candidate_skill={}, evidence_context={},
    )
    assert len(generated) == 2 and sources == frozen


@pytest.mark.asyncio
async def test_load_requires_original_hash_and_analysis_before_filtering_provenance():
    value = session("a", "r1")

    class Payloads:
        def get_session(self, content_ref):
            return copy.deepcopy(value)

    # Invalid hashes remain an error, even for sources that would later be filtered.
    with pytest.raises(ValueError, match="content hash changed"):
        await load_sessions([{"content_ref": "ref", "content_hash": "wrong", "session_id": "a", "user_id": "u"}],
                            Payloads())
    value["provenance"]["identity_verified"] = False
    with pytest.raises(ValueError, match="mandatory ingest analysis"):
        await load_sessions([{"content_ref": "ref", "content_hash": session_payload_hash(value),
                              "session_id": "a", "user_id": "u"}], Payloads())
    value["_judge_scores"] = {"source": "llm", **{key: 0.8 for key in (
        "overall_score", "task_completion", "response_quality", "efficiency", "tool_usage",
    )}}
    value["value_judge"] = {"mode": "merged", "decision": "valuable", "confidence": 0.9, "reason": "Reusable"}
    assert await load_sessions([{"content_ref": "ref", "content_hash": session_payload_hash(value),
                                 "session_id": "a", "user_id": "u"}], Payloads()) == []
