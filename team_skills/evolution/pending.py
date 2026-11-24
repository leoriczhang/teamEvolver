"""Frozen pending Candidate catalog and safe Bundle consolidation."""
from __future__ import annotations

import hashlib

from team_skills.candidates.repository import CandidateStateError
from team_skills.config import SkillsModuleConfig
from team_skills.evolution.history import evolution_feedback, skill_payload
from team_skills.evolution.kernel.bundle_changes import select_editable_files


def scopes(provenance):
    return sorted({
        (str(item.get("connection_id") or ""), str(item.get("entry_agent_id") or ""))
        for item in provenance if isinstance(item, dict) and item.get("connection_id")
    })


class PendingCandidates:
    def __init__(self, repo, tx, tenant_id, items, config=None):
        self.repo, self.tx, self.tenant_id = repo, tx, tenant_id
        self.config = config or SkillsModuleConfig(0, {})
        self.items = {item["candidate_id"]: item for item in items}
        self.bundles = {}

    @classmethod
    async def load(cls, repo, tx, tenant_id, config=None):
        items = []
        async with tx.tenant_tx(tenant_id) as conn:
            while True:
                page, total = await repo.list(conn, scope="open", limit=200, offset=len(items), compact=True)
                items.extend(page)
                if len(items) >= total or not page:
                    break
        return cls(repo, tx, tenant_id, items, config)

    def cards(self):
        return [{
            **{key: item[key] for key in ("candidate_id", "skill_name", "revision", "status", "base_version")},
            "scope": scopes(item["detail"].get("execution_provenance") or []),
            "rationale": str(item["detail"].get("rationale") or "")[:1200],
            "feedback": evolution_feedback(item.get("evaluation") or {}),
        } for item in self.items.values()]

    async def bundle(self, candidate_id):
        if candidate_id not in self.items:
            raise CandidateStateError(
                f"candidate is outside the frozen tenant catalog; use an exact candidate_id: {list(self.items)}"
            )
        expected = self.items[candidate_id]
        if candidate_id not in self.bundles:
            async with self.tx.tenant_tx(self.tenant_id) as conn:
                actual = await self.repo.require(conn, candidate_id)
                if any(actual[key] != expected[key] for key in ("revision", "tree_sha256", "status")):
                    raise CandidateStateError("candidate changed after catalog snapshot")
                self.bundles[candidate_id] = await self.repo.read_bundle(conn, candidate_id)
        return dict(self.bundles[candidate_id])

    async def read(self, candidate_id):
        bundle = await self.bundle(candidate_id)
        item = self.items[candidate_id]
        return {
            "candidate_id": candidate_id, "revision": item["revision"], "status": item["status"],
            "skill": skill_payload(item["skill_name"], bundle, include_bundle=False),
            "editable_files": select_editable_files(
                bundle, extensions=self.config.get("bundle_text_extensions"),
                max_file_bytes=self.config.get("bundle_edit_max_bytes"),
                max_prompt_bytes=self.config.get("bundle_prompt_max_bytes"),
            ),
            "files": [{"path": path, "bytes": len(data)} for path, data in bundle.items()],
        }

    async def prepare(self, result, *, context, config, current):
        target_id = result["target_candidate_id"]
        source_ids = result.get("source_candidate_ids") or []
        ids = [target_id, *source_ids]
        if len(set(ids)) != len(ids) or any(ident not in self.items for ident in ids):
            raise CandidateStateError("merge requires distinct candidates from the frozen catalog")
        items = [self.items[ident] for ident in ids]
        target = items[0]
        if current and current["name"] != target["skill_name"]:
            raise CandidateStateError("evolve may only merge into its current Skill")
        expected_scope = scopes(context.get("execution_provenance") or [])
        for item in items:
            if item["status"] != "open":
                raise CandidateStateError("selected candidate is not open")
            if scopes(item["detail"].get("execution_provenance") or []) != expected_scope:
                raise CandidateStateError("candidate belongs to a different source Agent scope")
        if any(item["base_version"] != 0 for item in items[1:]):
            raise CandidateStateError("cannot retire a Candidate for a different published Skill")
        bundle = await self.bundle(target_id)
        sources, conflicts = [], set()
        for item in items[1:]:
            other = await self.bundle(item["candidate_id"])
            sources.append({**await self.read(item["candidate_id"]),
                            "editable_files": select_editable_files(
                                other, extensions=config.get("bundle_text_extensions"),
                                max_file_bytes=config.get("bundle_edit_max_bytes"),
                                max_prompt_bytes=config.get("bundle_prompt_max_bytes"))})
            for path, data in other.items():
                if path == "SKILL.md":
                    continue
                if path in bundle and bundle[path] != data:
                    conflicts.add(path)
                else:
                    bundle[path] = data
        snapshots = [{key: item[key] for key in (
            "candidate_id", "skill_name", "revision", "base_version", "tree_sha256",
        )} for item in items]
        for snapshot in snapshots:
            snapshot["files"] = [
                {"path": path, "sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}
                for path, data in (await self.bundle(snapshot["candidate_id"])).items()
            ]
        return target, bundle, sources, conflicts, snapshots
