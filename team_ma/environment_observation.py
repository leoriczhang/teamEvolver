"""Observe business Session environments without rewriting accepted task snapshots."""

from datetime import datetime, timezone

from teamEvolver.managed_agents import MAError
from teamEvolver.managed_agents.events import tag_values

from .commands import enqueue
from .repository import MARepository
from .schemas import digest, validate_config


def _created_at(value):
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc) if parsed.tzinfo else None
    except ValueError:
        return None


def _excluded(session, excluded):
    return session.get("id") in excluded or any(
        str(tag).startswith(("te-replay:", "te-test:")) for tag in tag_values(session.get("tags"))
    )


async def latest_environment(client, entry_agent_id, excluded):
    """MA lists Sessions by creation time, descending; never use activity time."""
    page, visited = None, set()
    for _ in range(10):
        result = await client.page(
            "sessions", agent_id=entry_agent_id, order="desc", limit=50,
            **({"page": page} if page else {}),
        )
        rows = [row for row in (result.get("data") or []) if isinstance(row, dict)
                and isinstance(row.get("id"), str) and _created_at(row.get("created_at"))]
        rows.sort(key=lambda row: (_created_at(row["created_at"]), row["id"]), reverse=True)
        for row in rows:
            if _excluded(row, excluded):
                continue
            try:
                session = await client.request("GET", f"sessions/{row['id']}")
            except MAError as exc:
                if exc.status == 404:
                    continue
                raise
            if _excluded(session, excluded) or session.get("id") != row["id"]:
                continue
            agent = session.get("agent") or {}
            if not isinstance(agent, dict) or agent.get("id") != entry_agent_id:
                continue
            created = _created_at(session.get("created_at")) or _created_at(row["created_at"])
            environment = session.get("environment") or {}
            if not isinstance(environment, dict):
                continue
            ident = environment.get("id")
            if not isinstance(ident, str) or not ident.strip() or len(ident) > 200:
                continue
            return {
                "session_id": session["id"], "created_at": created.isoformat(),
                "environment_id": ident.strip(),
                "environment_hash": (digest(environment["config"])
                                     if isinstance(environment.get("config"), dict) else ""),
            }
        page = result.get("next_page")
        if not page:
            return None
        if page in visited:
            raise MAError(502, "PaginationLoop")
        visited.add(page)
    # Do not select an older Session when the bounded search found no business Session.
    return None


async def _superseded(repo, conn, task, connection, live, operation):
    if task.kind == "ma.collect" or not (
        live["config"]["environment_id"] != connection["config"]["environment_id"]
        or live["detail"].get("environment_observation_revision", 0) > connection["revision"]
    ):
        return None
    result = {"status": "environment_superseded", "connection_revision": live["revision"]}
    await repo.checkpoint(conn, task.task_id, operation["checkpoint"], result)
    return result


async def refresh_environment(task, services, client, connection, operation):
    """Persist observation and enqueue discovery atomically under the config lock."""
    # A partially executed discovery/release must finish with its frozen inputs.
    if task.kind != "ma.collect" and operation["checkpoint"]:
        return None
    repo = MARepository(services.tx)
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        live = await repo.connection(conn, task.subject_id)
        if not live["enabled"]:
            return None
        if live["revision"] != connection["revision"]:
            return await _superseded(repo, conn, task, connection, live, operation)
        excluded = await repo.replay_session_ids(conn, task.subject_id)
    observed = await latest_environment(client, connection["config"]["entry_agent_id"], excluded)
    if not observed:
        if not connection["config"]["environment_id"]:
            raise MAError(503, "EnvironmentNotObserved")
        return None
    async with services.tx.tenant_tx(task.tenant_id) as conn:
        await services.tasks.require_current(conn, task)
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))",
                           f"{task.tenant_id}:ma:{task.subject_id}")
        live = await repo.connection(conn, task.subject_id)
        if not live["enabled"]:
            return None
        if live["revision"] != connection["revision"]:
            return await _superseded(repo, conn, task, connection, live, operation)
        previous = live["detail"].get("environment_observation") or {}
        previous_time = _created_at(previous.get("created_at"))
        current_time = _created_at(observed["created_at"])
        if previous_time and (current_time, observed["session_id"]) < (
                previous_time, str(previous.get("session_id") or "")):
            return None
        applied = await repo.applied(conn, task.subject_id)
        baseline_hash = previous.get("environment_hash")
        if not previous and applied and applied["record"].get("connection_revision") == live["revision"]:
            baseline_hash = applied["record"].get("environment_hash")
        changed = (
            observed["environment_id"] != live["config"]["environment_id"]
            or bool(observed["environment_hash"] and baseline_hash
                    and observed["environment_hash"] != baseline_hash)
        )
        detail = {**live["detail"], "environment_observation": observed}
        if not changed:
            if previous != observed:
                await repo.status(conn, task.subject_id, live["status"], detail)
            return None
        config = validate_config({**live["config"], "environment_id": observed["environment_id"]})
        updated = await repo.save(
            conn, tenant_id=task.tenant_id, connection_id=task.subject_id, config=config,
            expected_revision=live["revision"], actor="session-environment-observer",
        )
        detail["environment_observation_revision"] = updated["revision"]
        if config["replay_enabled"]:
            detail.update(
                replay_resources={"status": "preparing"},
                replay_capability={"supported": False, "reason": "回放资源准备中"},
            )
        await repo.status(conn, task.subject_id, "discovering", detail)
        job = await enqueue(
            conn, services, tenant_id=task.tenant_id, connection_id=task.subject_id, kind="ma.discover",
            operation_id="ma-environment:" + digest([task.subject_id, updated["revision"], observed])[:40],
        )
        result = {"status": "environment_changed", "environment_id": observed["environment_id"],
                  "connection_revision": updated["revision"], "discovery_job_id": job["job_id"]}
        if task.kind != "ma.collect":
            await repo.checkpoint(conn, task.task_id, operation["checkpoint"], result)
        return result
