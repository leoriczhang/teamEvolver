"""Tenant-scoped immutable datasource connection settings and credential handles."""
from __future__ import annotations

import copy
import json
import os
import re

from .adapters._runtime import validate_content
from .adapters._shared.connection_settings import expanded_settings, normalize_settings
from .adapters.repository import AdapterRepository
from .config import DEFAULT_SESSION_CONFIG

_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,99}\Z")


class ConnectionConflict(ValueError):
    pass


class ConnectionNotFound(ValueError):
    pass


def connection_id(value):
    if not isinstance(value, str) or not _ID.fullmatch(value) or value == "default":
        raise ValueError("connection_id must be 1..100 letters/digits/._- and cannot be default")
    return value


def freeze_record(record):
    return {"connection_id": record["connection_id"], "revision": record["revision"],
            **copy.deepcopy(record["document"])}


def _record(row):
    result = dict(row)
    if isinstance(result["document"], str):
        result["document"] = json.loads(result["document"])
    return result


class ConnectionRepository:
    def __init__(self, tx):
        self.tx, self.schema = tx, tx.pool.schema

    async def get(self, conn, identity, revision=None):
        row = await conn.fetchrow(
            f"SELECT v.connection_id,v.revision,v.document FROM {self.schema}.datasource_connection_versions v "
            f"JOIN {self.schema}.datasource_connections h USING(tenant_id,connection_id) "
            "WHERE v.connection_id=$1 AND v.revision=COALESCE($2::bigint,h.revision)", identity, revision,
        )
        if row is None:
            raise ConnectionNotFound("datasource connection not found")
        return _record(row)

    async def list(self, conn):
        rows = await conn.fetch(
            f"SELECT v.connection_id,v.revision,v.document FROM {self.schema}.datasource_connections h "
            f"JOIN {self.schema}.datasource_connection_versions v USING (tenant_id,connection_id,revision) "
            "ORDER BY v.connection_id"
        )
        return [_record(row) for row in rows]

    async def freeze(self, conn, identity, revision=None):
        return freeze_record(await self.get(conn, identity, revision))

    async def save(self, conn, *, tenant_id, identity, expected_revision, document):
        from .schedule import validate_schedule

        connection_id(identity)
        if type(expected_revision) is not int or expected_revision < 0:
            raise ValueError("expected_revision must be a nonnegative integer")
        if not isinstance(document, dict) or set(document) - {
            "label", "enabled", "adapter_id", "adapter_revision", "settings", "default_filters", "schedule",
        }:
            raise ValueError("unsupported datasource connection fields")
        value = copy.deepcopy(document)
        if type(value.get("enabled", True)) is not bool:
            raise ValueError("enabled must be boolean")
        value.setdefault("enabled", True)
        if not isinstance(value.get("label", ""), str) or len(value.get("label", "")) > 200:
            raise ValueError("label must be bounded text")
        value.setdefault("label", identity)
        if not isinstance(value.get("adapter_id"), str) or type(value.get("adapter_revision")) is not int:
            raise ValueError("adapter_id and adapter_revision are required")
        adapters = AdapterRepository(self.tx)
        version = await adapters.get_version(conn, adapter_id=value["adapter_id"], revision=value["adapter_revision"])
        if version is None:
            raise ValueError("adapter version not found")
        metadata = validate_content(version["source"], value["adapter_id"])
        if value["enabled"] and metadata.get("enabled", True) is False:
            raise ValueError("adapter source is disabled")
        value["settings"] = normalize_settings(value.get("settings", {}), metadata)
        filters = value.setdefault("default_filters", {})
        if (not isinstance(filters, dict) or set(filters) - set(metadata.get("supported_filters", []))
                or any(key.endswith("timestamp") for key in filters)):
            raise ValueError("default_filters must use supported non-time fields")
        json.dumps(filters, allow_nan=False)
        value["schedule"] = validate_schedule(value.get("schedule", DEFAULT_SESSION_CONFIG["datasource_schedule"]))
        await conn.execute("SELECT pg_advisory_xact_lock(hashtextextended($1,0))", f"{tenant_id}:connection:{identity}")
        current = await conn.fetchval(
            f"SELECT revision FROM {self.schema}.datasource_connections WHERE connection_id=$1", identity,
        ) or 0
        if current != expected_revision:
            raise ConnectionConflict("connection changed; reload before saving")
        revision = current + 1
        await adapters.bind(conn, tenant_id=tenant_id, binding_key="datasource:" + identity,
                            adapter_id=value["adapter_id"], revision=value["adapter_revision"])
        await conn.execute(
            f"INSERT INTO {self.schema}.datasource_connections(tenant_id,connection_id,revision) VALUES($1,$2,$3) "
            "ON CONFLICT(tenant_id,connection_id) DO UPDATE SET revision=EXCLUDED.revision,updated_at=now()",
            tenant_id, identity, revision,
        )
        await conn.execute(
            f"INSERT INTO {self.schema}.datasource_connection_versions VALUES($1,$2,$3,$4::jsonb,now())",
            tenant_id, identity, revision, json.dumps(value, allow_nan=False),
        )
        return await self.get(conn, identity)


async def resolve_settings(services, tenant_id, snapshot, metadata):
    settings = expanded_settings(snapshot["settings"], metadata)
    result = copy.deepcopy(settings["options"])
    for alias, name in settings["env_refs"].items():
        if not os.environ.get(name):
            raise ValueError("connection environment credential is unavailable")
        result[alias] = os.environ[name]
    if settings["secret_refs"]:
        secrets = services.extras.get("secret_repo")
        if secrets is None:
            raise ValueError("connection credential resolver is unavailable")
        async with services.tx.tenant_tx(tenant_id) as conn:
            for alias, reference in settings["secret_refs"].items():
                value = await secrets.resolve(conn, secret_ref=reference)
                if not isinstance(value, str) or not value.strip():
                    raise ValueError("connection credential is empty")
                result[alias] = value
    return result
