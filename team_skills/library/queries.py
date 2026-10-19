"""Public read-only immutable Skill treatment for Dataset and Replay producers."""
from __future__ import annotations

from .bundle import bundle_tree_sha256, encode_bundle_payload
from .repository import SkillLibraryRepository


async def pinned_treatment(conn, tx, refs):
    library = SkillLibraryRepository(tx)
    members, versions = [], {}
    for ref in refs:
        name = ref["skill_id"]
        head, bundle = await library.read_current_bundle(conn, name)
        version = ref.get("revision", head["version"])
        if type(version) is not int or version < 1:
            raise ValueError("Skill revision must be a positive integer")
        if version != head["version"]:
            bundle = await library.read_version_bundle(conn, name, version)
        tree = bundle_tree_sha256(bundle)
        if ref.get("content_hash") and ref["content_hash"] != tree:
            raise ValueError("Skill content hash does not match its version")
        members.append({"name": name, "bundle": encode_bundle_payload(bundle)})
        versions[name] = {"revision": version, "content_hash": tree}
    return {"treatment": {"kind": "skill_set", "skills": members} if members else None, "versions": versions}
