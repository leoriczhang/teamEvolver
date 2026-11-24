import asyncio
import base64
import hashlib
import io
import json
import zipfile

import httpx
import pytest

from team_replay.adapters.managed_agents import ManagedAgentsFactory
from team_replay.engine import execute_pair
from team_replay.hooks import ReplayContext, ReplayTreatment, ReplayUnsupported
from team_replay.metrics import compare_efficiency
from team_replay.policy import progressive_replay_decision
from team_replay.runtime_matrix import evaluate_runtime_compatibility
from team_skills.library.bundle import encode_bundle_payload
from tests.support.managed_agents import FakeEffects, FakeMA, save_artifact


def test_mixed_materials_mount_original_bytes_and_send_paths_without_document_coercion():
    class Server(FakeMA):
        uploads = []

        def respond(self, request):
            if request.method == "POST" and request.url.path.endswith("/files"):
                self.uploads.append(request.content)
                return httpx.Response(200, json={"id": "file-" + str(len(self.uploads))})
            return super().respond(request)

    server = Server()
    graph = {"entry_agent_id": "root", "agents": {
        "root": {"id": "root", "version": 1, "skills": [], "tools": []},
    }}
    record = {
        "connection_id": "one", "config": {}, "agent_graph": graph, "skill_bindings": {},
        "environment": {"config": {"type": "cloud"}}, "replay_environment": {"id": "env", "config": {}},
    }

    async def client(record):
        return server.client()

    async def cleanup(*args):
        pass

    cap = {"run": lambda coroutine, timeout=None: asyncio.run(coroutine), "client": client,
           "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
           "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    originals = {"mnt/session/uploads/screenshot.png": b"\x89PNG\r\n\x1a\noriginal pixels",
                 "uploads/report.pdf": b"%PDF-original", "assets/music.mp3": b"ID3-original"}
    context = ReplayContext(
        request_id="materials", runtime_type="managed-agents", treatment=ReplayTreatment("baseline", None),
        materials=tuple({"path": path, "sha256": hashlib.sha256(raw).hexdigest(),
                         "content_b64": base64.b64encode(raw).decode()} for path, raw in originals.items()),
        context_snapshot={}, timeout_seconds=30,
        source_session={"runtime": {"connection_id": "one", "agent_graph": graph}},
    )
    session = ManagedAgentsFactory({"case": record}, cap).open(context)
    try:
        session.send("Use the provided materials")
        session.send("Continue")
        mounted = next(iter(server.sessions.values()))["resources"]
        assert [r["mount_path"] for r in mounted] == ["screenshot.png", "report.pdf", "assets/music.mp3"]
        assert all(raw in uploaded for raw, uploaded in zip(originals.values(), server.uploads, strict=True))
        writes = [json.loads(r.content)["events"][0]["content"] for r in server.requests
                  if r.method == "POST" and r.url.path.endswith("/events")]
        assert all(block["type"] == "text" for block in writes[0])
        assert all(path in str(writes[0]) for path in originals)
        assert all("/mnt/session/uploads/" + r["mount_path"] in str(writes[0]) for r in mounted)
        assert writes[1] == [{"type": "text", "text": "Continue"}]
    finally:
        session.close()


@pytest.mark.parametrize("branch", ["baseline", "candidate"])
def test_treatment_replaces_all_recorded_references_even_when_current_targets_differ(branch):
    server = FakeMA()
    graph = {"entry_agent_id": "root", "agents": {
        "root": {"id": "root", "version": 1, "skills": [], "tools": [],
                 "multiagent": {"agents": [{"type": "agent", "id": "writer", "version": 1}]}},
        "writer": {"id": "writer", "version": 1, "skills": [
            {"type": "custom", "skill_id": "original-core", "version": "1"}], "tools": []},
    }}
    record = {
        "connection_id": "one", "config": {}, "agent_graph": graph,
        "target_skill_name": "core", "target_agents": ["root"],
        "skill_bindings": {"core": {"id": "original-core", "agents": ["root"]}},
        "environment": {"config": {"type": "cloud"}},
        "replay_environment": {"id": "replay-env", "config": {}},
    }
    async def client(record):
        return server.client()
    async def cleanup(*args):
        pass
    cap = {"run": lambda coroutine, timeout=None: asyncio.run(coroutine), "client": client,
           "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
           "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    skill = {"name": "core", "bundle": encode_bundle_payload({"SKILL.md": b"Core"})}
    context = ReplayContext(request_id=branch, runtime_type="managed-agents",
                            treatment=ReplayTreatment(branch, skill), materials=(), context_snapshot={},
                            timeout_seconds=30, source_session={"runtime": {
                                "connection_id": "one", "agent_graph": graph}})
    session = ManagedAgentsFactory({"case": record}, cap).open(context)
    try:
        session.send("execute the task")
        root = next(iter(server.sessions.values()))["agent"]
        writer = next(iter(server.agents.values()))
        assert root["skills"] == writer["skills"]
        assert root["skills"][0]["skill_id"] != "original-core"
    finally:
        session.close()


@pytest.mark.parametrize("source", ["embedded", "skill_mount", "openviking"])
@pytest.mark.parametrize("treatment_runtime", [False, True])
def test_skill_replay_always_freezes_recorded_runtime_and_independent_artifact(source, treatment_runtime):
    from team_ma.runtime_binding import bind_runtime

    original = {"SKILL.md": b"---\nname: core\ndescription: Core\n---\nCore",
                "runtime/bootstrap.sh": b"echo recorded-runtime"}
    treatment = {"SKILL.md": b"Prompt-only baseline"}
    if treatment_runtime:
        treatment["runtime/bootstrap.sh"] = b"echo candidate-runtime"
    archive = io.BytesIO()
    with zipfile.ZipFile(archive, "w") as zipped:
        for path, data in original.items():
            zipped.writestr("core/" + path, data)
    class Server(FakeMA):
        def respond(self, request):
            if request.method == "GET" and request.url.path.endswith("skills/original-core/versions/1/content"):
                assert source != "openviking"
                return httpx.Response(200, content=archive.getvalue())
            return super().respond(request)
    server = Server()
    graph = {"entry_agent_id": "root", "agents": {
        "root": {"id": "root", "version": 1, "tools": [], "skills": [
            {"type": "custom", "skill_id": "original-core", "version": "1"}]},
    }}
    binding = {"skill_name": "core", "source": source, "working_directory": "/workspace/example",
               "paths": [{"bundle_prefix": "runtime/", "workspace_prefix": "runtime/"}],
               "bootstrap": "bash runtime/bootstrap.sh"}
    if source == "openviking":
        data = b"echo independent-runtime"
        binding["artifact"] = {
            "base_url": "https://viking.example", "uri": "viking://user/a/resources/runtime/archive.tar.xz",
            "sha256": "a" * 64, "bytes": 100,
            "files": [{"path": "runtime/bootstrap.sh", "bytes": len(data),
                       "sha256": hashlib.sha256(data).hexdigest()}],
        }
    record = {
        "connection_id": "one", "config": {"runtime_binding": binding},
        "agent_graph": graph, "target_skill_name": "core",
        "skill_bindings": {"core": {"id": "original-core", "agents": ["root"]}},
        "environment": {"config": {"type": "cloud"}},
        "replay_environment": {"id": "replay-env", "config": {}},
    }
    async def client(record):
        return server.client()
    async def cleanup(*args):
        pass
    cap = {"run": lambda coroutine, timeout=None: asyncio.run(coroutine), "client": client,
           "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
           "runtime_binding": bind_runtime, "replay_capability": lambda *args: {"supported": True},
           "save_artifact": save_artifact}
    context = ReplayContext(
        request_id="minimal", runtime_type="managed-agents", treatment=ReplayTreatment("baseline", {
            "name": "core", "bundle": encode_bundle_payload(treatment)}),
        materials=(), context_snapshot={}, timeout_seconds=30,
        source_session={"runtime": {"connection_id": "one", "agent_graph": graph}},
    )
    session = ManagedAgentsFactory({"case": record}, cap).open(context)
    try:
        session.send("execute the task")
        actual = next(iter(server.sessions.values()))["environment"]["config"]
        expected_binding = dict(binding)
        if source == "skill_mount":
            expected_binding.update(source="embedded", archive_compression="xz")
        expected, tree = bind_runtime({"type": "cloud", "tos": None}, expected_binding,
                                      original)
        assert actual == expected and session.runtime_tree == tree
        assert "/mnt/skills/core" not in actual["setup_script"]
        assert record["config"]["runtime_binding"]["source"] == source
    finally:
        session.close()


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
        assert set(session["agent"]) == {"type", "id", "version", "skills", "multiagent", "system", "model", "tools"}
        assert session["agent"]["version"] == 7
        assert session["agent"]["model"] == root["model"] and "base_agent" not in session["agent"]
        assert session["agent"]["multiagent"]["agents"][0]["id"] in server.agents
        assert "checklist" not in str(session).lower()
    assert len(cleaned) == 2
    assert all(b"Improved" not in data for data in server.skills.values())  # compressed Bundle, not system injection


def test_single_agent_pair_pins_recorded_execution_and_only_changes_target_skill():
    server = FakeMA()
    graph = {"entry_agent_id": "root", "agents": {
        "root": {"id": "root", "version": 7, "system": "Production instruction",
                 "model": {"id": "pinned-model"}, "tools": [{"type": "shell"}],
                 "skills": [{"type": "custom", "skill_id": "original", "version": "1"},
                            {"type": "custom", "skill_id": "unchanged", "version": "4"}]},
    }}
    record = {"connection_id": "one", "config": {"replay_vault_ids": ["source-vault"]}, "agent_graph": graph,
              "environment": {"id": "env", "config": {
                  "type": "cloud", "setup_script": "echo fixed-runtime",
                  "networking": {"type": "unrestricted"}, "tos": {"bucket": "production", "prefix": "shared"}}},
              "replay_environment": {"id": "env", "config": {
                  "networking": {"type": "limited"}, "tos": {"bucket": "old-replay"}}}, "target_skill_name": "demo",
              "skill_bindings": {"demo": {"id": "original", "agents": ["root"]}}}

    async def client(record):
        return server.client()

    async def cleanup(*args):
        pass

    cap = {"run": lambda coroutine, timeout=None: asyncio.run(coroutine), "client": client,
           "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
           "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    snapshots = []
    for branch in ("baseline", "candidate"):
        context = ReplayContext(
            request_id=branch, runtime_type="managed-agents", treatment=ReplayTreatment(branch, {
                "name": "demo", "bundle": encode_bundle_payload({"SKILL.md": branch.encode()})}),
            materials=(), context_snapshot={}, timeout_seconds=30,
            source_session={"runtime": {"connection_id": "one", "agent_graph": graph}},
        )
        session = ManagedAgentsFactory({"case": record}, cap).open(context)
        try:
            snapshots.append(session.send("execute").execution_snapshot)
        finally:
            session.close()
    assert not server.agents and len(server.sessions) == 2
    for value in server.sessions.values():
        assert set(value["agent"]) == {"type", "id", "version", "skills", "system", "model", "tools"}
        for key in ("system", "model", "tools"):
            assert value["agent"][key] == graph["agents"]["root"][key]
        assert value["agent"]["skills"][0] == graph["agents"]["root"]["skills"][1]
    assert snapshots[0]["session_id"] != snapshots[1]["session_id"]
    assert snapshots[0]["skills"] != snapshots[1]["skills"]
    assert snapshots[0]["environment_config_sha256"] == snapshots[1]["environment_config_sha256"]
    assert snapshots[0]["agent_execution_config_sha256"] == snapshots[1]["agent_execution_config_sha256"]
    assert snapshots[0]["agent_id"] == snapshots[1]["agent_id"] == "root"
    assert snapshots[0]["vault_ids"] == snapshots[1]["vault_ids"] == ["source-vault"]
    for value in server.sessions.values():
        assert value["environment"]["id"] == "env"
        assert value["environment"]["config"]["tos"] is None
        assert value["environment"]["config"]["networking"] == {"type": "unrestricted"}
    assert record["environment"]["config"]["tos"]["bucket"] == "production"


def test_replay_readback_rejects_cloud_model_instead_of_recorded_session_override():
    class ChangedModelMA(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "GET" and "/sessions/" in request.url.path:
                value = response.json()
                value["agent"]["model"] = {"id": "cloud-model"}
                return httpx.Response(200, json=value)
            return response

    server = ChangedModelMA()
    graph = {"entry_agent_id": "root", "agents": {
        "root": {"id": "root", "version": 7, "model": {"id": "recorded-model"}, "skills": []},
    }}
    record = {"connection_id": "one", "config": {}, "agent_graph": graph,
              "environment": {"config": {"type": "cloud"}},
              "replay_environment": {"id": "env", "config": {}}, "skill_bindings": {}}

    async def client(record):
        return server.client()

    async def cleanup(*args):
        pass

    cap = {"run": lambda coroutine, timeout=None: asyncio.run(coroutine), "client": client,
           "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
           "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    context = ReplayContext(request_id="drift", runtime_type="managed-agents",
                            treatment=ReplayTreatment("candidate", None), materials=(), context_snapshot={},
                            timeout_seconds=30, source_session={"runtime": {
                                "connection_id": "one", "agent_graph": graph,
                            }})
    session = ManagedAgentsFactory({"case": record}, cap).open(context)
    try:
        with pytest.raises(ReplayUnsupported, match="readback differs"):
            session.send("execute")
        assert server.sessions[next(iter(server.sessions))]["agent"]["model"] == {"id": "recorded-model"}
    finally:
        session.close()


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
        round_number = int((kwargs["harness"].get("previous_checklist_report") or {}).get("rounds", 0)) + 1
        done = round_number >= (9 if branch == "baseline" else 5)
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
