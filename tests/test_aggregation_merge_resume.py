from __future__ import annotations

import asyncio
from types import SimpleNamespace

from teamEvolver.aggregation.service import MemoryAggregationService
from teamEvolver.aggregation.state import AggregationState


def test_group_checkpoint_survives_without_full_state_compaction(tmp_path) -> None:
    state_path = tmp_path / "aggregation-state.json"
    state = AggregationState(
        path=state_path,
        account_id="default",
        skill_fingerprint="skill-v1",
    )
    state.mark_ok("stage:user-1", "source-fingerprint-1")

    state.checkpoint("stage:user-1", skill_fingerprint="skill-v1")

    recovered = AggregationState.load(state_path, "default")
    assert recovered.skill_fingerprint == "skill-v1"
    assert recovered.groups["stage:user-1"] == {
        "status": "ok",
        "source_fingerprint": "source-fingerprint-1",
    }


def test_skill_change_invalidates_merge_but_not_deterministic_staging(tmp_path) -> None:
    state = AggregationState(
        path=tmp_path / "aggregation-state.json",
        account_id="default",
        skill_fingerprint="skill-v1",
    )
    fingerprint = "sha256:" + "a" * 64
    staging_uri = "viking://user/admin/resources/staging/alice/snapshot"
    state.mark_stage_ok(
        "stage:alice",
        fingerprint,
        staging_uri=staging_uri,
        source_count=2,
        total_bytes=100,
    )

    assert state.needs_restage(
        "stage:alice",
        fingerprint,
        staging_uri=staging_uri,
    ) is False
    assert state.needs_recompile(
        "merge",
        "merge-input",
        current_skill_fingerprint="skill-v2",
    ) is True


def test_merge_resume_reuses_successful_groups_and_retries_failed_groups(
    tmp_path,
) -> None:
    service = MemoryAggregationService(
        SimpleNamespace(
            aggregation_merge_fan_in=4,
            aggregation_phase1_concurrency=2,
            aggregation_compile_runtime_timeout_seconds=3000,
            aggregation_staging_dir="staging",
            aggregation_state_dir=str(tmp_path),
            sharing_viking_account="default",
            sharing_viking_endpoint="http://openviking.example",
        )
    )
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    source_fingerprints = [
        f"sha256:{index:064x}"
        for index in range(8)
    ]
    staged_roots = [
        service._staging_uri(
            f"user-{index}",
            target_uri,
            source_fingerprint=source_fingerprints[index],
            work_root=work_root,
        )
        for index in range(8)
    ]
    state_path = service._state_path(
        "default",
        target_uri,
        "http://openviking.example",
    )
    first_state = AggregationState(
        path=state_path,
        account_id="default",
        skill_fingerprint="skill-v1",
    )
    for index in range(8):
        first_state.mark_stage_ok(
            f"stage:user-{index}",
            source_fingerprints[index],
            staging_uri=staged_roots[index],
            source_count=1,
            total_bytes=1,
        )
    first_state.save(skill_fingerprint="skill-v1")
    calls: list[tuple[int, str]] = []
    attempt = 1

    class Client:
        async def run_batch(self, **kwargs):
            target = kwargs["target_uri"]
            calls.append((attempt, target))
            if attempt == 1 and target.endswith("/_merge/L0/g1"):
                return {"ok": False, "stderr": "simulated timeout"}
            return {"ok": True}

    async def run_once():
        run = service.new_run(
            "default",
            endpoint="http://openviking.example",
            target_uri=target_uri,
        )
        await service._tree_reduce_merge(
            run=run,
            staged_roots=staged_roots,
            client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="skill-revision-v1",
        )
        return run

    first_run = asyncio.run(run_once())

    first_targets = [target for run_attempt, target in calls if run_attempt == 1]
    assert first_targets == [
        f"{work_root}/_merge/L0/g0",
        f"{work_root}/_merge/L0/g1",
    ]
    assert not any(
        group.group_key == "merge" and group.status == "ok"
        for group in first_run.groups
    )

    persisted = AggregationState.load(state_path, "default")
    assert persisted.groups["merge:L0:g0"]["status"] == "ok"
    assert persisted.groups["merge:L0:g1"]["status"] == "failed"

    attempt = 2
    second_run = asyncio.run(run_once())

    second_targets = [target for run_attempt, target in calls if run_attempt == 2]
    assert second_targets == [
        f"{work_root}/_merge/L0/g1",
        target_uri,
    ]
    assert any(
        group.group_key == "merge:L0:g0"
        and group.status == "skipped"
        and "reused" in group.detail
        for group in second_run.groups
    )
    assert any(
        group.group_key == "merge" and group.status == "ok"
        for group in second_run.groups
    )

    changed_state = AggregationState.load(state_path, "default")
    updated_fingerprint = "sha256:" + "f" * 64
    staged_roots[5] = service._staging_uri(
        "user-5",
        target_uri,
        source_fingerprint=updated_fingerprint,
        work_root=work_root,
    )
    changed_state.mark_stage_ok(
        "stage:user-5",
        updated_fingerprint,
        staging_uri=staged_roots[5],
        source_count=1,
        total_bytes=1,
    )
    changed_state.save(skill_fingerprint="skill-v1")

    attempt = 3
    third_run = asyncio.run(run_once())

    third_targets = [target for run_attempt, target in calls if run_attempt == 3]
    assert third_targets == [
        f"{work_root}/_merge/L0/g1",
        target_uri,
    ]
    assert any(
        group.group_key == "merge:L0:g0"
        and group.status == "skipped"
        and "reused" in group.detail
        for group in third_run.groups
    )


def test_merge_groups_run_with_bounded_parallelism(tmp_path) -> None:
    service = MemoryAggregationService(
        SimpleNamespace(
            aggregation_merge_fan_in=4,
            aggregation_merge_concurrency=3,
            aggregation_phase1_concurrency=6,
            aggregation_compile_runtime_timeout_seconds=3000,
            aggregation_staging_dir="staging",
            aggregation_state_dir=str(tmp_path),
            sharing_viking_account="default",
            sharing_viking_endpoint="http://openviking.example",
        )
    )
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    source_fingerprints = [
        f"sha256:{index:064x}"
        for index in range(16)
    ]
    staged_roots = [
        service._staging_uri(
            f"user-{index}",
            target_uri,
            source_fingerprint=source_fingerprints[index],
            work_root=work_root,
        )
        for index in range(16)
    ]
    state_path = service._state_path(
        "default",
        target_uri,
        "http://openviking.example",
    )
    state = AggregationState(
        path=state_path,
        account_id="default",
        skill_fingerprint="skill-v1",
    )
    for index in range(16):
        state.mark_stage_ok(
            f"stage:user-{index}",
            source_fingerprints[index],
            staging_uri=staged_roots[index],
            source_count=1,
            total_bytes=1,
        )
    state.save(skill_fingerprint="skill-v1")
    active = 0
    max_active = 0

    class Client:
        async def run_batch(self, **_kwargs):
            nonlocal active, max_active
            active += 1
            max_active = max(max_active, active)
            try:
                await asyncio.sleep(0.01)
                return {"ok": True}
            finally:
                active -= 1

    async def scenario() -> None:
        run = service.new_run(
            "default",
            endpoint="http://openviking.example",
            target_uri=target_uri,
        )
        completed = await service._tree_reduce_merge(
            run=run,
            staged_roots=staged_roots,
            client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="skill-revision-v1",
        )
        assert completed is True

    asyncio.run(scenario())

    assert max_active == 3


def test_ten_thousand_users_are_assigned_to_stable_publish_partitions(
    tmp_path,
) -> None:
    service = MemoryAggregationService(
        SimpleNamespace(
            aggregation_partition_count=256,
            aggregation_phase1_concurrency=6,
            aggregation_staging_dir="staging",
        )
    )
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    roots = [
        service._staging_uri(
            f"user-{index:05d}",
            target_uri,
            source_fingerprint=f"sha256:{index:064x}",
            work_root=work_root,
        )
        for index in range(10_000)
    ]
    new_root = service._staging_uri(
        "user-new",
        target_uri,
        source_fingerprint="sha256:" + "f" * 64,
        work_root=work_root,
    )

    partitions = service._partition_staged_roots(roots)
    extended = service._partition_staged_roots(
        [*roots, new_root]
    )

    assert len(partitions) == 256
    assert sum(len(items) for items in partitions.values()) == 10_000
    estimated_tasks = sum(
        service._tree_compile_task_count(len(items))
        for items in partitions.values()
    )
    assert 3_000 < estimated_tasks < 4_000
    original_assignment = {
        uri: partition
        for partition, items in partitions.items()
        for uri in items
    }
    extended_assignment = {
        uri: partition
        for partition, items in extended.items()
        for uri in items
    }
    assert all(
        extended_assignment[uri] == partition
        for uri, partition in original_assignment.items()
    )
    updated_root = service._staging_uri(
        "user-00001",
        target_uri,
        source_fingerprint="sha256:" + "e" * 64,
        work_root=work_root,
    )
    updated_partition = next(
        partition
        for partition, items in service._partition_staged_roots(
            [updated_root]
        ).items()
        if updated_root in items
    )
    assert updated_partition == original_assignment[roots[1]]


def test_large_account_publishes_partitions_instead_of_one_lossy_root(
    tmp_path,
) -> None:
    service = MemoryAggregationService(
        SimpleNamespace(
            aggregation_merge_fan_in=4,
            aggregation_merge_concurrency=2,
            aggregation_partition_threshold=4,
            aggregation_partition_count=16,
            aggregation_phase1_concurrency=4,
            aggregation_compile_runtime_timeout_seconds=3000,
            aggregation_staging_dir="staging",
            aggregation_state_dir=str(tmp_path),
            sharing_viking_account="default",
            sharing_viking_endpoint="http://openviking.example",
        )
    )
    target_uri = "viking://resources/team-memory"
    work_root = service._work_root(target_uri)
    source_fingerprints = [
        f"sha256:{index:064x}"
        for index in range(20)
    ]
    staged_roots = [
        service._staging_uri(
            f"user-{index:02d}",
            target_uri,
            source_fingerprint=source_fingerprints[index],
            work_root=work_root,
        )
        for index in range(20)
    ]
    state_path = service._state_path(
        "default",
        target_uri,
        "http://openviking.example",
    )
    state = AggregationState(
        path=state_path,
        account_id="default",
        skill_fingerprint="skill-v1",
    )
    for index in range(20):
        state.mark_stage_ok(
            f"stage:user-{index:02d}",
            source_fingerprints[index],
            staging_uri=staged_roots[index],
            source_count=1,
            total_bytes=1,
        )
    state.save(skill_fingerprint="skill-v1")
    compile_targets: list[str] = []
    manifests: list[tuple[str, str]] = []

    class Client:
        async def run_batch(self, **kwargs):
            compile_targets.append(kwargs["target_uri"])
            return {"ok": True}

        async def upsert_text(self, *, root_uri, uri, content):
            assert root_uri == target_uri
            manifests.append((uri, content))
            return {"ok": True}

        async def delete_uri(self, *, uri):
            raise AssertionError(f"unexpected stale partition: {uri}")

    async def scenario() -> None:
        run = service.new_run(
            "default",
            endpoint="http://openviking.example",
            target_uri=target_uri,
        )
        completed = await service._merge_staged_roots(
            run=run,
            staged_roots=staged_roots,
            client=Client(),
            skill_uri="viking://agent/skills/team-memory-okf",
            skill_revision="skill-revision-v1",
            state=AggregationState.load(state_path, "default"),
        )
        assert completed is True
        assert any(
            group.group_key == "merge"
            and group.status == "ok"
            and "partitions" in group.detail
            for group in run.groups
        )

    asyncio.run(scenario())

    assert compile_targets
    assert target_uri not in compile_targets
    assert all(
        target.startswith(f"{target_uri}/partitions/")
        or target.startswith(f"{work_root}/_merge/partitions/")
        for target in compile_targets
    )
    assert manifests[0][0] == f"{target_uri}/index.md"
