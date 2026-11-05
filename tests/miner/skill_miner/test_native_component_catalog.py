"""Incremental component context retains accepted ownership without inventing Evidence."""

import hashlib
import json
from pathlib import Path

import pytest

from team_miner.skill_miner.native_round import _reviewed_component_catalog
from tests.miner.skill_miner.support.native_components import SOURCE, package, snapshot, write_json


def reviewed_package(tmp_path):
    root = tmp_path / "job"
    reviewed = root / "rounds/round-1/reviewed_output"
    reviewed.mkdir(parents=True)
    manifest, dataset = package(reviewed)
    (reviewed / "analysis").rename(reviewed / "semantic_reports")
    return root, reviewed, manifest, dataset


def test_component_catalog_has_complete_manifest_real_hashes_and_no_generated_bodies(tmp_path):
    root, reviewed, manifest, _ = reviewed_package(tmp_path)
    before = snapshot(reviewed)
    catalog = json.loads(_reviewed_component_catalog(root, reviewed, 1, {SOURCE}, required=True))
    assert catalog["schema_version"] == "te-native-component-catalog.v1"
    assert catalog["round"] == 1 and catalog["is_evidence"] is False
    assert catalog["requires_source_review"] is True
    method = catalog["methods"][0]
    assert method["manifest"] == manifest
    assert method["manifest_path"] == "analysis/components/service-recovery/manifest.json"
    expected = {path.replace("semantic_reports/", "analysis/", 1): body for path, body in before.items()}
    assert {entry["path"] for entry in method["files"]} == set(expected)
    for entry in method["files"]:
        assert entry == {"path": entry["path"], "bytes": len(expected[entry["path"]]),
                         "sha256": hashlib.sha256(expected[entry["path"]]).hexdigest()}
    assert "诊断第" not in json.dumps(catalog, ensure_ascii=False)
    assert snapshot(reviewed) == before


def test_local_derived_dataset_and_prior_audit_do_not_become_remote_components(tmp_path):
    root, reviewed, _, dataset = reviewed_package(tmp_path)
    dataset["cases"][0]["query"] = "人工要求核对的新场景，仍由revision差异传递。"
    write_json(reviewed / "compiled_skill/service-recovery/benchmark.json", dataset)
    write_json(reviewed / "semantic_reports/components/service-recovery/assembly-audit.json", {"old": "audit"})
    (reviewed / "compiled_skill/service-recovery/BENCHMARK.md").write_text("人工编辑的展示文本。")
    before = snapshot(reviewed)
    catalog = json.loads(_reviewed_component_catalog(root, reviewed, 1, {SOURCE}, required=True))
    paths = {entry["path"] for entry in catalog["methods"][0]["files"]}
    assert not any(path.endswith(("benchmark.json", "BENCHMARK.md", "assembly-audit.json")) for path in paths)
    assert "人工要求核对的新场景" not in json.dumps(catalog, ensure_ascii=False)
    assert snapshot(reviewed) == before


def test_old_four_file_history_without_components_remains_compatible(tmp_path):
    root = tmp_path / "job"
    reviewed = root / "rounds/round-1/reviewed_output"
    write_json(reviewed / "compiled_skill/legacy/METHOD.json", {"name": "legacy"})
    assert _reviewed_component_catalog(root, reviewed, 1, {SOURCE}) is None
    with pytest.raises(ValueError, match="缺少完整组件清单"):
        _reviewed_component_catalog(root, reviewed, 1, {SOURCE}, required=True)


@pytest.mark.parametrize("change,match", [
    ("missing_case", "缺少文件"),
    ("foreign_source", "未知来源"),
    ("undeclared_reference", "未声明"),
    ("symlink", "符号链接"),
])
def test_invalid_reviewed_components_fail_without_modifying_history(tmp_path, change, match):
    root, reviewed, manifest, _ = reviewed_package(tmp_path)
    if change == "missing_case":
        (reviewed / manifest["cases"][0]["path"].replace("analysis/", "semantic_reports/", 1)).unlink()
    elif change == "undeclared_reference":
        (reviewed / "compiled_skill/service-recovery/references/extra.md").write_text("未声明的资源。")
    elif change == "symlink":
        path = reviewed / manifest["references"][0]["path"]
        other = tmp_path / "outside.md"
        other.write_bytes(path.read_bytes())
        path.unlink()
        path.symlink_to(other)
    allowed = {"viking://resources/unknown.md"} if change == "foreign_source" else {SOURCE}
    before = snapshot(reviewed)
    with pytest.raises(ValueError, match=match):
        _reviewed_component_catalog(root, reviewed, 1, allowed, required=True)
    assert snapshot(reviewed) == before


def test_new_component_round_rejects_partial_old_catalog(tmp_path):
    root, reviewed, _, _ = reviewed_package(tmp_path)
    write_json(reviewed / "compiled_skill/another-method/METHOD.json", {"name": "another-method"})
    with pytest.raises(ValueError, match="未覆盖全部当前方法"):
        _reviewed_component_catalog(root, reviewed, 1, {SOURCE}, required=True)


@pytest.mark.asyncio
async def test_incremental_inputs_freeze_component_catalog_as_context_and_reuse_it(tmp_path):
    from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner

    owner, repo, remote, job = new_owner(tmp_path)
    first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    next_round = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
                                        {}, ["region"], {}, "", "component-review", submitted_by="admin")
    entries = await owner._native_inputs(next_round)
    entry = next(row for row in entries if row["relative_path"] == "revision/component-catalog.json")
    catalog_file = Path(entry["local_path"])
    before = catalog_file.read_bytes()
    catalog = json.loads(before)
    assert entry["kind"] == "context" and catalog["is_evidence"] is False
    assert len(catalog["methods"][0]["manifest"]["cases"]) == 12
    assert len(remote.compile_calls) == 1
    reviewed = repo.jobs_root / job["job_id"] / "rounds/round-1/reviewed_output"
    (reviewed / "compiled_skill/policy-review/SKILL.md").write_text("后来变化不改变已冻结输入。")
    assert await owner._native_inputs(next_round) == entries
    assert catalog_file.read_bytes() == before


@pytest.mark.asyncio
async def test_incremental_inputs_reject_missing_required_manifest_before_submission(tmp_path):
    from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner

    owner, repo, remote, job = new_owner(tmp_path)
    first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    next_round = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
                                        {}, ["region"], {}, "", "missing-review", submitted_by="admin")
    reviewed = repo.jobs_root / job["job_id"] / "rounds/round-1/reviewed_output"
    (reviewed / "semantic_reports/components/policy-review/manifest.json").unlink()
    with pytest.raises(ValueError, match="缺少完整组件清单"):
        await owner._native_inputs(next_round)
    assert len(remote.compile_calls) == 1
