"""Staged attachment citations must bind to the current checkpoint's exact local file."""

import base64
import copy
from pathlib import Path

import httpx
import pytest

from team_miner.ov_round_orchestration import _digest
from team_miner.tests.test_ov_round_bridge import APPROVED, app_for, complete, create
from team_miner.tests.test_ov_round_bridge import rig as rig


async def stage_attachment(owner, job, *, content=b"# Approved regional terms\nThe scope is confirmed."):
    staged = await owner._stage_round_supplements(
        job,
        job["pending_checkpoint"]["id"],
        [{"name": "approval.md", "content_base64": base64.b64encode(content).decode()}],
    )
    return staged, staged["pending_attachments"][0]


def decision(job, uri):
    return {
        "checkpoint_id": job["pending_checkpoint"]["id"],
        "answers": {"scope": "The approved regional terms confirm the scope."},
        "unresolved": [],
        "references": {"scope": [uri]},
        "request_id": "attachment-review",
    }


@pytest.mark.asyncio
@pytest.mark.parametrize("action", ["continue", "finish"])
async def test_current_staged_attachment_is_read_locally_frozen_and_uploaded_next_round(rig, monkeypatch, action):
    waiting = await complete(rig, create(rig, rounds=2))
    if action == "finish":
        next_round = rig.repository.prepare_next_round(
            waiting["job_id"], waiting["pending_checkpoint"]["id"], {"scope": "Review the original policy."}, [], {}
        )
        waiting = await complete(rig, next_round)
        assert waiting["current_round"] == waiting["max_rounds"] == 2
    compile_count = len(rig.remote.compile_calls)
    staged, attachment = await stage_attachment(rig, waiting)
    uri = attachment["uri"]
    assert uri not in rig.remote.content
    content = Path(attachment["local_path"]).read_text()
    remote_reads = []
    original_read = rig._round_read_reference

    async def record_remote_read(account, reference_uri):
        remote_reads.append((account, reference_uri))
        return await original_read(account, reference_uri)

    monkeypatch.setattr(rig, "_round_read_reference", record_remote_read)
    payload = decision(staged, uri)
    payload["references"]["scope"].append(APPROVED)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(rig)), base_url="http://test") as client:
        response = await client.post(f"/api/mining/jobs/{staged['job_id']}/{action}", json=payload)
        assert response.status_code == 200, response.text
    second = rig.repository.get_job(staged["job_id"])
    assert remote_reads == [("default", APPROVED)]
    submission = second["supplements"][-1]
    snapshot = next(row for row in submission["reference_snapshots"] if row["uri"] == uri)
    assert snapshot["sha256"] == attachment["sha256"] == _digest(content)
    assert Path(snapshot["local_path"]).read_text() == content
    assert snapshot["local_path"] != attachment["local_path"]
    assert submission["attachments"] == staged["pending_attachments"]
    if action == "finish":
        assert second["current_round"] == 2 and second["status"] == "succeeded"
        assert uri not in rig.remote.content and len(rig.remote.compile_calls) == compile_count
        assert any("尚未经过下一轮 Compile" in warning for warning in second["artifact_quality"]["warnings"])
        return
    assert second["current_round"] == 2 and second["status"] == "preparing"
    await rig._submit_ov_skill_round(second)
    assert rig.remote.content[uri] == content
    frozen_references = [
        value for path, value in rig.remote.content.items()
        if path.startswith(second["source_snapshot"] + "/supplements/round-1/references/")
    ]
    assert any(f"Original URI: {uri}" in value and content in value for value in frozen_references)


@pytest.mark.asyncio
async def test_other_job_attachment_uses_current_account_ov_acl_instead_of_its_local_file(rig, monkeypatch):
    waiting = await complete(rig, create(rig, rounds=2))
    other = await complete(rig, create(rig, rounds=2, name="Other checkpoint"))
    _, attachment = await stage_attachment(rig, other)
    reads = []
    original_read = rig._round_read_reference

    async def record_remote_read(account, uri):
        reads.append((account, uri))
        return await original_read(account, uri)

    monkeypatch.setattr(rig, "_round_read_reference", record_remote_read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(rig)), base_url="http://test") as client:
        response = await client.post(
            f"/api/mining/jobs/{waiting['job_id']}/continue", json=decision(waiting, attachment["uri"])
        )
    assert response.status_code == 404
    assert reads == [("default", attachment["uri"])]
    unchanged = rig.repository.get_job(waiting["job_id"])
    assert unchanged["status"] == "waiting_human" and not unchanged["supplements"]
    assert not unchanged["pending_reference_snapshots"]


@pytest.mark.asyncio
@pytest.mark.parametrize("variant", ["unregistered-sibling", "wrong-round", "encoded-path"])
async def test_nonmatching_runs_uri_never_bypasses_ov_reference_validation(rig, monkeypatch, variant):
    waiting = await complete(rig, create(rig, rounds=2))
    staged, attachment = await stage_attachment(rig, waiting)
    uri = attachment["uri"]
    if variant == "unregistered-sibling":
        uri = uri.rsplit("/", 1)[0] + "/unknown.md"
    elif variant == "wrong-round":
        uri = uri.replace("/rounds/round-2/", "/rounds/round-3/")
    else:
        uri = uri.replace("/source/", "/%73ource/")
    reads = []
    original_read = rig._round_read_reference

    async def record_remote_read(account, reference_uri):
        reads.append((account, reference_uri))
        return await original_read(account, reference_uri)

    monkeypatch.setattr(rig, "_round_read_reference", record_remote_read)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(rig)), base_url="http://test") as client:
        response = await client.post(f"/api/mining/jobs/{staged['job_id']}/continue", json=decision(staged, uri))
    assert response.status_code == 404
    assert reads == [("default", uri)]
    assert rig.repository.get_job(staged["job_id"])["status"] == "waiting_human"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "tamper",
    ["content", "hash", "name", "uri", "foreign-record", "path-escape", "symlink", "utf8", "size", "checkpoint"],
)
async def test_staged_attachment_metadata_and_local_content_are_verified_before_acceptance(rig, monkeypatch, tamper):
    waiting = await complete(rig, create(rig, rounds=2))
    staged, attachment = await stage_attachment(rig, waiting)
    changed = copy.deepcopy(attachment)
    path = Path(attachment["local_path"])
    if tamper == "content":
        path.write_text("Tampered facts.")
    elif tamper == "hash":
        changed["sha256"] = "0" * 64
    elif tamper == "name":
        changed["name"] = "unknown.md"
    elif tamper == "uri":
        changed["uri"] = changed["uri"].replace("/rounds/round-2/", "/rounds/round-3/")
    elif tamper == "foreign-record":
        changed["uri"] = changed["uri"].replace(staged["job_id"], "ovmine-other-job")
    elif tamper in {"path-escape", "symlink"}:
        outside = rig.repository.state_root / "outside.md"
        outside.write_bytes(path.read_bytes())
        if tamper == "path-escape":
            changed["local_path"] = str(outside)
        else:
            path.unlink()
            path.symlink_to(outside)
    elif tamper == "utf8":
        path.write_bytes(b"\xff\xfe")
    elif tamper == "size":
        changed["content_bytes"] += 1
    else:
        checkpoint = {**staged["pending_checkpoint"], "round": 2}
        rig.repository.update_job(staged["job_id"], pending_checkpoint=checkpoint)
    staged = rig.repository.update_job(staged["job_id"], pending_attachments=[changed])
    remote_reads = []

    async def reject_remote_read(account, uri):
        remote_reads.append((account, uri))
        raise AssertionError("A matching but invalid staged attachment must fail locally")

    monkeypatch.setattr(rig, "_round_read_reference", reject_remote_read)
    with pytest.raises(ValueError):
        await rig._stage_round_references(staged, {"scope": [changed["uri"]]})
    assert not remote_reads
    unchanged = rig.repository.get_job(staged["job_id"])
    assert unchanged["status"] == "waiting_human" and not unchanged["pending_reference_snapshots"]


@pytest.mark.asyncio
async def test_large_staged_attachment_citation_respects_the_existing_reference_file_limit(rig):
    waiting = await complete(rig, create(rig, rounds=2))
    staged, attachment = await stage_attachment(rig, waiting, content=b"a" * (2 * 1024 * 1024 + 1))
    with pytest.raises(ValueError, match="2 MB"):
        await rig._stage_round_references(staged, {"scope": [attachment["uri"]]})
    assert not rig.repository.get_job(staged["job_id"])["pending_reference_snapshots"]


@pytest.mark.asyncio
async def test_frozen_reference_cache_does_not_hide_staged_attachment_tampering(rig):
    waiting = await complete(rig, create(rig, rounds=2))
    staged, attachment = await stage_attachment(rig, waiting)
    cached = await rig._stage_round_references(staged, {"scope": [attachment["uri"]]})
    snapshot = cached["pending_reference_snapshots"][0]
    frozen = Path(snapshot["local_path"]).read_bytes()
    Path(attachment["local_path"]).write_text("Changed after citation preparation.")
    with pytest.raises(ValueError, match="已被修改"):
        await rig._stage_round_references(cached, {"scope": [attachment["uri"]]})
    assert Path(snapshot["local_path"]).read_bytes() == frozen
