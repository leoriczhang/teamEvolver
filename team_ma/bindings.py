"""Accept member changes without claiming they are already applied in MA."""
from .commands import enqueue
from .repository import MARepository
from .schemas import MAConflict


async def accept_bindings(conn, services, *, tenant_id, connection_id, name, body, operation_id, actor):
    repo = MARepository(services.tx)
    previous = await repo.operation(conn, operation_id)
    if previous:
        request = previous["input"]["request"]
        if (previous["connection_id"] != connection_id or previous["kind"] != "ma.skill"
                or request.get("action") != "bind" or request.get("name") != name
                or request.get("binding_request") != body):
            raise MAConflict("Idempotency-Key already used for another binding change")
        return {"accepted": True, "job_id": operation_id, "status_url": f"/api/jobs/{operation_id}",
                "result_url": f"/api/ma/skill-jobs/{operation_id}", "dedupe": True}
    agents = {r["logical_name"] for r in await repo.all_resources(conn, connection_id, kind="agent")}
    if not set(body["agent_ids"]) <= agents:
        raise ValueError("Unknown target Agent")
    rows = await repo.all_resources(conn, connection_id, kind="skill")
    existing = next((r for r in rows if r["logical_name"] == name), None)
    record = {**(existing["record"] if existing else {}), "name": name, "desired_agents": body["agent_ids"]}
    if not record.get("id"):
        # A not-yet-created Skill has no native resource to mount. Save the
        # explicit targets (including an empty choice) for Candidate publication.
        record["agents"] = body["agent_ids"]
        if not existing and body["expected_revision"] == 0:
            await repo.create_binding(conn, tenant_id=tenant_id, connection_id=connection_id, name=name, record=record)
        else:
            await repo.bind(conn, tenant_id=tenant_id, connection_id=connection_id, kind="skill",
                            name=name, record=record, expected_revision=body["expected_revision"])
        return {"saved": True}
    version = int(record.get("latest_version") or record.get("version") or 0)
    if record.get("deleted") or not version:
        raise MAConflict("Cloud Skill is unavailable; discover the connection before binding")
    job = await enqueue(
        conn, services, tenant_id=tenant_id, connection_id=connection_id, kind="ma.skill",
        operation_id=operation_id, body={
            "name": name, "action": "bind", "skill_id": record["id"], "base_version": version,
            "agents": body["agent_ids"], "bundle": None, "target_version": None, "metadata": {},
            "actor": actor, "source": "console-binding", "mutation_id": operation_id, "binding_request": body,
        },
    )
    if not job["dedupe"]:
        # Preserve observed agents/bound_versions until the Worker verifies MA.
        await repo.bind(conn, tenant_id=tenant_id, connection_id=connection_id, kind="skill",
                        name=name, record=record, expected_revision=body["expected_revision"])
    return {**job, "result_url": f"/api/ma/skill-jobs/{operation_id}"}
