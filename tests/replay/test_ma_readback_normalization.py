"""Native Session readbacks expand member references and omit display metadata."""
import asyncio
import copy

import httpx
import pytest

from team_replay.adapters.managed_agents import ManagedAgentsFactory
from team_replay.hooks import ReplayContext, ReplayTreatment, ReplayUnsupported
from tests.support.managed_agents import FakeEffects, FakeMA, save_artifact


@pytest.mark.parametrize("drift", [
    "", "root_skill", "child_skill", "child_model", "child_system", "child_id", "vault", "vault_order",
    "shared_output", "environment",
])
def test_native_readback_normalization_preserves_execution_guards(drift):
    child = {"id": "writer", "version": 3, "name": "Writer", "system": "Write notes",
             "model": {"id": "fixed-model"}, "tools": [],
             "skills": [{"type": "custom", "skill_id": "notes", "version": "2", "display_name": "Notes"}]}
    root = {"id": "root", "version": 2, "tools": [], "skills": [],
            "multiagent": {"agents": [{"type": "agent", "id": "writer", "version": 3}]}}
    graph = {"entry_agent_id": "root", "agents": {"root": root, "writer": child}}

    class NativeReadbackMA(FakeMA):
        def respond(self, request):
            response = super().respond(request)
            if request.method == "GET" and request.url.path.endswith(tuple(self.sessions)):
                value = response.json()
                config = value["environment"]["config"]
                # Native overrides merge with the original Environment; null
                # explicitly clears TOS and is omitted in readback.
                if "tos" not in config:
                    config["tos"] = {"bucket": "production", "prefix": "shared"}
                elif config["tos"] is None:
                    config.pop("tos")
                if drift == "shared_output":
                    config["tos"] = {"bucket": "production", "prefix": "shared"}
                elif drift == "environment":
                    config["setup_script"] = "different"
                agent = value["agent"]
                agent.pop("skills", None)
                expanded = agent["multiagent"]["agents"][0]
                expanded.update({key: copy.deepcopy(child[key]) for key in ("name", "system", "model", "skills")})
                expanded["skills"][0].pop("display_name")
                if drift == "root_skill":
                    agent["skills"] = [{"type": "custom", "skill_id": "unexpected", "version": "1"}]
                elif drift == "vault":
                    value["vault_ids"] = ["unexpected"]
                elif drift == "vault_order":
                    value["vault_ids"] = list(reversed(value["vault_ids"]))
                elif drift == "child_skill":
                    expanded["skills"][0]["version"] = "999"
                elif drift == "child_model":
                    expanded["model"]["id"] = "other-model"
                elif drift == "child_system":
                    expanded["system"] = "Other instructions"
                elif drift == "child_id":
                    expanded["id"] = "other"
                return httpx.Response(200, json=value)
            return response

    server = NativeReadbackMA()
    record = {"connection_id": "one", "config": {"replay_vault_ids": ["first", "second"]},
              "agent_graph": graph, "skill_bindings": {},
              "environment": {"config": {"type": "cloud", "tos": {"bucket": "production", "prefix": "shared"}}},
              "replay_environment": {"id": "env", "config": {}}}

    async def client(record):
        return server.client()

    async def cleanup(*args):
        pass

    capabilities = {"run": lambda coro, timeout=None: asyncio.run(coro), "client": client,
                    "effects": lambda *args: FakeEffects(), "cleanup": cleanup,
                    "replay_capability": lambda *args: {"supported": True}, "save_artifact": save_artifact}
    context = ReplayContext(
        request_id="native-normalization", runtime_type="managed-agents", treatment=ReplayTreatment("baseline", None),
        materials=(), context_snapshot={}, timeout_seconds=30,
        source_session={"runtime": {"connection_id": "one", "agent_graph": graph}},
    )
    session = ManagedAgentsFactory({"case": record}, capabilities).open(context)
    try:
        if drift:
            with pytest.raises(ReplayUnsupported, match="readback differs"):
                session.send("write notes")
        else:
            assert session.send("write notes").response
    finally:
        session.close()


def test_skill_display_name_is_not_execution_identity():
    from team_replay.adapters.managed_agents import _same_agent_fields

    desired = {"skills": [{"type": "custom", "skill_id": "notes", "version": 2, "display_name": "Notes"}]}
    actual = {"skills": [{"type": "custom", "skill_id": "notes", "version": 2}]}
    assert _same_agent_fields(actual, desired, {})
    actual["skills"][0]["skill_id"] = "other"
    assert not _same_agent_fields(actual, desired, {})
    assert _same_agent_fields({"skills": None}, {"skills": []}, {})
