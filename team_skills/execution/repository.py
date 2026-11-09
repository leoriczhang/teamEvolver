"""Tenant-scoped immutable Prompt assets and release heads; caller owns transactions."""
from __future__ import annotations

import copy
import uuid

from team_skills.evolution.repository import canonical, lock_key, unpack
from team_skills.library.repository import SkillLibraryError, SkillNotFound

from .contracts import composition_hash, plan_for, validate_profile


class ExecutionVersionConflict(SkillLibraryError):
    status_code = 409


class ExecutionRepository:
    def __init__(self, tx):
        self.tx, self.schema = tx, tx.pool.schema

    async def profiles(self, conn):
        rows = await conn.fetch(f"SELECT * FROM {self.schema}.execution_profiles ORDER BY profile_id")
        return [self._profile(row) for row in rows]

    @staticmethod
    def _profile(row):
        return {**unpack(row["document"]), "revision": row["revision"],
                "current_release_id": row["current_release_id"]}

    async def profile(self, conn, profile_id, *, for_update=False):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.execution_profiles WHERE profile_id=$1"
            + (" FOR UPDATE" if for_update else ""), profile_id,
        )
        if not row:
            raise SkillNotFound("execution profile not found")
        return self._profile(row)

    async def release(self, conn, release_id):
        row = await conn.fetchrow(
            f"SELECT * FROM {self.schema}.execution_releases WHERE release_id=$1", release_id,
        )
        if not row:
            raise SkillNotFound("execution release not found")
        item = dict(row)
        item["document"] = unpack(item["document"])
        if composition_hash(item["document"]) != item["composition_hash"]:
            raise RuntimeError("execution composition hash mismatch")
        return item

    async def current(self, conn, profile_id):
        profile = await self.profile(conn, profile_id)
        return profile, await self.release(conn, profile["current_release_id"])

    async def check_registration_available(self, conn, profile_id, component_ids):
        """Preview availability only; register() still locks and checks atomically."""
        if await conn.fetchval(f"SELECT 1 FROM {self.schema}.execution_profiles WHERE profile_id=$1", profile_id):
            raise ExecutionVersionConflict("profile already exists; baseline registration is not an update path")
        if await conn.fetchval(
            f"SELECT 1 FROM {self.schema}.execution_assets WHERE component_id=ANY($1::text[]) LIMIT 1",
            component_ids,
        ):
            raise ExecutionVersionConflict("component_id already belongs to another profile")

    async def register(self, conn, *, tenant_id, raw, skill_dependencies, actor):
        profile = validate_profile(raw)
        if (not isinstance(skill_dependencies, list)
                or sorted(d.get("name", "") for d in skill_dependencies) != profile["affected_skill_ids"]):
            raise SkillLibraryError("execution dependencies must match the profile's affected Skills")
        pid = profile["profile_id"]
        await lock_key(conn, tenant_id, f"execution-profile:{pid}")
        if await conn.fetchval(f"SELECT 1 FROM {self.schema}.execution_profiles WHERE profile_id=$1", pid):
            raise ExecutionVersionConflict("profile already exists; baseline registration is not an update path")
        components = profile.pop("components")
        await conn.execute(
            f"INSERT INTO {self.schema}.execution_profiles(tenant_id,profile_id,document) VALUES($1,$2,$3::jsonb)",
            tenant_id, pid, canonical(profile),
        )
        for part in sorted(components, key=lambda item: item["component_id"]):
            cid = part["component_id"]
            await lock_key(conn, tenant_id, f"execution-asset:{cid}")
            if await conn.fetchval(f"SELECT 1 FROM {self.schema}.execution_assets WHERE component_id=$1", cid):
                raise ExecutionVersionConflict("component_id already belongs to another profile")
            await conn.execute(
                f"INSERT INTO {self.schema}.execution_assets VALUES($1,$2,$3,1)", tenant_id, pid, cid,
            )
            await self._version(conn, tenant_id, part, actor)
        document = {
            "execution_plan": plan_for(profile, components), "skill_dependencies": skill_dependencies,
            "origin": "baseline-registration",
        }
        await self._release(conn, tenant_id=tenant_id, profile=profile, document=document, actor=actor)
        return await self.current(conn, pid)

    async def _version(self, conn, tenant_id, part, actor):
        await conn.execute(
            f"INSERT INTO {self.schema}.execution_asset_versions"
            "(tenant_id,component_id,revision,component,actor) VALUES($1,$2,$3,$4::jsonb,$5)",
            tenant_id, part["component_id"], int(part["revision"]), canonical(part), actor,
        )

    async def _release(self, conn, *, tenant_id, profile, document, actor,
                       release_id="", candidate_id="", candidate_revision=None, validation_ref="", restored_from=""):
        rid = release_id or uuid.uuid4().hex
        sha = composition_hash(document)
        await conn.execute(
            f"INSERT INTO {self.schema}.execution_releases"
            "(tenant_id,release_id,profile_id,composition_hash,document,candidate_id,candidate_revision,"
            "validation_ref,actor,restored_from) VALUES($1,$2,$3,$4,$5::jsonb,$6,$7,$8,$9,$10)",
            tenant_id, rid, profile["profile_id"], sha, canonical(document), candidate_id,
            candidate_revision, validation_ref, actor, restored_from,
        )
        await conn.execute(
            f"UPDATE {self.schema}.execution_profiles SET current_release_id=$2,revision=revision+1,"
            "updated_at=now() WHERE profile_id=$1", profile["profile_id"], rid,
        )
        return await self.release(conn, rid)

    async def publish(self, conn, *, tenant_id, profile_id, expected_release_id, document, actor,
                      candidate_id, candidate_revision, validation_ref, restored_from=""):
        rid = f"candidate:{candidate_id}:r{candidate_revision}"
        existing = await conn.fetchval(
            f"SELECT release_id FROM {self.schema}.execution_releases WHERE release_id=$1", rid,
        )
        if existing:
            saved = await self.release(conn, rid)
            if saved["composition_hash"] != composition_hash(document):
                raise ExecutionVersionConflict("release id reused with different content")
            return saved
        profile = await self.profile(conn, profile_id, for_update=True)
        # A concurrent caller may have committed this same release while we waited.
        if await conn.fetchval(f"SELECT 1 FROM {self.schema}.execution_releases WHERE release_id=$1", rid):
            saved = await self.release(conn, rid)
            if saved["composition_hash"] != composition_hash(document):
                raise ExecutionVersionConflict("release id reused with different content")
            return saved
        if profile["current_release_id"] != expected_release_id:
            raise ExecutionVersionConflict("execution baseline changed after validation")
        baseline = (await self.release(conn, expected_release_id))["document"]["execution_plan"]
        before = {p["component_id"]: p for p in baseline["components"]}
        for part in sorted(document["execution_plan"]["components"], key=lambda item: item["component_id"]):
            cid = part["component_id"]
            row = await conn.fetchrow(
                f"SELECT current_revision FROM {self.schema}.execution_assets WHERE component_id=$1 FOR UPDATE", cid,
            )
            if not row or int(row["current_revision"]) != int(before[cid]["revision"]):
                raise ExecutionVersionConflict("business asset changed after validation")
            if part["content_hash"] != before[cid]["content_hash"]:
                if int(part["revision"]) != int(before[cid]["revision"]) + 1:
                    raise SkillLibraryError("invalid proposed component revision")
                await self._version(conn, tenant_id, part, actor)
                await conn.execute(
                    f"UPDATE {self.schema}.execution_assets SET current_revision=$2 WHERE component_id=$1",
                    cid, int(part["revision"]),
                )
        return await self._release(
            conn, tenant_id=tenant_id, profile=profile, document=document, actor=actor, release_id=rid,
            candidate_id=candidate_id, candidate_revision=candidate_revision, validation_ref=validation_ref,
            restored_from=restored_from,
        )

    async def releases(self, conn, profile_id):
        await self.profile(conn, profile_id)
        rows = await conn.fetch(
            f"SELECT release_id,composition_hash,candidate_id,validation_ref,restored_from,created_at "
            f"FROM {self.schema}.execution_releases WHERE profile_id=$1 ORDER BY created_at DESC LIMIT 100",
            profile_id,
        )
        return [dict(row) for row in rows]

    async def rollback_document(self, conn, profile_id, release_id):
        profile = await self.profile(conn, profile_id, for_update=True)
        target = await self.release(conn, release_id)
        if target["profile_id"] != profile_id:
            raise SkillLibraryError("release belongs to another execution profile")
        document = copy.deepcopy(target["document"])
        current = (await self.release(conn, profile["current_release_id"]))["document"]["execution_plan"]
        current_parts = {part["component_id"]: part for part in current["components"]}
        # Restoration is materialized as new immutable component versions.
        for part in sorted(document["execution_plan"]["components"], key=lambda item: item["component_id"]):
            revision = await conn.fetchval(
                f"SELECT current_revision FROM {self.schema}.execution_assets WHERE component_id=$1 FOR UPDATE",
                part["component_id"],
            )
            changed = part["content_hash"] != current_parts[part["component_id"]]["content_hash"]
            part["revision"] = int(revision) + changed
        return profile, document

    async def record_receipt(self, conn, *, tenant_id, profile_id, user_id, receipt):
        profile = await self.profile(conn, profile_id)
        if profile["user_id"] != user_id:
            raise SkillNotFound("execution profile not found")
        rid = receipt.get("release_id")
        release = await self.release(conn, rid)
        if release["profile_id"] != profile_id or receipt.get("composition_hash") != release["composition_hash"]:
            raise ExecutionVersionConflict("receipt does not match the released composition")
        request_id = receipt.get("request_id")
        if not isinstance(request_id, str) or not 1 <= len(request_id) <= 200:
            raise SkillLibraryError("receipt requires a bounded request_id")
        if receipt.get("state") not in {"downloaded", "activated"}:
            raise SkillLibraryError("receipt state must be downloaded or activated")
        # Agent reports are exposure metadata, not proof that the model consumed the content.
        value = {**receipt, "evidence_level": "agent_reported"}
        await lock_key(conn, tenant_id, f"execution-receipt:{profile_id}:{request_id}")
        old = await conn.fetchval(
            f"SELECT document FROM {self.schema}.execution_receipts WHERE profile_id=$1 AND request_id=$2",
            profile_id, request_id,
        )
        if old and unpack(old) != value:
            raise ExecutionVersionConflict("receipt request_id was reused")
        await conn.execute(
            f"INSERT INTO {self.schema}.execution_receipts"
            "(tenant_id,profile_id,request_id,release_id,user_id,document) VALUES($1,$2,$3,$4,$5,$6::jsonb) "
            "ON CONFLICT DO NOTHING", tenant_id, profile_id, request_id, rid, user_id, canonical(value),
        )
        return value
