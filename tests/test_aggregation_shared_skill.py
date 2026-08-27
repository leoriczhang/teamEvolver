from __future__ import annotations

import asyncio
from types import SimpleNamespace

from teamEvolver.aggregation.service import AggregationRun, MemoryAggregationService
from teamEvolver.aggregation.staging import (
    StagingInventory,
    StagingSnapshot,
    StagingSource,
)
from teamEvolver.aggregation.state import AggregationState


def _service() -> MemoryAggregationService:
    return MemoryAggregationService(
        SimpleNamespace(
            aggregation_max_users_per_batch=12,
            aggregation_phase1_concurrency=1,
            aggregation_merge_concurrency=1,
            aggregation_staging_dir="staging",
            sharing_viking_endpoint="https://openviking.example",
            sharing_viking_user="team",
        )
    )


def test_existing_shared_skill_is_the_run_source_of_truth() -> None:
    service = _service()

    class Client:
        async def get_skill(self, *, skill_name):
            assert skill_name == "team-memory-okf"
            return {
                "ok": True,
                "result": {
                    "content": "published body",
                    "revision": "published-revision",
                },
            }

    result = asyncio.run(service._ensure_shared_skill(Client()))

    assert result["content"] == "published body"
    assert result["revision"] == "published-revision"


def test_missing_shared_skill_is_published_once_from_local_fallback() -> None:
    service = _service()
    calls = []

    class Client:
        async def get_skill(self, *, skill_name):
            calls.append(("get", skill_name))
            return {"ok": False, "exit_code": 404}

        async def publish_shared_skill(self, **kwargs):
            calls.append(("publish", kwargs))
            return {
                "ok": True,
                "result": {
                    "content": kwargs["skill_body"],
                    "revision": "initial-revision",
                },
            }

    result = asyncio.run(service._ensure_shared_skill(Client()))

    assert result["revision"] == "initial-revision"
    assert [call[0] for call in calls] == ["get", "publish"]


def test_user_staging_copies_deterministically_without_running_skill(
    tmp_path,
    monkeypatch,
) -> None:
    service = _service()
    calls = []
    fingerprint = "sha256:" + "a" * 64

    class Client:
        def __init__(self, **kwargs):
            calls.append(("init", kwargs))

        async def inspect(self, kinds):
            calls.append(("inspect", kinds))
            return StagingInventory(
                user_id="alice",
                source_root="viking://user/alice/memories",
                kinds=("events",),
                files=(
                    StagingSource(
                        uri="viking://user/alice/memories/events/launch.md",
                        relative_path="events/launch.md",
                        kind="events",
                        size=12,
                        modified_at="2026-08-27T00:00:00Z",
                    ),
                ),
                fingerprint=fingerprint,
            )

        async def publish(self, inventory, *, staging_uri, run_id):
            calls.append(("publish", inventory, staging_uri, run_id))
            return StagingSnapshot(
                uri=staging_uri,
                source_count=1,
                total_bytes=12,
                chunk_count=1,
            )

    monkeypatch.setattr(
        "teamEvolver.aggregation.service.DeterministicStagingClient",
        Client,
    )
    run = AggregationRun(
        task_id="agg_test",
        account_id="default",
        endpoint="https://openviking.example",
        auth_mode="api_key",
        target_uri="viking://resources/team-memory",
        skill_uri="viking://agent/skills/team-memory-okf",
        skill_revision="shared-revision",
    )
    run.work_root = service._work_root(run.target_uri, "admin")
    state = AggregationState(
        path=tmp_path / "state.json",
        account_id="default",
        skill_fingerprint="old-skill-fingerprint",
    )

    result = asyncio.run(
        service._stage_one_user(
            run=run,
            user_id="alice",
            kinds=["events"],
            endpoint=run.endpoint,
            api_key="alice-secret",
            target_user_id="admin",
            target_api_key="admin-secret",
            agent_id="team-skill-evolver",
            state=state,
            force_all=True,
        )
    )

    assert result == f"{run.work_root}/users/alice/snapshots/{'a' * 64}"
    init = calls[0][1]
    assert init["source_user_id"] == "alice"
    assert init["source_api_key"] == "alice-secret"
    assert init["target_user_id"] == "admin"
    assert init["target_api_key"] == "admin-secret"
    assert calls[1] == ("inspect", ["events"])
    assert calls[2][0] == "publish"
    assert state.groups["stage:alice"]["staging_uri"] == result
