from __future__ import annotations

from types import SimpleNamespace

import pytest

from teamEvolver.integrations import skill_sync_adapters


class _Response:
    def raise_for_status(self) -> None:
        return None

    def json(self):
        return {
            "ok": True,
            "results": {
                "tenant-a": {
                    "verification": {
                        "skills": [
                            {
                                "name": "demo-skill",
                                "matched": True,
                                "actual_version": 3,
                                "actual_sha256": "sha",
                                "actual_tree_sha256": "tree",
                            }
                        ]
                    }
                }
            },
        }


class _Client:
    calls = []

    def __init__(self, **_kwargs) -> None:
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, endpoint, **kwargs):
        self.calls.append((endpoint, kwargs))
        return _Response()


@pytest.mark.anyio
async def test_publish_sync_uses_registered_capability_and_exact_endpoint(
    monkeypatch,
) -> None:
    _Client.calls.clear()
    monkeypatch.setattr(
        skill_sync_adapters,
        "list_agents",
        lambda _config: [
            {
                "agent_id": "demo:tenant-a",
                "runtime_type": "demo",
                "status": "active",
                "capability_ids": ["skill.sync.v1"],
                "capability_details": {
                    "skill.sync.v1": {"auth_profile": "demo"}
                },
                "endpoints": {
                    "skill_sync_url": "https://agent.example/custom-sync"
                },
            },
            {
                "agent_id": "ingest-only",
                "capability_ids": ["session.ingest.v1"],
                "endpoints": {},
            },
        ],
    )
    monkeypatch.setattr(skill_sync_adapters.httpx, "AsyncClient", _Client)
    monkeypatch.setenv(
        "TEAMEVOLVER_AGENT_DEMO_SKILL_SYNC_API_KEY",
        "sync-token",
    )

    result = await skill_sync_adapters.sync_published_skill(
        SimpleNamespace(validation_agentshub_api_key=""),
        job_id="job-1",
        expected={
            "name": "demo-skill",
            "version": 3,
            "sha256": "sha",
            "tree_sha256": "tree",
        },
        tenant_ids=["tenant-a"],
    )

    assert result["status"] == "synced"
    assert result["results"]["demo:tenant-a"]["status"] == "synced"
    assert len(_Client.calls) == 1
    endpoint, request = _Client.calls[0]
    assert endpoint == "https://agent.example/custom-sync"
    assert request["json"]["schema_version"] == "teamevolver.skill-changed.v1"
    assert request["json"]["tenant_ids"] == ["tenant-a"]
    assert request["headers"]["Authorization"] == "Bearer sync-token"
    assert request["headers"]["Idempotency-Key"].endswith(":demo:tenant-a")


@pytest.mark.anyio
async def test_publish_sync_reports_no_capable_agents(monkeypatch) -> None:
    monkeypatch.setattr(skill_sync_adapters, "list_agents", lambda _config: [])

    result = await skill_sync_adapters.sync_published_skill(
        SimpleNamespace(validation_agentshub_api_key=""),
        job_id="job-1",
        expected={"name": "demo"},
        tenant_ids=[],
    )

    assert result["status"] == "no_capable_agents"
    assert result["results"] == {}


@pytest.mark.anyio
async def test_runtime_specific_skill_is_not_sent_to_incompatible_agent(
    monkeypatch,
) -> None:
    _Client.calls.clear()
    monkeypatch.setattr(
        skill_sync_adapters,
        "list_agents",
        lambda _config: [
            {
                "agent_id": "hermes:profile",
                "runtime_type": "hermes",
                "runtime_class": "hermes",
                "status": "active",
                "capability_ids": ["skill.sync.v1"],
                "endpoints": {
                    "skill_sync_url": "https://agent.example/custom-sync"
                },
            }
        ],
    )
    monkeypatch.setattr(skill_sync_adapters.httpx, "AsyncClient", _Client)

    result = await skill_sync_adapters.sync_skill_event(
        SimpleNamespace(validation_agentshub_api_key=""),
        {
            "event_id": "event-runtime-specific",
            "mutation_id": "mutation-1",
            "action": "publish",
            "skills": [
                {
                    "name": "agentshub-only",
                    "runtime_policy": {
                        "supported_runtimes": ["agentshub"],
                        "distribution_runtimes": ["agentshub"],
                    },
                }
            ],
            "tenant_ids": [],
        },
    )

    assert result["status"] == "synced"
    assert result["results"]["hermes:profile"]["status"] == "cancelled"
    assert _Client.calls == []
