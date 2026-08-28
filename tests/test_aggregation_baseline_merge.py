from __future__ import annotations

import asyncio
from types import SimpleNamespace

from teamEvolver.aggregation.service import MemoryAggregationService
from teamEvolver.aggregation.state import AggregationState


def _service(tmp_path, *, preserve: bool = True) -> MemoryAggregationService:
    return MemoryAggregationService(
        SimpleNamespace(
            aggregation_merge_fan_in=4,
            aggregation_merge_concurrency=1,
            aggregation_phase1_concurrency=1,
            aggregation_compile_runtime_timeout_seconds=3000,
            aggregation_staging_dir="staging",
            aggregation_state_dir=str(tmp_path),
            aggregation_preserve_manual_edits=preserve,
            sharing_viking_account="default",
            sharing_viking_endpoint="http://openviking.example",
        )
    )


def _staged(service, target_uri, work_root, count):
    fps = [f"sha256:{index:064x}" for index in range(count)]
    roots = [
        service._staging_uri(
            f"user-{index}",
            target_uri,
            source_fingerprint=fps[index],
            work_root=work_root,
        )
        for index in range(count)
    ]
    return fps, roots


def test_final_compile_includes_baseline_source_when_enabled(tmp_path) -> None:
    service = _service(tmp_path)
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    _fps, staged_roots = _staged(service, target_uri, work_root, 2)
    baseline_uri = f"{work_root}/_baseline/deadbeef"

    calls: list[list[str]] = []

    class Client:
        async def run_batch(self, **kwargs):
            calls.append(list(kwargs["source_uris"]))
            return {"ok": True}

    async def scenario() -> None:
        run = service.new_run(
            "default",
            endpoint="http://openviking.example",
            target_uri=target_uri,
        )
        run.work_root = work_root
        state = AggregationState(
            path=service._state_path("default", target_uri, run.endpoint),
            account_id="default",
            skill_fingerprint="skill-v1",
        )
        completed = await service._tree_reduce_merge(
            run=run,
            staged_roots=staged_roots,
            client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="rev-1",
            state=state,
            baseline_uri=baseline_uri,
            baseline_fingerprint="sha256:" + "a" * 64,
        )
        assert completed is True

    asyncio.run(scenario())

    # One final compile that writes the target, and it must include the baseline
    # as the first source alongside the two staged roots.
    assert len(calls) == 1
    assert calls[0][0] == baseline_uri
    assert set(calls[0]) == {baseline_uri, *staged_roots}


def test_baseline_change_triggers_final_recompile(tmp_path) -> None:
    service = _service(tmp_path)
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    fps, staged_roots = _staged(service, target_uri, work_root, 2)
    baseline_uri = f"{work_root}/_baseline/deadbeef"
    state_path = service._state_path("default", target_uri, "http://openviking.example")

    # Seed stable per-user staging fingerprints so the only variable across runs
    # is the baseline fingerprint.
    seed = AggregationState(path=state_path, account_id="default", skill_fingerprint="skill-v1")
    for index in range(2):
        seed.mark_stage_ok(
            f"stage:user-{index}",
            fps[index],
            staging_uri=staged_roots[index],
            source_count=1,
            total_bytes=1,
        )
    seed.save(skill_fingerprint="skill-v1")

    calls: list[list[str]] = []

    class Client:
        async def run_batch(self, **kwargs):
            calls.append(list(kwargs["source_uris"]))
            return {"ok": True}

    async def run_once(baseline_fingerprint: str) -> None:
        run = service.new_run(
            "default",
            endpoint="http://openviking.example",
            target_uri=target_uri,
        )
        run.work_root = work_root
        state = AggregationState.load(state_path, "default")
        completed = await service._tree_reduce_merge(
            run=run,
            staged_roots=staged_roots,
            client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="rev-1",
            state=state,
            baseline_uri=baseline_uri,
            baseline_fingerprint=baseline_fingerprint,
        )
        assert completed is True

    # First run compiles; second run with the SAME baseline reuses (skipped);
    # third run with a CHANGED baseline fingerprint must recompile.
    asyncio.run(run_once("sha256:" + "a" * 64))
    asyncio.run(run_once("sha256:" + "a" * 64))
    asyncio.run(run_once("sha256:" + "b" * 64))

    assert len(calls) == 2  # run 1 and run 3 compiled; run 2 was reused


def test_no_baseline_source_when_disabled(tmp_path) -> None:
    service = _service(tmp_path, preserve=False)
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    _fps, staged_roots = _staged(service, target_uri, work_root, 2)

    calls: list[list[str]] = []

    class Client:
        async def run_batch(self, **kwargs):
            calls.append(list(kwargs["source_uris"]))
            return {"ok": True}

    async def scenario() -> None:
        run = service.new_run(
            "default",
            endpoint="http://openviking.example",
            target_uri=target_uri,
        )
        run.work_root = work_root
        state = AggregationState(
            path=service._state_path("default", target_uri, run.endpoint),
            account_id="default",
            skill_fingerprint="skill-v1",
        )
        # Simulate the run wiring: disabled means no baseline is passed.
        completed = await service._tree_reduce_merge(
            run=run,
            staged_roots=staged_roots,
            client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="rev-1",
            state=state,
        )
        assert completed is True

    asyncio.run(scenario())

    assert service._preserve_manual_edits() is False
    assert len(calls) == 1
    assert set(calls[0]) == set(staged_roots)
