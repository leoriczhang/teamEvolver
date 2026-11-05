import asyncio

from team_replay.adapters.managed_agents import ManagedAgentsFactory
from team_replay.engine import execute_pair
from team_replay.hooks import ReplayContext, ReplayTreatment
from team_replay.metrics import compare_efficiency
from team_replay.policy import progressive_replay_decision
from team_replay.runtime_matrix import evaluate_runtime_compatibility
from team_skills.library.bundle import encode_bundle_payload
from tests.support.managed_agents import FakeEffects, FakeMA, save_artifact


def test_full_ma_isolated_branches_reuse_each_session_and_real_skill_bundle():
    server, effects, cleaned = FakeMA(), FakeEffects(), []
    root = {"id": "root", "version": 7, "name": "editor", "system": "Plan and ask for confirmation",
            "model": {"id": "model"}, "skills": [], "tools": [],
            "multiagent": {"type": "coordinator", "agents": [{"type": "agent", "id": "writer", "version": 3}]}}
    writer = {"id": "writer", "version": 3, "name": "writer", "system": "Write", "model": {"id": "model"},
              "tools": [], "skills": [{"type": "custom", "skill_id": "original-skill", "version": "1"}]}
    # Deliberately root first: persistence order must not determine traversal.
    graph = {"entry_agent_id": "root", "agents": {"root": root, "writer": writer}}
    record = {"connection_id": "generic", "connection_revision": 1, "runtime_hash": "hash",
              "config": {"replay_enabled": True, "replay_vault_ids": []},
              "agent_graph": graph, "environment": {"id": "env", "config": {"type": "cloud", "env": {}}},
              "replay_environment": {"id": "replay-env", "config": {"type": "cloud"}},
              "skill_bindings": {"note-craft": {"id": "original-skill", "version": "1", "agents": ["writer"]}}}
    async def client(record):
        return server.client()
    async def cleanup(connection, owner, client):
        cleaned.append((connection, owner))
    cap = {"run": lambda coroutine, timeout=None: asyncio.run(coroutine), "client": client,
           "effects": lambda connection, owner: effects, "cleanup": cleanup,
           "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    factory = ManagedAgentsFactory({"case": record}, cap)
    bundle = encode_bundle_payload({"SKILL.md": b"---\nname: note-craft\ndescription: Notes\n---\nImproved\n"})
    for branch in ("baseline", "candidate"):
        context = ReplayContext(
            request_id=branch, runtime_type="managed-agents",
            treatment=ReplayTreatment(branch, {"name": "note-craft", "bundle": bundle}),
            materials=(), context_snapshot={}, timeout_seconds=30,
            source_session={"runtime": {"type": "managed-agents", "connection_id": "generic", "agent_graph": graph}},
        )
        session = factory.open(context)
        first = session.send("write a note")
        second = session.send("yes, continue")
        assert first.metrics["total_tokens"] == second.metrics["total_tokens"] == 12
        assert first.trace_id.split(":")[0] == second.trace_id.split(":")[0]
        session.close()
    assert len(server.sessions) == 2
    assert len(server.agents) == 2
    for agent in server.agents.values():
        assert agent["skills"][0]["skill_id"] != "original-skill"
        assert agent["system"] == writer["system"]
    for session in server.sessions.values():
        assert session["agent"]["system"] == root["system"]
        assert session["agent"]["version"] == 7
        assert "model" not in session["agent"] and "base_agent" not in session["agent"]
        assert session["agent"]["multiagent"]["agents"][0]["id"] in server.agents
        assert "checklist" not in str(session).lower()
    assert len(cleaned) == 2
    assert all(b"Improved" not in data for data in server.skills.values())  # compressed Bundle, not system injection


def test_every_managed_connection_needs_its_own_verdict():
    policy = {"required_runtimes": ["managed-agents"], "required_targets": [
        {"connection_id": "first", "connection_revision": 1}, {"connection_id": "second", "connection_revision": 1},
    ]}
    result = {"runtime_type": "managed-agents", "status": "evaluated", "accepted": True, "verdict": "accept",
              "target_validation": {"first": {"status": "evaluated", "accepted": True, "verdict": "accept",
                                              "connection_revision": 1}}}
    gate = evaluate_runtime_compatibility(policy, [result])
    assert gate["status"] == "blocked"
    assert gate["target_matrix"]["second"]["status"] == "missing"


def test_real_ma_transport_runs_full_nine_and_five_turn_branches_before_comparing():
    server, cleaned = FakeMA(), []
    graph = {"entry_agent_id": "root", "agents": {
        "root": {"id": "root", "version": 1, "skills": [], "tools": []},
    }}
    record = {
        "connection_id": "one", "config": {}, "agent_graph": graph, "skill_bindings": {},
        "environment": {"config": {"type": "cloud"}}, "replay_environment": {"id": "env", "config": {}},
    }

    async def client(record):
        return server.client()

    async def cleanup(connection, owner, client):
        cleaned.append(owner)

    cap = {
        "client": client, "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
        "run": lambda coroutine, timeout=None: asyncio.run(coroutine),
        "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact,
    }
    contexts = {
        branch: ReplayContext(
            request_id=branch, runtime_type="managed-agents", treatment=ReplayTreatment(branch, None),
            materials=(), context_snapshot={}, timeout_seconds=30,
            source_session={"runtime": {"connection_id": "one", "agent_graph": graph}},
        ) for branch in ("baseline", "candidate")
    }
    case = {"query": "Create the full video", "checks": [{"id": "R1", "text": "Deliver a video", "kind": "output"}]}

    def judge(**kwargs):
        messages = kwargs["messages"]
        root_user = next(m["event_id"] for m in messages if m["role"] == "user")
        ident = root_user.split(":")[0]
        branch = next(s["tags"][0]["value"] for key, s in server.sessions.items() if key == ident)
        done = len(kwargs["interactions"]) >= (9 if branch == "baseline" else 5)
        return {"judge": "model", "items": [
            {"id": "R1", "satisfied": done, "evidence": "Actual video" if done else ""},
        ]}

    results = execute_pair(
        ManagedAgentsFactory({"case": record}, cap), contexts, case, harness={}, max_interactions=20,
        judge=judge, feedback=lambda **kwargs: "Continue and deliver the completed video",
    )
    assert all(r["completed"] for r in results.values())
    assert results["baseline"]["interaction_turns"] == 9
    assert results["candidate"]["interaction_turns"] == 5
    assert results["baseline"]["total_tokens"] == 108
    assert results["candidate"]["total_tokens"] == 60
    checklists = {b: r["checklist_report"] for b, r in results.items()}
    efficiency = compare_efficiency(results["baseline"], results["candidate"], checklists=checklists)
    policy = progressive_replay_decision(
        efficiency=efficiency, baseline_checklist=checklists["baseline"], candidate_checklist=checklists["candidate"],
    )
    assert efficiency["comparison_ready"] and policy["decision_basis"] == "interaction_turns_decreased"
    assert len(server.sessions) == 2 and sorted(cleaned) == ["baseline", "candidate"]
