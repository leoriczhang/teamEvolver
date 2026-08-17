from __future__ import annotations

import json

import pytest

from teamEvolver.integrations.skill_sync_adapters import _ack_matches
from teamEvolver.skills.hub import SkillHub
from teamEvolver.skills.mutations import (
    SkillMutationCommand,
    SkillMutationService,
)
from teamEvolver.storage.memory import InMemoryObjectStore


def _service(tmp_path, deliverer):
    bucket = InMemoryObjectStore("skill-mutations")
    hub = SkillHub.from_bucket(bucket, user_alias="test")
    root = tmp_path / "skills"
    skill = root / "demo"
    skill.mkdir(parents=True)
    skill.joinpath("SKILL.md").write_text(
        "---\nname: demo\ndescription: demo\n---\n\nDo work.\n",
        encoding="utf-8",
    )
    return (
        SkillMutationService.from_hub(
            hub,
            config=object(),
            deliverer=deliverer,
        ),
        bucket,
        root,
    )


@pytest.mark.anyio
async def test_publish_commit_outbox_is_idempotent_and_restart_safe(
    tmp_path,
) -> None:
    delivered: list[str] = []

    async def deliver(_config, event):
        delivered.append(event["event_id"])
        return {
            "status": "synced",
            "results": {"agent-1": {"status": "synced"}},
        }

    service, bucket, root = _service(tmp_path, deliver)
    command = SkillMutationCommand(
        action="publish",
        name="demo",
        mutation_id="job-1",
        skills_dir=str(root),
        tenant_ids=("tenant-1",),
    )

    first = service.execute(command)
    second = service.execute(command)
    restarted = SkillMutationService.from_hub(
        service._hub,
        config=object(),
        deliverer=deliver,
    )
    summary = await restarted.drain()

    assert first == second
    assert first["event_id"].startswith("skill_evt_")
    assert summary == {"synced": 1, "failed": 0, "pending": 0}
    assert delivered == [first["event_id"]]
    event = json.loads(
        bucket.get_object(
            f"skill_sync_outbox/{first['event_id']}.json"
        ).read()
    )
    assert event["status"] == "synced"
    assert event["deliveries"]["agent-1"]["status"] == "synced"


def test_reconcile_repairs_missing_outbox_and_delete_writes_tombstone(
    tmp_path,
) -> None:
    async def deliver(_config, _event):
        return {"status": "synced", "results": {}}

    service, bucket, root = _service(tmp_path, deliver)
    published = service.execute(
        SkillMutationCommand(
            action="publish",
            name="demo",
            mutation_id="job-publish",
            skills_dir=str(root),
        )
    )
    bucket.delete_object(
        f"skill_sync_outbox/{published['event_id']}.json"
    )

    assert service.reconcile() == 1
    assert bucket.get_object(
        f"skill_sync_outbox/{published['event_id']}.json"
    ).read()

    deleted = service.execute(
        SkillMutationCommand(
            action="delete",
            name="demo",
            mutation_id="job-delete",
        )
    )
    tombstones = list(bucket.iter_objects("skill_tombstones/demo/"))
    assert len(tombstones) == 1
    tombstone = json.loads(bucket.get_object(tombstones[0].key).read())
    assert tombstone["deleted"] is True
    assert deleted["expected"]["deleted"] is True


@pytest.mark.anyio
async def test_failed_delivery_is_persisted_for_retry(tmp_path) -> None:
    async def fail(_config, _event):
        raise RuntimeError("offline")

    service, bucket, root = _service(tmp_path, fail)
    commit = service.execute(
        SkillMutationCommand(
            action="publish",
            name="demo",
            mutation_id="job-retry",
            skills_dir=str(root),
        )
    )

    summary = await service.drain()

    assert summary["failed"] == 1
    event = json.loads(
        bucket.get_object(
            f"skill_sync_outbox/{commit['event_id']}.json"
        ).read()
    )
    assert event["status"] == "pending"
    assert event["attempt"] == 1
    assert "offline" in event["last_error"]


@pytest.mark.anyio
async def test_delivery_retry_state_is_independent_per_integration(
    tmp_path,
) -> None:
    calls = 0

    async def deliver(_config, event):
        nonlocal calls
        calls += 1
        if calls == 1:
            return {
                "status": "failed",
                "results": {
                    "agent-a": {
                        "status": "failed",
                        "detail": "offline",
                        "attempted": True,
                    },
                    "agent-b": {
                        "status": "synced",
                        "ack": {"ok": True},
                        "attempted": True,
                    },
                },
            }
        return {
            "status": "synced",
            "results": {
                "agent-a": {
                    "status": "synced",
                    "ack": {"ok": True},
                    "attempted": True,
                }
            },
        }

    service, bucket, root = _service(tmp_path, deliver)
    commit = service.execute(
        SkillMutationCommand(
            action="publish",
            name="demo",
            mutation_id="job-independent-retry",
            skills_dir=str(root),
        )
    )

    first = await service.drain()
    event_key = f"skill_sync_outbox/{commit['event_id']}.json"
    event = json.loads(bucket.get_object(event_key).read())
    assert first["failed"] == 1
    assert event["deliveries"]["agent-a"]["attempt"] == 1
    assert event["deliveries"]["agent-a"]["status"] == "pending"
    assert event["deliveries"]["agent-b"]["status"] == "synced"

    event["next_retry_at"] = "2000-01-01T00:00:00+00:00"
    event["deliveries"]["agent-a"]["next_retry_at"] = (
        "2000-01-01T00:00:00+00:00"
    )
    bucket.put_object(
        event_key,
        json.dumps(event).encode("utf-8"),
    )
    second = await service.drain()
    event = json.loads(bucket.get_object(event_key).read())

    assert second["synced"] == 1
    assert event["status"] == "synced"
    assert event["deliveries"]["agent-a"]["status"] == "synced"
    assert event["deliveries"]["agent-b"]["status"] == "synced"


def test_manifest_reconciliation_backfills_missing_commit(tmp_path) -> None:
    async def deliver(_config, _event):
        return {"status": "no_capable_agents", "results": {}}

    service, bucket, root = _service(tmp_path, deliver)
    service._hub.push_skills(str(root))

    assert service.reconcile() == 1
    commits = list(bucket.iter_objects("skill_mutation_commits/"))
    events = list(bucket.iter_objects("skill_sync_outbox/"))
    assert len(commits) == 1
    assert len(events) == 1


def test_skill_sync_ack_requires_exact_hashes_and_delete_confirmation() -> None:
    expected = {
        "name": "demo",
        "version": 3,
        "sha256": "a" * 64,
        "tree_sha256": "b" * 64,
    }
    published = {
        "ok": True,
        "results": {
            "tenant-1": {
                "verification": {
                    "skills": [
                        {
                            "name": "demo",
                            "matched": True,
                            "actual_version": 3,
                            "actual_sha256": "a" * 64,
                            "actual_tree_sha256": "b" * 64,
                        }
                    ]
                }
            }
        },
    }
    deleted = {
        "ok": True,
        "results": {
            "tenant-1": {
                "verification": {
                    "skills": [
                        {
                            "name": "demo",
                            "matched": True,
                            "removed": True,
                        }
                    ]
                }
            }
        },
    }

    assert _ack_matches(
        published,
        action="update",
        skills=[expected],
        tenant_ids=["tenant-1"],
    ) == (True, "")
    assert _ack_matches(
        deleted,
        action="delete",
        skills=[{**expected, "deleted": True}],
        tenant_ids=["tenant-1"],
    ) == (True, "")
    published["results"]["tenant-1"]["verification"]["skills"][0][
        "actual_sha256"
    ] = "c" * 64
    matched, reason = _ack_matches(
        published,
        action="update",
        skills=[expected],
        tenant_ids=["tenant-1"],
    )
    assert matched is False
    assert "sha256 mismatch" in reason
