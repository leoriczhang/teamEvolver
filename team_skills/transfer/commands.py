"""Transfer acceptance, frozen export versions and durable result receipts."""
import hashlib
from dataclasses import asdict

from team_skills.evolution.repository import canonical, digest, lock_key, unpack
from team_skills.library.repository import SkillLibraryRepository, SkillMutationConflict, SkillNotFound
from teamEvolver.security.secrets import secret_fingerprint

from .validation import validate_request


class TransferCommands:
    def __init__(self, services):
        self.services = services
        self.schema = services.tx.pool.schema

    async def get(self, conn, command_id):
        row = await conn.fetchrow(
            f"SELECT command_id,operation,request_hash,payload,payload_hash,checkpoint,result "
            f"FROM {self.schema}.skill_transfers WHERE command_id=$1", command_id,
        )
        return {**dict(row), **{k: unpack(row[k]) for k in ("payload", "checkpoint", "result")}} if row else None

    async def accept(self, conn, *, tenant_id, command_id, operation, body, config, actor=""):
        body = validate_request(operation, body)
        options = dict(body["options"])
        token = options.pop("token", "")
        request = {**body, "options": {**options, "credential_hash": secret_fingerprint(token) if token else ""}}
        await lock_key(conn, tenant_id, f"transfer:{command_id}")
        existing = await self.get(conn, command_id)
        if existing:
            if existing["request_hash"] != digest(request) or existing["operation"] != operation:
                raise SkillMutationConflict("Idempotency-Key was used for a different transfer")
            return existing
        if token:
            secrets = self.services.extras.get("secret_repo")
            if secrets is None:
                from .packages import TransferError
                raise TransferError("凭证加密服务未配置", 503)
            ref = await secrets.store(conn, tenant_id=tenant_id, plaintext=token, purpose="skill-transfer")
            options["secret_ref"] = ref.ref
        payload = {**body, "options": options, "actor": actor, "limits": asdict(config.bundle_limits)}
        if operation == "export":
            lib = SkillLibraryRepository(self.services.tx)
            versions = []
            for name in sorted(body["names"]):
                head = await lib.get_skill(conn, name)
                if not head:
                    raise SkillNotFound(f"Skill not found: {name}")
                versions.append({"name": name, "version": head["version"], "tree_sha256": head["tree_sha256"]})
            payload["versions"] = versions
        if body["channel"] == "git" and operation == "export" and not options.get("new_branch"):
            options["new_branch"] = ("main" if options.get("mode") == "new_repository"
                                     else "skills-export-" + digest(command_id)[:20])
        await conn.execute(
            f"INSERT INTO {self.schema}.skill_transfers "
            "(tenant_id,command_id,operation,request_hash,payload,payload_hash) VALUES ($1,$2,$3,$4,$5::jsonb,$6)",
            tenant_id, command_id, operation, digest(request), canonical(payload), digest(payload),
        )
        await self.services.tasks.enqueue(
            conn, tenant_id=tenant_id, task_id=command_id, kind="skill.transfer",
            subject_type="skill_transfer", subject_id=command_id, schema_version="1",
            dedupe_key=command_id, payload_ref=command_id, payload_hash=digest(payload),
            config_revision=config.revision,
        )
        return await self.get(conn, command_id)

    async def checkpoint(self, conn, command_id, checkpoint):
        await conn.execute(
            f"UPDATE {self.schema}.skill_transfers SET checkpoint=$2::jsonb WHERE command_id=$1",
            command_id, canonical(checkpoint),
        )

    async def finish(self, conn, command_id, result, archive=None):
        if archive is not None:
            result = {**result, "download_url": f"/api/skills/transfer/jobs/{command_id}/download",
                      "sha256": hashlib.sha256(archive).hexdigest()}
        await conn.execute(
            f"UPDATE {self.schema}.skill_transfers SET result=$2::jsonb,archive=$3,archive_hash=$4,finished_at=now() "
            "WHERE command_id=$1 AND result IS NULL", command_id, canonical(result), archive,
            hashlib.sha256(archive).hexdigest() if archive is not None else None,
        )

    async def archive(self, conn, command_id):
        return await conn.fetchrow(
            f"SELECT archive,archive_hash FROM {self.schema}.skill_transfers "
            "WHERE command_id=$1 AND result IS NOT NULL AND archive IS NOT NULL", command_id,
        )
