"""Skill planning and held-out Cases share evidence and join before persistence."""
from __future__ import annotations

import asyncio
from contextlib import asynccontextmanager
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from team_skills.config import SkillsModuleConfig
from team_skills.evolution import cycle
from team_skills.evolution.prompt_studio import pipeline_graph


@pytest.fixture
def branch(monkeypatch):
    @asynccontextmanager
    async def tenant_tx(tenant_id):
        yield None

    sessions = [
        {"session_id": "s1", "content_hash": "h1", "user_alias": "alice"},
        {"session_id": "s2", "content_hash": "h2", "user_alias": "bob"},
    ]
    repo = SimpleNamespace(
        branch=AsyncMock(return_value=None),
        evidence=AsyncMock(return_value=[]),
        record_evidence=AsyncMock(),
        save_branch=AsyncMock(),
        consume=AsyncMock(),
        finish=AsyncMock(),
    )
    library = SimpleNamespace(
        list_skills=AsyncMock(return_value=[]),
        get_skill=AsyncMock(return_value=None),
    )
    candidates = SimpleNamespace(
        live_for_skill=AsyncMock(return_value=None),
        create=AsyncMock(return_value={"candidate_id": "candidate", "revision": 1}),
    )
    monkeypatch.setattr(cycle, "EvolutionRepository", lambda tx: repo)
    monkeypatch.setattr(cycle, "SkillLibraryRepository", lambda tx: library)
    monkeypatch.setattr(cycle, "CandidateRepository", lambda tx: candidates)
    monkeypatch.setattr(
        cycle, "ExperienceRepository", lambda tx: SimpleNamespace(patterns=AsyncMock(return_value=[])),
    )
    monkeypatch.setattr(cycle, "_inputs", AsyncMock(return_value=sessions))
    monkeypatch.setattr(cycle, "load_sessions", AsyncMock(return_value=sessions))
    monkeypatch.setattr(cycle, "groups", lambda value: {cycle.NO_SKILL_KEY: value})
    services = SimpleNamespace(
        tx=SimpleNamespace(tenant_tx=tenant_tx),
        tasks=SimpleNamespace(require_current=AsyncMock()),
    )
    guard = SimpleNamespace(heartbeat=AsyncMock(return_value=True))
    task = SimpleNamespace(tenant_id="default", task_id="evolve")

    async def run(planner, synthesizer):
        return await cycle._cycle(
            task, services, {}, SkillsModuleConfig(1, {"auto_evaluate": False}),
            planner, synthesizer, None, guard, None,
        )

    return SimpleNamespace(run=run, candidates=candidates, repo=repo, guard=guard, sessions=sessions)


def proposal():
    return {
        "action": "create_skill",
        "skill": {"name": "new-skill", "description": "Use for shared tasks", "content": "Reusable steps"},
        "evidence_classification": {"team_skill": [{"claim": "candidate-only claim"}]},
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("first", ["planner", "dataset"])
async def test_both_start_before_either_finishes_and_join_before_candidate_write(branch, first):
    started = {key: asyncio.Event() for key in ("planner", "dataset")}
    release = {key: asyncio.Event() for key in started}
    finished = {key: asyncio.Event() for key in started}
    seen = {}

    async def planner(**kwargs):
        started["planner"].set()
        await release["planner"].wait()
        finished["planner"].set()
        return proposal()

    async def synthesizer(**kwargs):
        seen.update(kwargs)
        started["dataset"].set()
        await release["dataset"].wait()
        finished["dataset"].set()
        return [{"case_id": "held-out", "skill_ids": [kwargs["skill_name"]]}]

    running = asyncio.create_task(branch.run(planner, synthesizer))
    try:
        await asyncio.wait_for(
            asyncio.gather(*(event.wait() for event in started.values())), timeout=0.5,
        )
        assert seen["sessions"] == branch.sessions
        assert not seen.get("candidate_skill")
        assert "candidate-only claim" not in str(seen)
        release[first].set()
        await asyncio.wait_for(finished[first].wait(), timeout=0.5)
        branch.candidates.create.assert_not_awaited()
        release["dataset" if first == "planner" else "planner"].set()
        await asyncio.wait_for(running, timeout=0.5)
        detail = branch.candidates.create.call_args.kwargs["detail"]
        assert detail["replay_cases"][0]["skill_ids"] == ["new-skill"]
        branch.repo.consume.assert_awaited_once()
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)


@pytest.mark.asyncio
async def test_skip_cancels_unneeded_dataset_generation(branch):
    dataset_started, dataset_cancelled = asyncio.Event(), asyncio.Event()

    async def planner(**kwargs):
        await asyncio.wait_for(dataset_started.wait(), timeout=0.5)
        return {"action": "skip", "rationale": "No shared Skill change"}

    async def synthesizer(**kwargs):
        dataset_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            dataset_cancelled.set()

    await asyncio.wait_for(branch.run(planner, synthesizer), timeout=1)
    assert dataset_cancelled.is_set()
    branch.candidates.create.assert_not_awaited()
    branch.repo.consume.assert_awaited_once()


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["planner", "dataset"])
async def test_failure_leaves_no_candidate_or_consumed_evidence(branch, failure):
    dataset_started, dataset_stopped = asyncio.Event(), asyncio.Event()

    async def planner(**kwargs):
        await asyncio.wait_for(dataset_started.wait(), timeout=0.5)
        if failure == "planner":
            raise RuntimeError("planner failed")
        return proposal()

    async def synthesizer(**kwargs):
        dataset_started.set()
        try:
            if failure == "dataset":
                raise RuntimeError("dataset failed")
            await asyncio.Event().wait()
        finally:
            dataset_stopped.set()

    with pytest.raises(RuntimeError, match=f"{failure} failed"):
        await asyncio.wait_for(branch.run(planner, synthesizer), timeout=1)
    assert dataset_stopped.is_set()
    branch.candidates.create.assert_not_awaited()
    branch.repo.consume.assert_not_awaited()
    branch.repo.save_branch.assert_not_awaited()


@pytest.mark.asyncio
async def test_cancelled_worker_stops_both_model_branches(branch):
    started = [asyncio.Event(), asyncio.Event()]
    stopped = [asyncio.Event(), asyncio.Event()]

    async def blocked(index):
        started[index].set()
        try:
            await asyncio.Event().wait()
        finally:
            stopped[index].set()

    running = asyncio.create_task(branch.run(lambda **kw: blocked(0), lambda **kw: blocked(1)))
    try:
        await asyncio.wait_for(asyncio.gather(*(event.wait() for event in started)), timeout=0.5)
    finally:
        running.cancel()
        await asyncio.gather(running, return_exceptions=True)
    assert all(event.is_set() for event in stopped)
    branch.candidates.create.assert_not_awaited()


@pytest.mark.asyncio
async def test_dataset_failure_cancels_a_still_running_planner(branch):
    planner_started, planner_stopped = asyncio.Event(), asyncio.Event()

    async def planner(**kwargs):
        planner_started.set()
        try:
            await asyncio.Event().wait()
        finally:
            planner_stopped.set()

    async def synthesizer(**kwargs):
        await planner_started.wait()
        raise RuntimeError("dataset failed")

    with pytest.raises(RuntimeError, match="dataset failed"):
        await asyncio.wait_for(branch.run(planner, synthesizer), timeout=1)
    assert planner_stopped.is_set()
    branch.repo.consume.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("lease_lost", [False, True])
async def test_long_running_branches_renew_resource_lease_or_stop(branch, monkeypatch, lease_lost):
    monkeypatch.setattr(cycle, "RESOURCE_HEARTBEAT_SECONDS", 0.01)
    renewed = asyncio.Event()

    async def heartbeat(*args, **kwargs):
        renewed.set()
        return not lease_lost

    branch.guard.heartbeat.side_effect = heartbeat

    async def planner(**kwargs):
        await renewed.wait()
        return proposal()

    async def synthesizer(**kwargs):
        return []

    # _cycle also renews before loading a group; only release the planner
    # after the heartbeat issued while the model is running.
    calls = 0

    async def counted_heartbeat(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls > 1:
            return await heartbeat(*args, **kwargs)
        return True

    branch.guard.heartbeat.side_effect = counted_heartbeat
    if lease_lost:
        with pytest.raises(RuntimeError, match="resource lease lost"):
            await asyncio.wait_for(branch.run(planner, synthesizer), timeout=1)
        branch.candidates.create.assert_not_awaited()
    else:
        await asyncio.wait_for(branch.run(planner, synthesizer), timeout=1)
        branch.candidates.create.assert_awaited_once()
    assert calls > 1


def test_pipeline_forks_dataset_from_evidence_and_joins_at_validation():
    graph = pipeline_graph({})
    edges = {(edge["from"], edge["to"]) for edge in graph["edges"]}
    assert ("group", "dataset_synthesis") in edges
    assert not any(
        (stage, "dataset_synthesis") in edges for stage in ("evolve_skill", "create_skill", "merge")
    )
    assert {
        ("evolve_skill", "validate"), ("create_skill", "validate"),
        ("merge", "validate"), ("dataset_synthesis", "validate"),
    } <= edges
    nodes = {node["id"]: node for node in graph["nodes"]}
    assert nodes["merge"]["conditional"]
    assert nodes["validate"]["join"] == "all"
    assert not nodes["dataset_synthesis"].get("conditional")
