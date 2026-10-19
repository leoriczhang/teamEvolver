"""Transfer domain operations over the authoritative Skill repository."""
from team_skills.config import BundleLimits
from team_skills.library.bundle import bundle_tree_sha256
from team_skills.library.repository import SkillLibraryRepository, skill_id_for, validate_bundle

from .channels import MarketplaceAdapter, ZipAdapter
from .git import GitAdapter
from .packages import MAX_CONTENT_BYTES, MAX_FILES, MAX_SKILLS, TransferError, package


class SkillTransferService:
    def __init__(self, tx, adapters=None):
        self.library = SkillLibraryRepository(tx)
        self.schema = tx.pool.schema
        self.adapters = adapters if adapters is not None else {
            "zip": ZipAdapter(), "marketplace": MarketplaceAdapter(), "git": GitAdapter(),
        }

    def adapter(self, channel):
        return self.adapters[channel]

    async def list_skills(self, conn):
        return [{"name": r["name"], "description": r["description"], "version": r["version"],
                 "file_count": len(r["files"])} for r in await self.library.list_skills(conn)]

    @staticmethod
    def check_size(packages):
        if (not 1 <= len(packages) <= MAX_SKILLS or sum(len(p.files) for p in packages) > MAX_FILES
                or sum(len(v) for p in packages for v in p.files.values()) > MAX_CONTENT_BYTES):
            raise TransferError("Skill 数量或大小超出限制", 413)

    async def import_skills(self, conn, *, tenant_id, command_id, payload, packages):
        self.check_size(packages)
        if len({p.name for p in packages}) != len(packages):
            raise TransferError("导入包含重复 Skill 名称")
        limits = BundleLimits(**payload["limits"])
        packages = sorted([package(p.name, validate_bundle(p.name, p.files, limits), p.origin)
                           for p in packages], key=lambda p: p.name)
        # Lock existing AND absent heads in the same order as normal publishers.
        # Conflicts and every version/outbox/result commit share this transaction.
        heads = {}
        for item in packages:
            await conn.execute(
                f"INSERT INTO {self.schema}.skills (tenant_id,name,skill_id) VALUES ($1,$2,$3) "
                "ON CONFLICT (tenant_id,name) DO NOTHING", tenant_id, item.name, skill_id_for(item.name),
            )
            heads[item.name] = await conn.fetchrow(
                f"SELECT current_version,deleted FROM {self.schema}.skills WHERE name=$1 FOR UPDATE", item.name,
            )
        existing = {name for name, head in heads.items() if head["current_version"] and not head["deleted"]}
        conflict = payload.get("conflict", "replace")
        if conflict == "error" and existing:
            raise TransferError("存在同名 Skill；请选择替换或跳过", 409)
        result = {"channel": payload["channel"], "imported": [], "skipped": [], "errors": []}
        for item in packages:
            if item.name in existing and conflict == "skip":
                result["skipped"].append({"name": item.name, "reason": "exists"})
                continue
            commit = await self.library.commit_bundle(
                conn, tenant_id=tenant_id, name=item.name, bundle=item.files,
                mutation_id=f"{command_id}:{item.name}", limits=limits, actor=payload["actor"],
                source="transfer", metadata={"channel": payload["channel"], "origin": item.origin},
            )
            result["imported"].append({
                "name": item.name, "version": commit.version,
                "status": "unchanged" if commit.status == "unchanged" else
                          "updated" if item.name in existing else "created",
                "files": sorted(item.files), "tree_sha256": bundle_tree_sha256(item.files), "origin": item.origin,
            })
        return result

    async def export_packages(self, conn, payload):
        packages = []
        for version in payload["versions"]:
            files = await self.library.read_version_bundle(conn, version["name"], version["version"])
            if bundle_tree_sha256(files) != version["tree_sha256"]:
                raise TransferError("Skill 版本 hash 校验失败", 409)
            packages.append(package(version["name"], files))
            self.check_size(packages)
        return packages
