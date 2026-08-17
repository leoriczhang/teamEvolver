from __future__ import annotations

import hashlib
import json
from pathlib import Path

from teamEvolver.evolve import EvolveServer, EvolveServerConfig
from teamEvolver.evolve.store.object_store import (
    fetch_skill_bundle,
    fetch_version_bundle,
    load_manifest,
)
from teamEvolver.skills.bundle import attach_bundle_payload, bundle_tree_sha256
from teamEvolver.storage import InMemoryObjectStore


class _NativeBatchStore:
    native_batch_write = True

    def __init__(self) -> None:
        self.inner = InMemoryObjectStore()
        self.batch_calls: list[dict] = []
        self.fail_batch = False

    def __getattr__(self, name):
        return getattr(self.inner, name)

    def object_precondition(self, key: str) -> dict[str, str]:
        try:
            current = self.inner.get_object(key).read()
        except FileNotFoundError:
            return {"kind": "create_if_absent"}
        return {
            "kind": "replace_if_hash",
            "base_hash": "sha256:" + hashlib.sha256(current).hexdigest(),
        }

    def batch_write(self, objects, *, preconditions, **kwargs):
        self.batch_calls.append(
            {
                "objects": dict(objects),
                "preconditions": dict(preconditions),
                **kwargs,
            }
        )
        if self.fail_batch:
            raise RuntimeError("CONFLICT: injected batch conflict")
        for key, expected in preconditions.items():
            actual = self.object_precondition(key)
            if actual != expected:
                try:
                    desired = objects[key]
                    desired_bytes = (
                        desired.encode("utf-8")
                        if isinstance(desired, str)
                        else bytes(desired)
                    )
                    current = self.inner.get_object(key).read()
                except FileNotFoundError:
                    current = None
                if current != desired_bytes:
                    raise RuntimeError("CONFLICT: precondition failed")
        for key, value in objects.items():
            self.inner.put_object(key, value)
        return {
            "created": list(objects),
            "updated": [],
            "unchanged": [],
            "queue_status": {"Semantic": {"error_count": 0}},
        }


def _server(tmp_path: Path) -> EvolveServer:
    return EvolveServer(
        EvolveServerConfig(
            llm_api_key="",
            publish_mode="direct",
        ),
        mock=True,
        mock_root=str(tmp_path),
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


def test_identical_publish_reuses_committed_version(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    skill = _skill(
        "# Stable",
        {"scripts/run.py": b"print('stable')\n"},
    )

    assert server._upload_skill(skill, "create_skill") == "uploaded"
    assert (
        server._upload_skill(skill, "create_skill")
        == "uploaded_idempotent"
    )

    manifest = load_manifest(
        server._skill_bucket,
        server._skill_prefix,
    )
    assert manifest["bundle-demo"]["version"] == 1
    assert server._id_registry.get_version("bundle-demo") == 1


def test_native_openviking_publish_batches_bundle_manifest_and_registry(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    bucket = _NativeBatchStore()
    server._skill_bucket = bucket
    skill = _skill(
        "# V1",
        {
            "scripts/run.py": b"print('v1')\n",
            "assets/data.bin": b"\x00\xff",
        },
    )

    assert server._upload_skill(skill, "create_skill") == "uploaded"

    assert len(bucket.batch_calls) == 1
    written = set(bucket.batch_calls[0]["objects"])
    assert {
        "manifest.json",
        "evolve_skill_registry.json",
        "skills/bundle-demo/SKILL.md",
        "skills/bundle-demo/files/scripts/run.py",
        "skills/bundle-demo/files/assets/data.bin",
        "skills/bundle-demo/versions/v1/SKILL.md",
        "skills/bundle-demo/versions/v1/files/scripts/run.py",
        "skills/bundle-demo/versions/v1/files/assets/data.bin",
        "skills/bundle-demo/versions/v1/bundle.json",
    } == written
    assert bucket.batch_calls[0]["wait"] is True
    assert bucket.batch_calls[0]["telemetry"] is True
    assert server._id_registry.get_version("bundle-demo") == 1
    assert server._id_registry.dirty is False

    server._id_registry.save_to_oss(bucket, server._skill_prefix)
    assert len(bucket.batch_calls) == 1


def test_native_openviking_publish_restores_registry_after_conflict(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    bucket = _NativeBatchStore()
    server._skill_bucket = bucket
    first = _skill("# V1", {"scripts/run.py": b"print('v1')\n"})
    second = _skill("# V2", {"scripts/run.py": b"print('v2')\n"})
    assert server._upload_skill(first, "create_skill") == "uploaded"
    bucket.fail_batch = True

    try:
        server._upload_skill(second, "improve_skill")
    except RuntimeError as exc:
        assert "CONFLICT" in str(exc)
    else:
        raise AssertionError("batch conflict must fail the publish")

    assert server._id_registry.get_version("bundle-demo") == 1
    assert server._id_registry.dirty is False
    manifest = load_manifest(bucket, "")
    assert manifest["bundle-demo"]["version"] == 1


def test_native_openviking_rollback_uses_one_monotonic_batch(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    bucket = _NativeBatchStore()
    server._skill_bucket = bucket
    first = _skill("# V1", {"scripts/run.py": b"print('v1')\n"})
    second = _skill("# V2", {"scripts/run.py": b"print('v2')\n"})
    assert server._upload_skill(first, "create_skill") == "uploaded"
    assert server._upload_skill(second, "improve_skill") == "uploaded"

    result = server._rollback_skill("bundle-demo", 1)

    assert result["status"] == "rolled_back"
    assert result["new_version"] == 3
    assert len(bucket.batch_calls) == 3
    rollback_objects = bucket.batch_calls[-1]["objects"]
    assert "skills/bundle-demo/versions/v3/SKILL.md" in rollback_objects
    assert load_manifest(bucket, "")["bundle-demo"]["version"] == 3
    assert server._id_registry.get_version("bundle-demo") == 3
    assert server._id_registry.dirty is False


def test_native_registry_save_uses_cas_from_the_merged_payload(
    tmp_path: Path,
) -> None:
    server = _server(tmp_path)
    bucket = _NativeBatchStore()
    server._skill_bucket = bucket
    server._id_registry.get_or_create("local-pending")
    remote_registry = json.dumps(
        {
            "external-seed": {
                "skill_id": "seed-id",
                "version": 4,
                "content_sha": "seed-sha",
                "history": [],
            }
        }
    ).encode("utf-8")
    bucket.put_object("evolve_skill_registry.json", remote_registry)

    server._id_registry.save_to_oss(bucket, "")

    assert len(bucket.batch_calls) == 1
    call = bucket.batch_calls[0]
    assert call["preconditions"]["evolve_skill_registry.json"] == {
        "kind": "replace_if_hash",
        "base_hash": "sha256:" + hashlib.sha256(remote_registry).hexdigest(),
    }
    persisted = json.loads(
        bucket.get_object("evolve_skill_registry.json").read()
    )
    assert set(persisted) == {"external-seed", "local-pending"}
