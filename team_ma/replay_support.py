"""Injected MA Replay capabilities; the Replay Adapter does not import this implementation."""

import copy
import json

from session_ingestion.queries import SessionQueries
from teamEvolver.managed_agents import MAClient
from teamEvolver.storage.postgres import PostgresObjectStore

from .discovery import replay_capability
from .effects import Effects
from .recovery import delete_owned, recover_owned
from .repository import MARepository
from .runtime_binding import bind_runtime


def capabilities(services, tenant_id, task=None):
    source_cache = {}

    async def original_file_id(record, material):
        """Return a transport hint, never an alternative source of input bytes."""
        sid = material.get("source_session_id")
        if not sid:
            return ""
        if sid not in source_cache:
            async with services.tx.tenant_tx(tenant_id) as conn:
                row = await SessionQueries(services.tx).get_session(conn, session_id=sid)
                source = {}
                if row and row.get("content_ref"):
                    try:
                        raw = await PostgresObjectStore(services.tx, tenant_id).get(conn, row["content_ref"])
                    except FileNotFoundError:
                        pass
                    else:
                        decoded = json.loads(raw)
                        source = decoded.get("session", decoded)
                source_cache[sid] = source
        source = source_cache[sid]
        if (source.get("runtime") or {}).get("connection_id") != record["connection_id"]:
            return ""
        matches = {m["original_file_id"] for m in source.get("source_materials") or []
                   if m.get("original_file_id") and m.get("sha256") == material["sha256"]}
        return next(iter(matches)) if len(matches) == 1 else ""

    async def client(record):
        async with services.tx.tenant_tx(tenant_id) as conn:
            secret = await services.extras["secret_repo"].resolve(conn, secret_ref=record["config"]["ma_secret_ref"])
        factory = services.extras.get("ma_client_factory") or MAClient
        return factory(record["config"]["ma_base_url"], secret)

    def effects(connection_id, owner_id):
        owner = task.task_id + "/" + owner_id if task else owner_id
        return Effects(services, tenant_id, connection_id, owner, task=task)

    async def save_artifact(name, data, owner_id):
        import hashlib
        sha = hashlib.sha256(data).hexdigest()
        key = f"ma/replay/{owner_id}/{sha}"
        async with services.tx.tenant_tx(tenant_id) as conn:
            await PostgresObjectStore(services.tx, tenant_id).put(conn, key, data)
        return {"path": name, "object_ref": key, "sha256": sha, "size": len(data)}

    async def cleanup(connection_id, owner_id, remote_client):
        repo = MARepository(services.tx)
        async with services.tx.tenant_tx(tenant_id) as conn:
            owner = task.task_id + "/" + owner_id if task else owner_id
            owned = await repo.effects(conn, connection_id, owner_id=owner)
        for effect in reversed(owned):
            if effect["kind"] not in {"replay-session", "replay-agent", "replay-skill", "replay-file"}:
                continue
            remote_id = effect["remote_id"]
            if effect["status"] in {"cleaned", "failed"}:
                continue
            if not remote_id:
                try:
                    receipt = await recover_owned(remote_client, effect)
                except Exception:
                    receipt = None
                if receipt:
                    remote_id, effect["receipt"] = receipt["id"], receipt
            async with services.tx.tenant_tx(tenant_id) as conn:
                await repo.confirm_effect(conn, effect["effect_id"], remote_id, effect["receipt"],
                                          status="cleanup_pending")
            try:
                if not remote_id or not await delete_owned(remote_client, effect["kind"], remote_id):
                    continue
            except Exception:
                continue
            async with services.tx.tenant_tx(tenant_id) as conn:
                await repo.confirm_effect(conn, effect["effect_id"], remote_id, effect["receipt"], status="cleaned")
        async with services.tx.tenant_tx(tenant_id) as conn:
            await repo.retire_session_events(conn, connection_id)

    return {"run": services.tx.pool.run, "client": client, "effects": effects, "save_artifact": save_artifact,
            "cleanup": cleanup, "runtime_binding": bind_runtime, "replay_capability": replay_capability,
            "original_file_id": original_file_id, "copy": copy.deepcopy}
