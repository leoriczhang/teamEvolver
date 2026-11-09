"""Independent event fixtures for factual source identity and incremental targets."""
from __future__ import annotations

import copy
from types import SimpleNamespace

from session_ingestion.api import _dedupe_key
from session_ingestion.source_revisions import (
    plan_snapshot,
    preserve_requests,
    reported_session,
    segment_provenance,
    source_identity,
)


def event(identifier, *, text="same", updated="2026-10-20T01:00:00Z", timestamp=None):
    return {"trace_id": identifier, "prompt_text": text, "response_text": "answer",
            "source_updated_at": updated, "source_timestamp": timestamp or f"2026-10-20T00:0{identifier[-1]}:00Z"}


def snapshot(events, **extra):
    return {"source_identity": {"source_instance_id": "training", "upstream_namespace": "project-one",
                                "identity_verified": True}, "turns": events, **extra}


def plan(value, previous=None, tenant="alpha"):
    return plan_snapshot(value, tenant_id=tenant, external_id="conversation", previous=previous)


def ids(value, key="target_trace_refs"):
    return [r["trace_id"] for r in value["provenance"][key]]


def test_factual_events_are_not_identified_by_text_or_pull_metadata():
    original = plan(snapshot([event("t1"), event("t2")]))
    repeated = copy.deepcopy(snapshot([event("t1"), event("t2")]))
    repeated["fetched_at"] = "tomorrow"
    repeated["_summary"] = "a different model-derived description"
    repeated["turns"][0]["analyzed_at"] = "different"
    actual = plan(repeated, original)
    assert ids(original) == ["t1", "t2"]  # Identical text is two real interactions.
    assert ids(actual) == []
    assert original["provenance"]["snapshot_revision"] == actual["provenance"]["snapshot_revision"]


def test_upstream_generation_enumeration_order_does_not_create_new_evidence():
    observations = [{"id": "call-b", "type": "GENERATION", "startTime": "2026-10-20T01:02:00Z",
                     "input": [{"role": "system", "content": "Later request"}]},
                    {"id": "call-a", "type": "GENERATION", "startTime": "2026-10-20T01:01:00Z",
                     "input": [{"role": "system", "content": "Earlier request"}]}]

    def fetched(items):
        return preserve_requests(snapshot([event("t1")]), [{"trace": {"id": "t1",
                                 "updatedAt": "2026-10-20T03:00:00Z"}, "observations": items}])

    original = plan(fetched(observations))
    repeated = plan(fetched(list(reversed(observations))), original)
    assert original["provenance"]["snapshot_revision"] == repeated["provenance"]["snapshot_revision"]
    assert repeated["provenance"]["revision_conflicts"] == []
    assert ids(repeated) == []
    requests = repeated["turns"][0]["request_context"]["generation_requests"]
    assert [r["observation_id"] for r in requests] == ["call-a", "call-b"]


def test_six_to_seven_preserves_context_and_only_targets_new_event():
    original = plan(snapshot([event(f"t{i}") for i in range(1, 7)]))
    actual = plan(snapshot([event(f"t{i}") for i in range(1, 8)]), original)
    assert len(actual["turns"]) == 7
    assert ids(actual) == ["t7"]
    assert ids(actual, "context_trace_refs") == [f"t{i}" for i in range(1, 7)]


def test_partial_response_keeps_missing_units_and_late_event_changes_affected_context():
    original = plan(snapshot([event("t1"), event("t3")]))
    actual = plan(snapshot([event("t2")]), original)
    assert [t["trace_id"] for t in actual["turns"]] == ["t1", "t2", "t3"]
    assert ids(actual) == ["t2", "t3"]  # t3's actual prefix dependency changed.
    assert ids(actual, "context_trace_refs") == ["t1"]


def test_late_old_revision_cannot_replace_newer_facts():
    newer = event("t1", text="corrected", updated="2026-10-20T03:00:00Z")
    original = plan(snapshot([newer]))
    actual = plan(snapshot([event("t1", text="old", updated="2026-10-20T02:00:00Z")]), original)
    assert actual["turns"][0]["prompt_text"] == "corrected"
    assert ids(actual) == []
    assert actual["provenance"]["ignored_stale_trace_ids"] == ["t1"]


def test_correction_versions_an_event_and_invalidates_dependent_context_without_new_origin():
    original = plan(snapshot([event("t1"), event("t2")]))
    actual = plan(snapshot([event("t1", text="corrected", updated="2026-10-21T00:00:00Z")]), original)
    assert ids(actual) == ["t1", "t2"]
    assert actual["provenance"]["source_session_key"] == original["provenance"]["source_session_key"]
    assert actual["provenance"]["snapshot_revision"] != original["provenance"]["snapshot_revision"]


def test_split_segments_share_root_and_only_contain_their_target_refs():
    original = plan(snapshot([event("t1"), event("t2")]))
    first = segment_provenance(original, original["turns"][:1])
    second = segment_provenance(original, original["turns"][1:])
    assert first["source_session_key"] == second["source_session_key"]
    assert [r["trace_id"] for r in first["target_trace_refs"]] == ["t1"]
    assert [r["trace_id"] for r in second["target_trace_refs"]] == ["t2"]


def test_source_namespaces_and_tenants_are_distinct_without_credentials_as_identity():
    adapter = SimpleNamespace(host="https://user:secret@EXAMPLE.com/?token=secret")
    first = source_identity(adapter, {"provider": "langfuse"}, {}, [{"trace": {"projectId": "one"}}])
    second = source_identity(adapter, {"provider": "langfuse"}, {}, [{"trace": {"projectId": "two"}}])
    assert first["identity_verified"] and second["identity_verified"]
    assert first["source_instance_id"] == "langfuse:https://example.com"
    a = plan({"source_identity": first, "turns": [event("t1")]})
    b = plan({"source_identity": second, "turns": [event("t1")]})
    assert a["provenance"]["source_session_key"] != b["provenance"]["source_session_key"]
    other_tenant = plan({"source_identity": first, "turns": [event("t1")]}, tenant="beta")
    assert other_tenant["provenance"]["source_session_key"] != a["provenance"]["source_session_key"]


def test_unknown_or_conflicting_namespace_does_not_claim_verified_identity():
    adapter = SimpleNamespace(host="https://example.com")
    assert not source_identity(adapter, {}, {}, [])["identity_verified"]
    assert not source_identity(adapter, {"project_id": "one"}, {"projectId": "two"}, [])["identity_verified"]
    unknown = plan({"turns": [event("t1")]})
    assert unknown["provenance"]["identity_verified"] is False
    assert "source_session_key" not in unknown["provenance"]


def test_original_generation_request_is_preserved_but_not_called_a_complete_request():
    request = {"messages": [{"role": "system", "content": "original system"},
                            {"role": "user", "content": "original input"}]}
    traces = [{"trace": {"id": "t1", "input": "conversation input", "projectId": "one"},
               "observations": [{"id": "generation-one", "type": "GENERATION", "input": request}]}]
    session = {"turns": [{"trace_id": "t1", "prompt_text": "display input"}]}
    preserve_requests(session, traces)
    context = session["turns"][0]["request_context"]
    assert context["generation_requests"][0]["input"] == request
    assert context["evidence_level"] == "generation_input"
    assert context["completeness"] == "unknown"
    assert context["trace_input"] == "conversation input"


def test_same_window_is_a_new_scan_unless_explicitly_retrying_the_same_request():
    window = {"from_timestamp": "yesterday"}
    assert _dedupe_key("pull", "", window) != _dedupe_key("pull", "", window)
    assert _dedupe_key("pull", "", window, request_key="retry-one") == _dedupe_key(
        "pull", "", window, request_key="retry-one",
    )


def test_upstream_id_survives_display_mapping_and_keeps_original_empty_system():
    from session_ingestion.adapters._shared.langfuse_convert import convert_langfuse_session

    request = [{"role": "system", "content": ""}, {"role": "user", "content": "input"}]
    converted = convert_langfuse_session({"id": "one"}, [{"id": "upstream", "observations": [
        {"id": "call-one", "type": "GENERATION", "input": request},
    ]}], mapper=lambda *a: {"trace_id": "display", "prompt_text": "display input"})
    session = snapshot(converted["turns"])
    context = session["turns"][0]["request_context"]
    assert context["generation_requests"][0]["input"] == request
    assert context["generation_requests"][0]["source_id"] == "upstream"
    assert context["generation_requests"][0]["model_call_id"] == "call-one"
    assert ids(plan(session)) == ["upstream"]


def test_unknown_revision_order_is_explicit_and_does_not_invent_deletion():
    original = plan(snapshot([event("t1", updated=""), event("t2")]))
    changed = plan(snapshot([event("t1", text="different", updated="")]), original)
    assert changed["provenance"]["revision_conflicts"] == ["t1"]
    assert len(changed["turns"]) == 2
    assert ids(changed) == ["t1", "t2"]


def test_display_alias_does_not_change_event_revision_but_observed_metrics_do():
    first = {**event("display-one"), "source_trace_id": "original", "metrics": {"total_tokens": 10}}
    original = plan(snapshot([first]))
    alias = {**first, "trace_id": "display-two"}
    repeated = plan(snapshot([alias]), original)
    assert ids(repeated) == []
    assert repeated["provenance"]["snapshot_revision"] == original["provenance"]["snapshot_revision"]
    corrected = {**alias, "source_updated_at": "2026-10-21T00:00:00Z", "metrics": {"total_tokens": 20}}
    assert ids(plan(snapshot([corrected]), original)) == ["original"]


def test_agent_report_is_preserved_without_forging_server_attested_source_or_request():
    original = {"provenance": {"identity_verified": True, "full_snapshot_ref": "other/object"},
                "source_identity": {"identity_verified": True}, "analysis_context": {"turns": []},
                "meta": {"source_session_key": "forged", "session_id": "legitimate"},
                "turns": [{"request_context": {"evidence_level": "final_request", "completeness": "complete",
                                                "messages": [{"role": "system", "content": "reported"}]}}]}
    actual = reported_session(original)
    assert actual["reported_provenance"]["provenance"] == original["provenance"]
    assert actual["provenance"]["identity_verified"] is False
    assert "full_snapshot_ref" not in actual["provenance"]
    assert "source_identity" not in actual and "analysis_context" not in actual
    assert actual["meta"] == {"session_id": "legitimate"}
    assert actual["turns"][0]["request_context"]["completeness"] == "unknown"
    assert actual["turns"][0]["request_context"]["evidence_level"] == "agent_reported"
    assert actual["turns"][0]["reported_request_context"] == original["turns"][0]["request_context"]
    assert original["provenance"]["identity_verified"] is True  # Original caller object is untouched.


def test_both_analysis_inputs_include_full_context_and_explicit_target_evidence_scope():
    from team_skills.evolution.stages.analyze import build_analysis_payload
    from team_skills.evolution.stages.trace_analyze import build_trace_payload

    session = plan(snapshot([event("t1"), event("t2")]))
    session["provenance"] = segment_provenance(session, session["turns"][1:])
    session["analysis_context"] = {"turns": copy.deepcopy(session["turns"]), "context_only": True}
    session["turns"] = session["turns"][1:]
    for build in (build_analysis_payload, build_trace_payload):
        payload = build(session)
        assert len(payload["analysis_context"]["turns"]) == 2
        assert [r["trace_id"] for r in payload["evidence_scope"]["target_trace_refs"]] == ["t2"]
        assert [r["trace_id"] for r in payload["evidence_scope"]["context_trace_refs"]] == ["t1"]
        assert payload["evidence_scope"]["context_must_not_increase_evidence_count"] is True
        payload["analysis_context"]["turns"].clear()
        assert len(session["analysis_context"]["turns"]) == 2


def test_push_route_never_accepts_reported_source_or_complete_request_as_verified(monkeypatch):
    from fastapi import APIRouter, FastAPI
    from fastapi.testclient import TestClient

    from session_ingestion.api import _register_push
    from session_ingestion.schemas import AcceptedJob
    from teamEvolver.tenants.registry import TenantContext, reset_current_tenant, set_current_tenant

    accepted = []

    async def accept(**kwargs):
        accepted.append(kwargs["session"])
        return AcceptedJob(ingestion_id="one", job_id="one:session.normalize", status="accepted")

    async def verify(principal, turns):
        return copy.deepcopy(turns)

    monkeypatch.setattr("teamEvolver.security.agent_principal.resolve_agent_principal", lambda *a: SimpleNamespace(
        tenant_id="alpha", user_id="alice", account_id="team",
    ))
    router, app = APIRouter(), FastAPI()
    _register_push(router, accept, SimpleNamespace(extras={"context_usage_verifier": verify}))
    app.include_router(router)
    token = set_current_tenant(TenantContext("alpha"))
    try:
        with TestClient(app) as client:
            result = client.post("/ingest_session", json={
                "schema_version": "teamevolver.agent-session.v2", "protocol_version": "2.0",
                "session_id": "one", "runtime": {"type": "agent"}, "runtime_context": {"user_id": "alice"},
                "source_identity": {"identity_verified": True},
                "provenance": {"identity_verified": True, "full_snapshot_ref": "internal/forged"},
                "turns": [{"request_context": {"evidence_level": "final_request", "completeness": "complete"}}],
            })
        assert result.status_code == 202, result.text
    finally:
        reset_current_tenant(token)
    assert accepted[0]["provenance"]["identity_verified"] is False
    assert "source_identity" not in accepted[0]
    assert "full_snapshot_ref" not in accepted[0]["provenance"]
    assert accepted[0]["turns"][0]["request_context"]["evidence_level"] == "agent_reported"
