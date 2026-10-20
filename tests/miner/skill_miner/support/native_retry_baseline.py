# ruff: noqa: F401
"""Restore only byte-proven accepted raw artifacts before an unaccepted explicit retry."""

import hashlib
import json

import pytest

from team_miner.skill_miner.native_compiler import COMPONENT_PROTOCOL, NOTES_SCHEMA, SKILL_BODY
from team_miner.skill_miner.native_retry_baseline import baseline_plan, restore_baseline
from tests.miner.skill_miner.support.native_explicit_retry import authorization, hashes
from tests.miner.skill_miner.support.native_mining_rounds import complete, new_owner


def digest(data):
    return hashlib.sha256(data if isinstance(data, bytes) else data.encode("utf-8")).hexdigest()

async def rig(tmp_path, *, components=False, remote_dataset=True):
    # Keep the historical four-file recovery contract independently covered.
    compiler = SKILL_BODY if components else SKILL_BODY.replace(f"component_protocol: {COMPONENT_PROTOCOL}\n", "")
    owner, repo, remote, job = new_owner(tmp_path, compiler=compiler)
    first = await owner._submit_ov_skill_round(job)
    complete(remote, first, question=True)
    if components and not remote_dataset:
        del remote.content[first["output_uri"] + "/compiled_skill/policy-review/benchmark.json"]
    remote.content[first["output_uri"] + "/analysis/notes/source-gap.json"] = json.dumps({
        "schema_version": NOTES_SCHEMA, "deferred": [{"reason": "原文未确定地区，保留未解决事项。",
            "source_refs": [first["original_source_snapshot"] + "/original/policy.md"]}], "questions": []},
        ensure_ascii=False)
    await owner._refresh_ov_skill_job(first)
    waiting = repo.get_job(job["job_id"])
    second = repo.prepare_next_round(job["job_id"], waiting["pending_checkpoint"]["id"],
        {}, ["region"], {}, "", "review-with-unresolved", submitted_by="admin")
    second = await owner._submit_ov_skill_round(second)
    task = {"task_id": second["ov_task_id"], "status": "completed", "error": None,
            "meta": {"pipeline": {"errors": ["map: schema: EmitRecords records.1.inputs Field required"]}}}
    remote.tasks[second["ov_task_id"]] = task
    partial = {}
    for uri in list(remote.content):
        if uri.startswith(second["output_uri"] + "/"):
            remote.content[uri] = "未验收的部分结果：" + remote.content[uri]
            partial[uri[len(second["output_uri"]) + 1:]] = remote.content[uri].encode("utf-8")
    await owner._refresh_ov_skill_job(second)
    failed = repo.get_job(job["job_id"])
    recovered = await owner._resume_native_resource_failure(job["job_id"], authorization(failed))
    root = repo.jobs_root / job["job_id"]
    folder = root / "round_inputs/round-2/native-explicit-retry" / (
        recovered["native_explicit_retry"]["authorization"]["request_id"] + "-baseline")
    writes = []
    original = owner._knowledge_request

    async def transport(account, method, path, **kwargs):
        if method == "POST" and path == "/api/v1/content/write" and kwargs["json"]["mode"] == "replace":
            manifest = json.loads((folder / "manifest.json").read_text())
            assert manifest["partial_sha256"] == {name: digest(data) for name, data in partial.items()}
            assert all((folder / "partial" / name).read_bytes() == data for name, data in partial.items())
            assert kwargs["json"]["wait"] is True
            remote.content[kwargs["json"]["uri"]] = kwargs["json"]["content"]
            writes.append(kwargs["json"]["uri"])
            return {"ok": True}
        if method == "POST" and path == "/api/v1/compile":
            assert (folder / "complete.json").is_file()
            desired = baseline_plan(root, recovered)["files"]
            assert {name: digest(remote.content[recovered["stable_output_uri"] + "/" + name])
                    for name in desired} == desired
        return await original(account, method, path, **kwargs)

    owner._knowledge_request = transport
    return owner, repo, remote, recovered, partial, writes, folder
