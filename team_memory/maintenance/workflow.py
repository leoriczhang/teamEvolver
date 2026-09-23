"""Snapshot and maintenance stages shared by manual and scheduled runs."""

from __future__ import annotations

import hashlib
from typing import TYPE_CHECKING

from team_memory.aggregation.sources import SourceExpansionError
from team_memory.aggregation.staging import DeterministicStagingClient

if TYPE_CHECKING:
    from team_memory.compile_client import CompileClient
    from team_memory.service import AggregationRun, MemoryAggregationService
    from team_memory.aggregation.state import AggregationState


async def ensure_private_parents(client: CompileClient, uri: str) -> None:
    parts = uri.removeprefix("viking://").strip("/").split("/")
    if len(parts) < 4 or parts[0] != "user":
        raise ValueError("Snapshot destination must be in a private user namespace")
    for end in range(3, len(parts)):
        await client.mkdir("viking://" + "/".join(parts[:end]))


async def freeze_skill(
    service: MemoryAggregationService, run: AggregationRun, client: CompileClient, stage: str
) -> tuple[str, str]:
    """Copy the entire Skill bundle, not just its Markdown definition."""
    await service._ensure_shared_skill(client, stage)
    source = service._shared_skill_uri(stage)
    inspector = _inspector(run, client)
    before = await inspector.inspect((), source_root=source, include_all_kinds=True)
    private_uri = f"viking://user/{run.merge_user_id}/skills/tm-{stage}-{run.task_id}"
    await ensure_private_parents(client, private_uri)
    copied = await client.copy_tree(source_uri=source, target_uri=private_uri)
    if not copied.get("ok"):
        raise SourceExpansionError(f"Cannot freeze {stage} Skill: {copied.get('stderr')}")
    after = await inspector.inspect((), source_root=source, include_all_kinds=True)
    if before.fingerprint != after.fingerprint:
        raise SourceExpansionError("Skill changed during snapshot; retry the run")
    if not any(item.relative_path == "SKILL.md" for item in before.files):
        raise SourceExpansionError("Skill snapshot is missing SKILL.md")
    digest = hashlib.sha256()
    for item in before.files:
        payload = await client.download_bytes(f"{private_uri}/{item.relative_path}")
        digest.update(item.relative_path.encode())
        digest.update(b"\0")
        digest.update(hashlib.sha256(payload).digest())
    return private_uri, "sha256:" + digest.hexdigest()


def _inspector(run: AggregationRun, client: CompileClient) -> DeterministicStagingClient:
    return DeterministicStagingClient(
        endpoint=run.endpoint,
        account_id=run.account_id,
        source_user_id=run.merge_user_id,
        source_api_key=client.api_key,
        target_user_id=run.merge_user_id,
        target_api_key=client.api_key,
        agent_id=client.agent_id,
        timeout_seconds=client.timeout_seconds,
    )


async def maintain(
    service: MemoryAggregationService,
    run: AggregationRun,
    client: CompileClient,
    state: AggregationState,
    *,
    full: bool,
) -> None:
    from team_memory.service import GroupResult

    inspector = _inspector(run, client)
    inventory = await inspector.inspect((), source_root=run.target_uri, include_all_kinds=True)
    if not inventory.files:
        service._append_group(
            run, GroupResult("maintenance", "(all)", run.target_uri, 0, "skipped", "team Memory is empty")
        )
        return
    prior = state.metadata.get("maintenance", {})
    if (
        not full
        and prior.get("output_fingerprint") == inventory.fingerprint
        and prior.get("skill_fingerprint") == run.maintenance_skill_revision
    ):
        service._append_group(
            run,
            GroupResult("maintenance", "(all)", run.target_uri, 1, "skipped", "Memory and maintenance Skill unchanged"),
        )
        return

    run.stage = "snapshot"
    run.snapshot_uri = f"{run.work_root}/maintenance/{run.task_id}"
    service._save_runs()
    await ensure_private_parents(client, run.snapshot_uri)
    copied = await client.copy_tree(source_uri=run.target_uri, target_uri=run.snapshot_uri)
    if not copied.get("ok"):
        service._append_group(
            run,
            GroupResult("snapshot", "(all)", run.snapshot_uri, 1, "failed", str(copied.get("stderr") or "copy failed")),
        )
        raise SourceExpansionError("Memory snapshot failed; maintenance was not started")
    service._append_group(run, GroupResult("snapshot", "(all)", run.snapshot_uri, 1, "ok", "ov cp"))
    run.stage = "maintenance"
    service._save_runs()
    result = await service._run_compile(
        client,
        source_uris=[run.snapshot_uri],
        target_uri=run.target_uri,
        skill_uri=run.maintenance_skill_uri,
        reason=(
            "Maintain the existing Team Memory using the supplied pre-maintenance copy. "
            "Preserve original provenance and human corrections. The source copy is an "
            "execution artifact, not a new factual source. Use soft archive markers and "
            "superseded-page pointers; local deletion does not remove remote files."
        ),
    )
    service._append_group(
        run,
        GroupResult(
            "maintenance",
            "(all)",
            run.target_uri,
            1,
            "ok" if result.get("ok") else "failed",
            str(result.get("stderr") or ""),
        ),
    )
    if not result.get("ok"):
        raise SourceExpansionError("DreamCycle compile failed; aggregation and snapshot are retained")
    after = await inspector.inspect((), source_root=run.target_uri, include_all_kinds=True)
    state.metadata["maintenance"] = {
        "output_fingerprint": after.fingerprint,
        "skill_fingerprint": run.maintenance_skill_revision,
        "snapshot_uri": run.snapshot_uri,
    }
    state.save(skill_fingerprint=state.skill_fingerprint)
