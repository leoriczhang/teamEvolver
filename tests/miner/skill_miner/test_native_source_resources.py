"""Lossless source assembly is domain independent and never changes source facts."""

import hashlib
import json

import pytest

from team_miner.skill_miner.native_components import SOURCE_RESOURCE_PROTOCOL, assemble_components
from team_miner.skill_miner.native_round import _reviewed_component_catalog
from team_miner.skill_miner.native_source_resources import frozen_source_resources
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT
from team_miner.skill_miner.ov_round_orchestration import _cache_inputs
from tests.miner.skill_miner.support.native_components import SOURCE, package, save_manifest, snapshot

RAW = '# 原始规则\n\n含 "引号"、\\转义与 | 表格。\n同一来源记为0万／20万，未确认。\n'.encode()


def copied_package(root, name="service-recovery"):
    manifest, dataset = package(root, name)
    manifest["schema_version"] = SOURCE_RESOURCE_PROTOCOL
    row = manifest["references"][0]
    row["copy_source"] = SOURCE
    (root / row["path"]).unlink()
    save_manifest(root, manifest)
    return manifest, dataset


def test_lossless_copy_preserves_quotes_conflicts_and_is_repeatable(tmp_path):
    manifest, expected = copied_package(tmp_path)
    original = snapshot(tmp_path)
    result = assemble_components(tmp_path, {SOURCE}, required=True, frozen_sources={SOURCE: RAW})
    path = manifest["references"][0]["path"]
    assert (tmp_path / path).read_bytes() == RAW
    assert all((tmp_path / name).read_bytes() == content for name, content in original.items())
    assert json.loads((tmp_path / "compiled_skill/service-recovery/benchmark.json").read_text()) == expected
    assert result[0]["source_copies"] == [{"path": path, "source_ref": SOURCE,
                                         "sha256": hashlib.sha256(RAW).hexdigest(), "bytes": len(RAW)}]
    assert result[0]["coverage_is_semantic_proof"] is False
    assert assemble_components(tmp_path, {SOURCE}, required=True, frozen_sources={SOURCE: RAW}) == result


@pytest.mark.parametrize("change,match", [
    ("unknown", "唯一绑定"), ("multiple", "唯一绑定"), ("tampered", "拒绝覆盖"),
    ("legacy", "字段无效"), ("escape", "越界"), ("missing", "唯一绑定"),
])
def test_invalid_source_copy_never_writes_or_repairs_business_content(tmp_path, change, match):
    manifest, _ = copied_package(tmp_path)
    row = manifest["references"][0]
    allowed = {SOURCE}
    if change == "unknown":
        row["copy_source"] = "viking://resources/unfrozen.md"
    elif change == "multiple":
        other = "viking://resources/another.md"
        allowed.add(other)
        method = tmp_path / "compiled_skill/service-recovery/METHOD.json"
        value = json.loads(method.read_text())
        value["source_refs"].append(other)
        method.write_text(json.dumps(value))
        row["source_refs"].append(other)
    elif change == "tampered":
        (tmp_path / row["path"]).write_bytes(RAW.replace(b"0", b"9"))
    elif change == "legacy":
        manifest["schema_version"] = "te-native-components.v1"
    elif change == "escape":
        row["path"] = "../outside.md"
    save_manifest(tmp_path, manifest)
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match=match):
        assemble_components(tmp_path, allowed, frozen_sources={} if change == "missing" else {SOURCE: RAW})
    assert snapshot(tmp_path) == before


def test_invalid_sibling_blocks_all_copies_and_datasets(tmp_path):
    copied_package(tmp_path)
    second, _ = copied_package(tmp_path, "another-method")
    (tmp_path / second["cases"][3]["path"]).unlink()
    before = snapshot(tmp_path)
    with pytest.raises(ValueError, match="缺少文件"):
        assemble_components(tmp_path, {SOURCE}, frozen_sources={SOURCE: RAW})
    assert snapshot(tmp_path) == before


def test_review_catalog_revalidates_copy_without_rewriting_archive(tmp_path):
    root = tmp_path / "job"
    reviewed = root / "rounds/round-1/reviewed_output"
    manifest, _ = copied_package(reviewed)
    assemble_components(reviewed, {SOURCE}, frozen_sources={SOURCE: RAW})
    (reviewed / "analysis").rename(reviewed / "semantic_reports")
    before = snapshot(reviewed)
    catalog = json.loads(_reviewed_component_catalog(root, reviewed, 1, {SOURCE}, required=True,
                                                     frozen_sources={SOURCE: RAW}))
    assert catalog["methods"][0]["manifest"] == manifest
    assert "同一来源" not in json.dumps(catalog, ensure_ascii=False)
    assert snapshot(reviewed) == before
    with pytest.raises(ValueError, match="拒绝覆盖"):
        _reviewed_component_catalog(root, reviewed, 1, {SOURCE}, frozen_sources={SOURCE: b"changed"})
    assert snapshot(reviewed) == before


def freeze(root, number=1, content=None):
    return _cache_inputs(root / f"round_inputs/round-{number}", [
        {"kind": "source", "relative_path": "original/rules.md", "origin_uri": SOURCE,
         "content": RAW.decode() if content is None else content},
        {"kind": "context", "relative_path": "revision/derived.md", "content": "派生内容"},
        {"kind": "human", "relative_path": "supplements/review.md", "content": "用户复核记录"},
    ])


def test_source_resolver_uses_verified_job_snapshot_and_excludes_context(tmp_path):
    freeze(tmp_path)
    result = frozen_source_resources(tmp_path, {"job_id": "job1"}, 1)
    assert result == {SOURCE: RAW, f"{RUNS_ROOT}/job1/rounds/round-1/source/original/rules.md": RAW}
    freeze(tmp_path, 2)
    assert len(frozen_source_resources(tmp_path, {"job_id": "job1"}, 2)) == 3


@pytest.mark.parametrize("change", ["content", "symlink", "version", "missing"])
def test_source_resolver_rejects_changed_or_ambiguous_frozen_inputs(tmp_path, change):
    from pathlib import Path

    entries = freeze(tmp_path)
    path = Path(entries[0]["local_path"])
    through = 1
    if change == "content":
        path.write_text("changed")
    elif change == "symlink":
        other = tmp_path / "outside.md"
        other.write_bytes(RAW)
        path.unlink()
        path.symlink_to(other)
    elif change == "version":
        freeze(tmp_path, 2, "changed")
        through = 2
    else:
        through = 2
    with pytest.raises(ValueError):
        frozen_source_resources(tmp_path, {"job_id": "job1"}, through)


@pytest.mark.asyncio
async def test_lossless_sources_across_two_rounds_keep_one_compile_each_and_remote_baseline(tmp_path):
    from team_miner.skill_miner.native_compiler import COMPONENT_PROTOCOL, SKILL_BODY
    from team_miner.skill_miner.native_retry_baseline import baseline_plan
    from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner
    from tests.miner.skill_miner.support.ov_round_bridge import SOURCE as SOURCE_DIRECTORY

    compiler = SKILL_BODY.replace(f"component_protocol: {COMPONENT_PROTOCOL}",
                                  f"component_protocol: {SOURCE_RESOURCE_PROTOCOL}")
    owner, repo, remote, job = new_owner(tmp_path, compiler=compiler)
    original = "# Original rules\n\n不得改写的原始引文与条件。\n"
    remote.content[SOURCE_DIRECTORY + "/policy.md"] = original
    first = await owner._submit_ov_skill_round(job)

    def native_output(job, question):
        complete(remote, job, question=question)
        uri = job["output_uri"] + "/analysis/components/policy-review/manifest.json"
        manifest = json.loads(remote.content[uri])
        ref = manifest["references"][0]
        ref["copy_source"] = ref["source_refs"][0]
        remote.content[uri] = json.dumps(manifest, ensure_ascii=False)
        del remote.content[job["output_uri"] + "/" + ref["path"]]
        del remote.content[job["output_uri"] + "/compiled_skill/policy-review/benchmark.json"]
        return ref["path"]

    relative = native_output(first, True)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    assert waiting["status"] == "waiting_human"
    assert (repo.workspace(waiting) / relative).read_text() == original
    assert first["output_uri"] + "/" + relative not in remote.content
    second = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
                                    {}, ["region"], {}, "", "source-copy-review", submitted_by="admin")
    plan = baseline_plan(repo.jobs_root / job["job_id"], second)
    assert relative not in plan["files"]  # A local source copy is not fabricated as an OV output.
    second = await owner._submit_ov_skill_round(second)
    assert len(remote.compile_calls) == 2
    assert first["stable_output_uri"] == second["stable_output_uri"]
    native_output(second, False)
    await owner._refresh_ov_skill_job(second)
    done = repo.get_job(job["job_id"])
    assert done["status"] == "succeeded" and len(remote.compile_calls) == 2
    assert (repo.workspace(done) / relative).read_text() == original


@pytest.mark.asyncio
async def test_frozen_v1_component_job_keeps_original_contract_after_v2_upgrade(tmp_path):
    from team_miner.skill_miner.native_compiler import COMPONENT_PROTOCOL, SKILL_BODY
    from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner

    compiler = SKILL_BODY.replace(f"component_protocol: {COMPONENT_PROTOCOL}",
                                  "component_protocol: te-native-components.v1")
    owner, repo, remote, job = new_owner(tmp_path, compiler=compiler)
    submitted = await owner._submit_ov_skill_round(job)
    assert submitted["native_component_protocol"] == "te-native-components.v1"
    complete(remote, submitted)
    await owner._refresh_ov_skill_job(submitted)
    assert repo.get_job(job["job_id"])["status"] == "succeeded"
    assert len(remote.compile_calls) == 1
