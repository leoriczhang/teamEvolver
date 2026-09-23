from __future__ import annotations

import asyncio
import json
from types import SimpleNamespace

from teamEvolver.proxy.routes import _skill_cache_status
from teamEvolver.proxy.uploads import UploadsMixin
from teamEvolver.storage import InMemoryObjectStore
from teamEvolver.tenants import registry
from team_skills.library.bundle import bundle_tree_sha256, write_skill_bundle
from team_skills.library.hub import SkillHub


def test_skill_cache_status_compares_local_bundle_with_manifest(tmp_path) -> None:
    bundle = {
        "SKILL.md": b"---\nname: alpha\ndescription: test\n---\n\nbody\n",
        "scripts/run.py": b"print('ok')\n",
    }
    record = {
        "name": "alpha",
        "version": 3,
        "format": "bundle_v1",
        "tree_sha256": bundle_tree_sha256(bundle),
    }
    bucket = InMemoryObjectStore()
    bucket.put_object("manifest.json", json.dumps(record).encode() + b"\n")
    hub = SkillHub.from_bucket(bucket)
    write_skill_bundle(tmp_path / "alpha", bundle)
    owner = SimpleNamespace(_skills_dir=lambda: str(tmp_path))

    status = _skill_cache_status(owner, hub)

    assert status["manifest_skills"] == 1
    assert status["local_cache_skills"] == 1
    assert status["local_cache_matches_manifest"] is True
    assert status["local_cache_generation"] == status["manifest_generation"]

    (tmp_path / "alpha" / "scripts" / "run.py").write_text(
        "print('changed')\n",
        encoding="utf-8",
    )
    changed = _skill_cache_status(owner, hub)

    assert changed["local_cache_matches_manifest"] is False
    assert changed["local_cache_mismatched"] == ["alpha"]


def test_pg_cache_poll_refreshes_every_tenant(monkeypatch) -> None:
    tenants = [
        registry.TenantContext(tenant_id="default"),
        registry.TenantContext(tenant_id="tenant-a"),
    ]

    class FakeRegistry:
        def __init__(self, _config):
            pass

        def list_tenants(self):
            return tenants

    seen = []

    async def fake_pull():
        seen.append(registry.current_tenant_id())

    owner = SimpleNamespace(
        config=SimpleNamespace(storage_pg_enabled=True),
        _pull_skills_from_cloud=fake_pull,
    )
    monkeypatch.setattr(registry, "TenantRegistry", FakeRegistry)

    asyncio.run(UploadsMixin._pull_all_tenant_skill_caches(owner))

    assert seen == ["default", "tenant-a"]
    assert registry.current_tenant_id() == "default"
