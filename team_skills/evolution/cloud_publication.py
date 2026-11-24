"""Public candidate gate and finalization for a cloud-native Skill command."""
from team_skills.candidates.repository import CandidateRepository, CandidateStateError

from .reset import retire_cloud_baseline


async def guard(conn, tx, task, request):
    metadata = request["metadata"]
    if not metadata.get("candidate_id"):
        return
    item = await CandidateRepository(tx).require(conn, metadata["candidate_id"], for_update=True)
    if (item["revision"] != metadata["revision"] or item["status"] != "validating"
            or item["validate_task_id"] != task.task_id):
        raise CandidateStateError("Cloud publication Candidate changed; preserve the pending proposal")


async def finalize(conn, tx, task, request, result, binding):
    metadata = request["metadata"]
    await guard(conn, tx, task, request)
    boundary = await conn.fetchval("SELECT clock_timestamp()")
    binding["evolution_reset_at"] = boundary.isoformat()
    if metadata.get("candidate_id"):
        decision = {**result, **{key: metadata[key] for key in (
            "review_overridden", "validation_overridden", "validation_verdict",
        )
                                if key in metadata}}
        if metadata.get("publish_mode"):
            decision["mode"] = metadata["publish_mode"]
        await CandidateRepository(tx).transition(
            conn, tenant_id=task.tenant_id, candidate_id=metadata["candidate_id"], to_status="published",
            actor=request["actor"], expected_revision=metadata["revision"], decision=decision,
            published_version=result["version"], mutation_id=request["mutation_id"],
        )
    else:
        await retire_cloud_baseline(conn, tx, tenant_id=task.tenant_id, name=request["name"], item=binding)


async def failed(conn, tx, task, request, code):
    metadata = request["metadata"]
    if not metadata.get("candidate_id"):
        return
    repo = CandidateRepository(tx)
    item = await repo.require(conn, metadata["candidate_id"], for_update=True)
    if item["status"] == "validating" and item["validate_task_id"] == task.task_id:
        await repo.transition(
            conn, tenant_id=task.tenant_id, candidate_id=item["candidate_id"], to_status="open",
            actor=request["actor"], expected_revision=item["revision"],
            decision={"status": "cloud_publish_failed", "accepted": False, "reason": code,
                      "job_id": task.task_id},
        )
