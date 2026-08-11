from __future__ import annotations

from pathlib import Path

from teamEvolver.evolve import EvolveServer, EvolveServerConfig
from teamEvolver.evolve.store.object_store import (
    fetch_skill_bundle,
    fetch_version_bundle,
    load_manifest,
)
from teamEvolver.skills.bundle import attach_bundle_payload, bundle_tree_sha256


def _server(tmp_path: Path) -> EvolveServer:
    return EvolveServer(
        EvolveServerConfig(
            storage_backend="local",
            local_root=str(tmp_path),
            llm_api_key="",
            publish_mode="direct",
        )
    )


def _skill(body: str, files: dict[str, bytes]) -> dict[str, object]:
    return attach_bundle_payload(
        {
            "name": "bundle-demo",
            "description": "Bundle demo",
            "category": "general",
            "content": body,
        },
        {"SKILL.md": b"stale", **files},
    )


def test_publish_versions_and_rollback_preserve_complete_bundle(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    first = _skill(
        "# V1",
        {
            "scripts/run.py": b"print('v1')\n",
            "assets/data.bin": b"\x00\xff",
        },
    )
    assert server._upload_skill(first, "create_skill") == "uploaded"

    second = _skill(
        "# V2",
        {
            "scripts/run.py": b"print('v2')\n",
            "scripts/check.sh": b"echo ok\n",
            "assets/data.bin": b"\x00\xff",
        },
    )
    assert server._upload_skill(second, "improve_skill") == "uploaded"

    active = fetch_skill_bundle(
        server._skill_bucket,
        server._skill_prefix,
        "bundle-demo",
        load_manifest(server._skill_bucket, server._skill_prefix)["bundle-demo"],
    )
    archived_v1 = fetch_version_bundle(
        server._skill_bucket,
        server._skill_prefix,
        "bundle-demo",
        1,
    )
    assert active["scripts/run.py"] == b"print('v2')\n"
    assert active["scripts/check.sh"] == b"echo ok\n"
    assert archived_v1["scripts/run.py"] == b"print('v1')\n"
    assert archived_v1["assets/data.bin"] == b"\x00\xff"

    result = server._rollback_skill("bundle-demo", 1)
    restored_manifest = load_manifest(
        server._skill_bucket,
        server._skill_prefix,
    )["bundle-demo"]
    restored = fetch_skill_bundle(
        server._skill_bucket,
        server._skill_prefix,
        "bundle-demo",
        restored_manifest,
    )

    assert result["status"] == "rolled_back"
    assert result["new_version"] == 3
    assert restored["scripts/run.py"] == b"print('v1')\n"
    assert "scripts/check.sh" not in restored
    assert restored_manifest["tree_sha256"] == bundle_tree_sha256(restored)
