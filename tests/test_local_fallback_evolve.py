"""End-to-end: the skill evolution flow runs entirely on built-in local storage.

Simulates an OpenViking outage (every viking store build falls back to
``LocalObjectStore``) and drives a full evolution cycle — session drain,
skill grouping, evolution, immediate publish, registry/manifest writes —
without any OpenViking connectivity. This pins the guarantee that skill
evolution keeps working when cloud Volcengine / self-hosted OpenViking /
any server endpoint is down.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

import teamEvolver.evolve.runtime.orchestrator as orchestrator_module
from teamEvolver.evolve.kernel.enums import DecisionAction
from teamEvolver.evolve.kernel.settings import EvolveServerConfig
from teamEvolver.evolve.runtime.orchestrator import EvolveServer
from teamEvolver.evolve.store.object_store import (
    fetch_skill_bundle,
    load_manifest,
)
from teamEvolver.storage import LocalObjectStore


def _session(session_id: str, score: float = 0.7) -> dict:
    return {
        "session_id": session_id,
        "timestamp": f"2026-01-{int(session_id[-1]):02d}T00:00:00+00:00",
        "turns": [
            {
                "turn_num": 1,
                "prompt_text": f"instruction {session_id}",
                "response_text": f"response {session_id}",
            }
        ],
    }


def _fallback_config(tmp_path: Path) -> EvolveServerConfig:
    return EvolveServerConfig(
        storage_backend="viking",
        # OpenViking outage: this endpoint never answers (probe is stubbed to
        # confirm the outage classification below).
        viking_endpoint="http://viking.test",
        storage_fallback_enabled=True,
        storage_local_root=str(tmp_path / "local_store"),
        llm_api_key="test-key",
        publish_mode="direct",
        dataset_synthesis_enabled=False,
        bundle_static_checks_enabled=False,
        use_session_judge=False,
        max_parallel_groups=4,
        history_path=str(tmp_path / "evolve_history.jsonl"),
        processed_log_path=str(tmp_path / "evolve_processed.json"),
    )


@pytest.fixture()
def outage(monkeypatch):
    """Every viking store build observes an OpenViking outage."""
    monkeypatch.setattr(
        "teamEvolver.storage.probe_viking_availability",
        lambda store, *, timeout=3.0: (False, "ConnectError: simulated outage"),
    )


@pytest.fixture()
def no_llm(monkeypatch):
    """LLM stages are stubbed; storage flow is what is under test.

    The summarize stub keeps the real metadata extraction
    (``_skills_referenced`` drives skill grouping) and only skips the LLM call.
    """
    from teamEvolver.evolve.stages.summarize import (
        _extract_session_metadata,
        build_session_trajectory,
    )

    async def fake_summarize(_llm, sessions):
        for session in sessions:
            _extract_session_metadata(session)
            session["_trajectory"] = build_session_trajectory(session)
        return [str(session.get("_summary") or "") for session in sessions]

    async def fake_evolve(
        _llm,
        skill_name,
        _sessions,
        _current_skill,
        _existing_skill_names,
        *,
        evolution_context=None,
    ):
        return {
            "action": DecisionAction.IMPROVE,
            "rationale": f"improve {skill_name}",
            "skill": {
                "name": skill_name,
                "description": f"desc {skill_name}",
                "content": f"# {skill_name}\n\nupdated body\n",
                "category": "general",
            },
            "evidence_classification": {
                "team_skill": [
                    {
                        "claim": f"reusable rule for {skill_name}",
                        "supporting_session_ids": ["s"],
                        "causal_link": "observed",
                    }
                ],
                "user_memory": [],
                "task_requirement": [],
                "agent_runtime": [],
                "insufficient_evidence": [],
            },
        }

    async def fake_create(
        _llm,
        _sessions,
        _existing_skill_names,
        *,
        evolution_context=None,
        library_reader=None,
    ):
        return {
            "action": DecisionAction.CREATE,
            "rationale": "new pattern",
            "skill": {
                "name": "fresh-skill",
                "description": "brand new",
                "content": "# fresh-skill\n\nnew body\n",
                "category": "general",
            },
            "evidence_classification": {
                "team_skill": [
                    {
                        "claim": "reusable new rule",
                        "supporting_session_ids": ["s"],
                        "causal_link": "observed",
                    }
                ],
                "user_memory": [],
                "task_requirement": [],
                "agent_runtime": [],
                "insufficient_evidence": [],
            },
        }

    monkeypatch.setattr(orchestrator_module, "summarize_sessions_parallel", fake_summarize)
    async def summarize_one(_llm, _session):
        return ""

    monkeypatch.setattr(orchestrator_module, "summarize_session", summarize_one)
    monkeypatch.setattr(orchestrator_module, "evolve_skill_from_sessions", fake_evolve)
    monkeypatch.setattr(orchestrator_module, "create_skill_from_sessions", fake_create)


@pytest.mark.anyio
async def test_skill_evolution_completes_on_local_fallback(
    tmp_path: Path, outage, no_llm
) -> None:
    server = EvolveServer(_fallback_config(tmp_path), mock=False)

    # Both the session queue and the skill library live on the built-in store.
    assert isinstance(server._bucket, LocalObjectStore)
    assert isinstance(server._skill_bucket, LocalObjectStore)
    assert server._bucket.root == server._skill_bucket.root

    # Two skill groups (2 sessions / 2 users each, meeting the team-evidence
    # minima) + two no-skill sessions from two users.
    for idx, skill in enumerate(("alpha-skill", "beta-skill")):
        for user_idx, user in enumerate(("alice", "bob")):
            session = _session(f"has-skill-{idx}-{user_idx}")
            session["user_alias"] = user
            session["turns"][0]["read_skills"] = [{"skill_name": skill}]
            server._bucket.put_object(
                f"sessions/sess-{idx}-{user_idx}.json",
                json.dumps(session).encode("utf-8"),
            )
    for user_idx, user in enumerate(("carol", "dave")):
        session = _session(f"no-skill-{user_idx}")
        session["user_alias"] = user
        server._bucket.put_object(
            f"sessions/sess-noskill-{user_idx}.json",
            json.dumps(session).encode("utf-8"),
        )

    summary = await server._run_once()

    # Every branch evolved and published — all writes landed on local storage.
    uploaded = {
        record.get("skill_name")
        for record in summary["evolutions"]
        if record.get("uploaded")
    }
    assert {"alpha-skill", "beta-skill", "fresh-skill"} <= uploaded

    manifest = load_manifest(server._skill_bucket, server._skill_prefix)
    for name in ("alpha-skill", "beta-skill", "fresh-skill"):
        assert name in manifest
        assert server._id_registry.get_version(name) >= 1
        # The published bundle is readable back from the local store
        # (publish -> read round trip, e.g. for Hermes skill sync).
        record = manifest[name]
        bundle = fetch_skill_bundle(server._skill_bucket, server._skill_prefix, name, record)
        assert b"body" in bundle["SKILL.md"]

    # The session queue was drained and consumed on local storage.
    remaining = [obj.key for obj in server._bucket.iter_objects(prefix="sessions/")]
    assert remaining == []

    # Data is physically on disk under the fallback root.
    assert any((tmp_path / "local_store").rglob("SKILL.md"))


@pytest.mark.anyio
async def test_skill_hub_pulls_evolved_skills_from_local_fallback(
    tmp_path: Path, outage, no_llm
) -> None:
    """The consumer side (SkillHub.pull_skills) mirrors the local store into a
    local skills directory — the same flow Hermes uses for team skills."""
    from teamEvolver.skills.hub import SkillHub

    config = _fallback_config(tmp_path)
    server = EvolveServer(config, mock=False)
    skill = {
        "name": "local-born-skill",
        "description": "born during outage",
        "content": "# local-born-skill\n\nbody\n",
        "category": "general",
    }
    assert server._upload_skill(skill, "create_skill") == "uploaded"

    hub = SkillHub.team_from_config(
        type(
            "_Cfg",
            (),
            {
                "sharing_backend": "viking",
                "sharing_viking_endpoint": "http://viking.test",
                "sharing_local_fallback_enabled": True,
                "sharing_local_root": str(tmp_path / "local_store"),
                "sharing_skill_backend": "",
                "sharing_session_backend": "",
                "sharing_viking_api_key": "",
                "sharing_viking_personal_api_key": "",
                "sharing_viking_team_api_key": "",
                "sharing_viking_account": "default",
                "sharing_viking_user": "team",
                "sharing_viking_agent": "team-skill-evolver",
                "sharing_viking_agent_id": "",
                "sharing_viking_root_prefix": "team-skill-evolver",
                "sharing_viking_group_id": "",
                "sharing_viking_customer_id": "",
                "sharing_user_alias": "",
            },
        )()
    )
    assert isinstance(hub._bucket, LocalObjectStore)
    # Point the hub at the same local library root the engine published into
    # (the engine's bucket is a fallback-namespaced directory; the consumer
    # hub must read that exact root to see the skills).
    hub._bucket = server._skill_bucket

    skills_dir = tmp_path / "skills_out"
    hub.pull_skills(str(skills_dir))
    pulled = (skills_dir / "local-born-skill" / "SKILL.md").read_text(encoding="utf-8")
    assert "local-born-skill" in pulled
