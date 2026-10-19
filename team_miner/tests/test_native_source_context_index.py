"""Compiler upgrades must not migrate accepted packages or their components."""

import hashlib
import json

from team_miner.native_compiler import (
    AUXILIARY_FILES,
    COMPONENT_PROTOCOL,
    SKILL_BODY,
    SOURCE_CONTEXT_PROTOCOL,
    freeze_assets,
    frozen_assets,
)


def test_current_components_and_source_index_do_not_migrate_a_frozen_v1_package(tmp_path):
    # A valid historical package has its own content-addressed attachment set;
    # its absence of source_context_protocol is not permission to install defaults.
    legacy_assets = {
        "references/source-observation.md": "历史抽取规则，保留原受理版本。",
        "references/output-contract.md": "te-native-components.v1；历史组件不包含copy_source。",
    }
    legacy_digests = {name: hashlib.sha256(body.encode()).hexdigest() for name, body in legacy_assets.items()}
    digest = hashlib.sha256(json.dumps(legacy_digests, sort_keys=True).encode()).hexdigest()
    legacy_body = (
        "# 历史编译 Skill\ncomponent_protocol: te-native-components.v1\ncompiler_assets_sha256: " + digest + "\n"
    )
    legacy_root = tmp_path / "legacy"
    asset_root = legacy_root / "compiler-assets"
    for name, body in legacy_assets.items():
        path = asset_root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(body)
    (asset_root / "manifest.json").write_text(json.dumps(legacy_digests))
    before = {path: path.read_bytes() for path in asset_root.rglob("*") if path.is_file()}

    fresh_root = tmp_path / "fresh"
    freeze_assets(fresh_root, SKILL_BODY)
    current = frozen_assets(fresh_root, SKILL_BODY)
    assert SOURCE_CONTEXT_PROTOCOL in current["references/source-observation.md"]
    assert COMPONENT_PROTOCOL == "te-native-components.v2"
    assert f"component_protocol: {COMPONENT_PROTOCOL}" in SKILL_BODY
    assert current == AUXILIARY_FILES
    assert SOURCE_CONTEXT_PROTOCOL not in legacy_body
    assert frozen_assets(legacy_root, legacy_body) == legacy_assets
    assert all(path.read_bytes() == data for path, data in before.items())
