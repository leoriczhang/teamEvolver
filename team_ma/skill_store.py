"""Cloud-authoritative MA Skills and immutable validation snapshots."""
from __future__ import annotations

import asyncio
import copy
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from team_skills.library.bundle import bundle_tree_sha256, decode_bundle_payload, encode_bundle_payload
from team_skills.library.repository import (
    SkillCommit,
    SkillMutationConflict,
    SkillNotFound,
    SkillVersionConflict,
    bundle_metadata,
)
from teamEvolver.managed_agents import MAClient, MAError

from .discovery import read_skill_zip
from .repository import MARepository
from .schemas import canonical, digest, unpack


def timestamp(value):
    if isinstance(value, datetime):
        return value
    if isinstance(value, (float, int)):
        return datetime.fromtimestamp(value, timezone.utc)
    if value:
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    return None


class CloudSnapshot:
    def __init__(self, provider, tenant, connections, bindings, selected=""):
        self.provider, self.tenant = provider, tenant
        self.connections, self.bindings, self.selected = connections, bindings, selected
        self.heads, self.bundles, self.history, self.entries = {}, {}, {}, []
        self.ambiguous = set()

    def managed(self, name):
        if name in self.ambiguous:
            raise SkillMutationConflict("Same-name cloud Skills require an explicit connection_id")
        return name in self.bindings or name in self.heads

    def target(self, name):
        self.managed(name)
        if name in self.bindings:
            return self.bindings[name]
        eligible = [c for c in self.connections if c["enabled"]
                    and (not self.selected or c["connection_id"] == self.selected)]
        if len(eligible) != 1:
            raise SkillMutationConflict("Choose one MA connection with connection_id before creating a cloud Skill")
        return {"connection": eligible[0], "record": {"name": name, "agents": []}}

    def head(self, name):
        self.managed(name)
        return copy.deepcopy(self.heads.get(name))

    async def bundle(self, conn, name, version):
        binding = self.target(name)
        ident = binding["record"].get("id")
        key = (binding["connection"]["connection_id"], ident, int(version))
        if key in self.bundles:
            return dict(self.bundles[key])
        cached = await self.provider.cached(conn, key)
        if cached is None:
            raise SkillNotFound("Cloud version snapshot is unavailable; reload this version before validation")
        return cached[1]

    async def queue(self, conn, *, name, action, mutation_id, bundle=None, base_version=None,
                    actor="", source="console", metadata=None, target_version=None):
        binding = self.target(name)
        operation_id = "ma-skill:" + digest([binding["connection"]["connection_id"], mutation_id])[:40]
        existing = await self.provider.repo.operation(conn, operation_id)
        if existing:
            old = existing["input"]["request"]
            incoming = {"name": name, "action": action,
                        "bundle": encode_bundle_payload(bundle) if bundle is not None else None,
                        "target_version": target_version}
            if (any(old.get(k) != v for k, v in incoming.items() if k != "action")
                    or (old["action"] != action and
                        not {old["action"], action} <= {"publish", "update"})):
                raise SkillMutationConflict("Idempotency-Key already used for a different cloud Skill edit")
            if base_version is not None and old["base_version"] != int(base_version):
                raise SkillMutationConflict("Idempotency-Key already used for another base version")
            job = {"job_id": operation_id, "status": "accepted", "accepted": True,
                   "status_url": f"/api/jobs/{operation_id}",
                   "result_url": f"/api/ma/skill-jobs/{operation_id}"}
            return SkillCommit(name, action, "accepted", old["base_version"], old["base_version"], mutation_id,
                               record={"job": job, "backend": "managed-agents"})
        current = self.heads.get(name)
        previous = (int(current["version"]) if current else
                    int(binding["record"].get("latest_version") or 0) if action == "delete" else 0)
        expected = previous if base_version is None else int(base_version)
        if expected != previous:
            raise SkillVersionConflict(f"{name} is at cloud v{previous}, not expected v{expected}")
        payload = {
            "name": name, "action": action, "skill_id": binding["record"].get("id", ""),
            "base_version": expected, "bundle": encode_bundle_payload(bundle) if bundle is not None else None,
            "actor": actor, "source": source, "metadata": metadata or {},
            "target_version": target_version, "mutation_id": mutation_id,
            "agents": binding["record"].get("desired_agents", binding["record"].get("agents")) or [],
            "no_change": bool(current and bundle is not None
                              and current["tree_sha256"] == bundle_tree_sha256(bundle)),
        }
        from .commands import enqueue
        connection = binding["connection"]
        job = await enqueue(
            conn, self.provider.services, tenant_id=self.tenant, connection_id=connection["connection_id"],
            kind="ma.skill", body=payload,
            operation_id=operation_id,
        )
        job["result_url"] = f"/api/ma/skill-jobs/{operation_id}"
        return SkillCommit(name, action, "accepted", previous, previous, mutation_id,
                           restored_from=target_version, record={"job": job, "backend": "managed-agents"})


class CloudSkillProvider:
    def __init__(self, services):
        self.services, self.repo = services, MARepository(services.tx)

    @asynccontextmanager
    async def client(self, tenant, connection):
        async with self.services.tx.tenant_tx(tenant) as conn:
            token = await self.services.extras["secret_repo"].resolve(
                conn, secret_ref=connection["config"]["ma_secret_ref"],
            )
        factory = self.services.extras.get("ma_client_factory") or MAClient
        async with factory(connection["config"]["ma_base_url"], token) as client:
            yield client

    async def cached(self, conn, key):
        row = await conn.fetchrow(
            f"SELECT metadata,bundle,tree_sha256 FROM {self.repo.schema}.ma_skill_snapshots "
            "WHERE connection_id=$1 AND skill_id=$2 AND version=$3", *key,
        )
        if row is None:
            return None
        bundle = decode_bundle_payload(unpack(row["bundle"]))
        if bundle_tree_sha256(bundle) != row["tree_sha256"]:
            raise RuntimeError("Cloud Skill snapshot hash mismatch")
        return unpack(row["metadata"]), bundle

    async def fetch_version(self, tenant, connection, client, ident, name, version):
        key = (connection["connection_id"], ident, int(version))
        async with self.services.tx.tenant_tx(tenant) as conn:
            cached = await self.cached(conn, key)
        if cached is not None:
            return cached
        remote = await client.request("GET", f"skills/{ident}/versions/{version}")
        data = await client.download(f"skills/{ident}/versions/{version}/content")
        item = await asyncio.to_thread(read_skill_zip, data)
        if item.name != name:
            raise SkillMutationConflict("Cloud Skill package name differs from its resource binding")
        if str(remote.get("version")) != str(version) or remote.get("skill_id", ident) != ident:
            raise SkillMutationConflict("Cloud Skill version readback mismatch")
        meta = {
            **bundle_metadata(name, item.files), "version": int(version), "action": "cloud",
            "created_at": remote.get("created_at"), "actor": "", "restored_from": None, "deleted": False,
            "mutation_id": "", "file_count": len(item.files), "total_bytes": sum(map(len, item.files.values())),
        }
        async with self.services.tx.tenant_tx(tenant) as conn:
            await conn.execute(
                f"INSERT INTO {self.repo.schema}.ma_skill_snapshots"
                "(tenant_id,connection_id,skill_id,version,tree_sha256,metadata,bundle) "
                "VALUES($1,$2,$3,$4,$5,$6::jsonb,$7::jsonb) ON CONFLICT DO NOTHING",
                tenant, *key, meta["tree_sha256"], canonical(meta), canonical(encode_bundle_payload(item.files)),
            )
        return meta, item.files

    async def load(self, tenant, *, versions=None, connection_id=""):
        async with self.services.tx.tenant_tx(tenant) as conn:
            connections = await self.repo.connections(conn)
            if connection_id and not any(c["connection_id"] == connection_id for c in connections):
                raise SkillNotFound("MA connection is unavailable in this tenant")
            bindings = {}
            ambiguous = set()
            for connection in connections:
                if connection_id and connection["connection_id"] != connection_id:
                    continue
                for row in await self.repo.all_resources(conn, connection["connection_id"], kind="skill"):
                    name = row["logical_name"]
                    if name in bindings:
                        ambiguous.add(name)
                        bindings[(name, connection["connection_id"])] = {
                            "connection": connection, "record": row["record"]}
                    else:
                        bindings[name] = {"connection": connection, "record": row["record"]}
        result = CloudSnapshot(self, tenant, connections, bindings, connection_id)
        result.ambiguous = ambiguous
        gate = asyncio.Semaphore(4)

        async def read(key, binding):
            name = key[0] if isinstance(key, tuple) else key
            if not binding["record"].get("id"):
                return
            async with gate:
                connection, ident = binding["connection"], binding["record"]["id"]
                async with self.client(tenant, connection) as client:
                    try:
                        remote = await client.request("GET", f"skills/{ident}")
                    except MAError as exc:
                        if exc.status != 404:
                            raise
                        return  # Omit a remotely deleted head; never revive its TE import.
                    version = int(remote.get("latest_version") or 0)
                    if not version:
                        return  # Version cleanup may precede root resource deletion.
                    meta, bundle = await self.fetch_version(tenant, connection, client, ident, name, version)
                    head = {
                        **meta, "name": name, "skill_id": ident, "version": version, "deleted": False,
                        "backend": "managed-agents", "connection_id": connection["connection_id"],
                        "updated_by": "", "updated_at": timestamp(remote.get("updated_at")),
                        "created_at": timestamp(remote.get("created_at")),
                        "evolution_reset_at": timestamp(binding["record"].get("evolution_reset_at")),
                        "bound_version": binding["record"].get("bound_version", binding["record"].get("version")),
                        "bound_versions": binding["record"].get("bound_versions", {}),
                    }
                    result.entries.append(head)
                    if name not in ambiguous:
                        result.heads[name] = head
                    result.bundles[(connection["connection_id"], ident, version)] = bundle
                    if versions and name in versions and name not in ambiguous:
                        for requested in versions[name]:
                            saved, files = await self.fetch_version(tenant, connection, client, ident, name, requested)
                            result.bundles[(connection["connection_id"], ident, int(requested))] = files
                        history = await client.listing(f"skills/{ident}/versions")
                        result.history[name] = [
                            {"version": int(v["version"]), "action": "cloud", "deleted": False,
                             "created_at": timestamp(v.get("created_at")), "actor": "",
                             "restored_from": None, "tree_sha256": ""}
                            for v in sorted(history, key=lambda v: int(v["version"]), reverse=True)
                        ]
        await asyncio.gather(*(read(key, binding) for key, binding in bindings.items()))
        return result

    async def boundary(self, conn, name):
        rows = await conn.fetch(
            f"SELECT record FROM {self.repo.schema}.ma_resource_bindings WHERE kind='skill' AND logical_name=$1",
            name,
        )
        values = [timestamp(unpack(row["record"]).get("evolution_reset_at")) for row in rows]
        return bool(rows), max((v for v in values if v is not None), default=None)

    async def refresh(self, value, name):
        """Recheck a planning base outside the Candidate persistence transaction."""
        binding = value.target(name)
        ident, connection = binding["record"].get("id"), binding["connection"]
        if not ident:
            return
        async with self.client(value.tenant, connection) as client:
            remote = await client.request("GET", f"skills/{ident}")
            version = int(remote["latest_version"])
            meta, files = await self.fetch_version(value.tenant, connection, client, ident, name, version)
        old = value.heads.get(name) or {}
        value.heads[name] = {**old, **meta, "version": version, "name": name, "skill_id": ident,
                             "backend": "managed-agents", "connection_id": connection["connection_id"],
                             "updated_at": timestamp(remote.get("updated_at"))}
        value.bundles[(connection["connection_id"], ident, version)] = files

    async def reset(self, conn, value, name):
        binding = value.target(name)
        boundary = await conn.fetchval("SELECT clock_timestamp()")
        record = {**binding["record"], "evolution_reset_at": boundary.isoformat()}
        await self.repo.bind(conn, tenant_id=value.tenant, connection_id=binding["connection"]["connection_id"],
                             kind="skill", name=name, record=record)
        binding["record"] = record
        head = value.head(name)
        head["evolution_reset_at"] = boundary
        value.heads[name] = head
        return head
