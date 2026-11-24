"""Unpublished candidates are reusable assets, with frozen merge inputs."""
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from team_skills.candidates.repository import CandidateStateError
from team_skills.config import SkillsModuleConfig
from team_skills.evolution.agent.contract import validate_final
from team_skills.evolution.agent.runner import _accept_final
from team_skills.evolution.agent.tools import ToolContext, ToolRegistry
from team_skills.evolution.pending import PendingCandidates
from team_skills.evolution.source_context import contract_card, source_contract
from team_skills.evolution.stages.analyze import build_analysis_payload


def candidate(ident, *, status="open", connection="agent1"):
    return {
        "candidate_id": ident, "skill_name": ident, "revision": 1, "base_version": 0,
        "tree_sha256": ident, "status": status,
        "detail": {"execution_provenance": [{"connection_id": connection, "entry_agent_id": "entry"}]},
    }


def catalog(items, bundles):
    @asynccontextmanager
    async def tenant_tx(tenant):
        assert tenant == "alpha"
        yield None

    repo = SimpleNamespace(
        require=AsyncMock(side_effect=lambda conn, ident: next(i for i in items if i["candidate_id"] == ident)),
        read_bundle=AsyncMock(side_effect=lambda conn, ident: bundles[ident]),
    )
    return PendingCandidates(repo, SimpleNamespace(tenant_tx=tenant_tx), "alpha", items)


def bundle(name, **files):
    return {"SKILL.md": f"---\nname: {name}\ndescription: Useful task\n---\n{name} body".encode(), **files}


def merge():
    return {
        "action": "merge_skill", "target_candidate_id": "first", "source_candidate_ids": ["second"],
        "evidence_classification": {"team_skill": [{"claim": "same capability"}]},
    }


def test_merge_protocol_accepts_selection_without_fabricating_skill_body():
    decision, errors = validate_final(merge(), stage="create_skill")
    assert not errors and "skill" not in decision
    ctx = ToolContext(stage="create_skill")
    assert _accept_final(decision, ctx, "create_skill")[0] is None
    ctx.read_candidate_ids = ["first", "second"]
    assert _accept_final(decision, ctx, "create_skill")[0] == decision
    invalid = merge() | {"source_candidate_ids": ["first"]}
    assert validate_final(invalid, stage="create_skill")[0] is None
    assert _accept_final({"name": "wrong", "content": "x"}, ToolContext(stage="merge", skill_name="first"),
                         "merge")[0] is None


@pytest.mark.asyncio
async def test_candidate_reader_is_available_to_both_planning_stages():
    pending = catalog([candidate("first")], {"first": bundle("first", **{"run.py": b"print(1)"})})
    for stage in ("create_skill", "evolve_skill"):
        ctx = ToolContext(stage=stage, candidate_reader=pending.read)
        observations = await ToolRegistry(ctx).execute([{"tool": "read_candidate", "args": {"candidate_id": "first"}}])
        assert "first body" in str(observations)
        assert ctx.read_candidate_ids == ["first"]
        assert "bundle" not in (await pending.read("first"))["skill"]
        assert (await pending.read("first"))["editable_files"] == {"run.py": "print(1)"}


@pytest.mark.asyncio
async def test_merge_preserves_auxiliary_files_and_detects_collisions():
    items = [candidate("first"), candidate("second")]
    pending = catalog(items, {
        "first": bundle("first", **{"run.py": b"one", "opaque.bin": b"\x00\xff"}),
        "second": bundle("second", **{"run.py": b"two", "reference.md": b"useful"}),
    })
    target, files, sources, conflicts, snapshots = await pending.prepare(
        merge(), context={"execution_provenance": items[0]["detail"]["execution_provenance"]},
        config=SkillsModuleConfig(1, {}), current=None,
    )
    assert files["opaque.bin"] == b"\x00\xff" and files["reference.md"] == b"useful"
    assert conflicts == {"run.py"} and sources[0]["editable_files"]["run.py"] == "two"
    assert target["candidate_id"] == "first" and snapshots[1]["revision"] == 1


@pytest.mark.asyncio
@pytest.mark.parametrize("change", [
    {"status": "validating"}, {"base_version": 4},
    {"detail": {"execution_provenance": [{"connection_id": "another-agent"}]}},
])
async def test_merge_refuses_inflight_published_or_unrelated_sources(change):
    items = [candidate("first"), candidate("second") | change]
    pending = catalog(items, {"first": bundle("first"), "second": bundle("second")})
    with pytest.raises(CandidateStateError):
        await pending.prepare(
            merge(), context={"execution_provenance": items[0]["detail"]["execution_provenance"]},
            config=SkillsModuleConfig(1, {}), current=None,
        )


@pytest.mark.asyncio
async def test_catalog_reads_all_pages_and_rejects_changed_revision():
    pending = catalog([candidate("first")], {"first": bundle("first")})
    pending.repo.list = AsyncMock(side_effect=[([candidate("first")], 2), ([candidate("second")], 2)])
    loaded = await PendingCandidates.load(pending.repo, pending.tx, "alpha")
    assert list(loaded.items) == ["first", "second"]
    assert pending.repo.list.call_args.kwargs["offset"] == 1
    pending.repo.require.return_value = None
    pending.repo.require.side_effect = lambda *args: candidate("first") | {"revision": 2}
    with pytest.raises(CandidateStateError, match="changed"):
        await loaded.read("first")
    with pytest.raises(CandidateStateError, match="outside"):
        await loaded.read("other-tenant-candidate")


def test_source_contract_keeps_schema_and_marks_video_visibility_without_secrets():
    session = {
        "session_id": "s1", "runtime": {
            "connection_id": "agent1", "environment": {"config": {"secret": "NEVER-COPY"}},
            "agent_graph": {"entry_agent_id": "entry", "agents": {
                "entry": {"version": 13, "system": "Use visual_{shot_id}, hook_000_003; OCR allowed",
                          "model": {"api_key": "NEVER-COPY"}}}},
            "materials_missing": [{"reason": "input bytes unavailable", "url": "NEVER-COPY"}],
        },
        "turns": [{"prompt_text": "Analyze video", "response_text": "result",
                   "attachments": [{"type": "video", "source": {"url": "NEVER-COPY"}}]}],
    }
    contract = source_contract(session)
    assert contract["input_modalities"] == ["video"] and "text_only" in contract["analysis_visibility"]
    assert "NEVER-COPY" not in str(contract)
    assert build_analysis_payload(session)["source_contract"] == contract
    assert contract_card([session, session | {"session_id": "s2"}]).count("OCR allowed") == 1


@pytest.mark.asyncio
async def test_failed_candidate_read_cannot_be_used_to_create_an_alias():
    from team_skills.evolution.agent.runner import run_skill_agent

    pending = catalog([candidate("first"), candidate("second")], {
        "first": bundle("first"), "second": bundle("second"),
    })
    responses = iter([
        {"type": "tool_calls", "calls": [{"tool": "read_candidate", "args": {"candidate_id": "wrong-id"}}]},
        {"type": "final", "decision": {
            "action": "create_skill", "skill": {"name": "another-alias", "content": "same task"},
            "candidate_distinction": "Existing candidates cannot be read, so create another one",
            "evidence_classification": {"team_skill": [{"claim": "same capability"}]},
        }},
        {"type": "tool_calls", "calls": [
            {"tool": "read_candidate", "args": {"candidate_id": ident}} for ident in pending.items
        ]},
        {"type": "final", "decision": merge()},
    ])
    llm = SimpleNamespace(chat=AsyncMock(side_effect=lambda *a, **kw: json.dumps(next(responses))))
    result = await run_skill_agent(
        llm, stage="create_skill", system_prompt="Plan a reusable skill", max_rounds=4,
        candidate_reader=pending.read, evolution_context={
            "pending_candidates": pending.cards(),
            "execution_provenance": candidate("first")["detail"]["execution_provenance"],
        },
    )
    assert result["action"] == "merge_skill"
    assert result["agent_audit"][0]["candidate_reads"][0]["ok"] is False


@pytest.mark.asyncio
async def test_compaction_preserves_exact_candidate_ids_and_source_contract(monkeypatch):
    from team_skills.evolution.agent import runner

    pending = catalog([candidate("first")], {"first": bundle("first")})
    monkeypatch.setattr(runner, "MODEL_EVIDENCE_BYTES", 200)
    monkeypatch.setattr(runner, "bounded_evidence", AsyncMock(return_value={"summary": "lossy summary"}))

    async def chat(messages, **kwargs):
        text = json.dumps(messages, ensure_ascii=False)
        assert '"candidate_id"' in str(messages) and "first" in text
        assert "hook_000_003" in text
        return json.dumps({"type": "final", "decision": {"action": "skip"}})

    await runner.run_skill_agent(
        SimpleNamespace(chat=chat), stage="create_skill", system_prompt="Plan", max_rounds=1,
        sessions=[{"session_id": "s1", "_source_contract": {"agents": [
            {"id": "agent1", "system": "opening_hook must cite hook_000_003"},
        ]}}],
        candidate_reader=pending.read, evolution_context={"pending_candidates": pending.cards()},
    )
