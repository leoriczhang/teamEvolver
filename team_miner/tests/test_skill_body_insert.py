"""Long integrated drafts need bounded deltas, not repeated whole-body repairs."""

import asyncio
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.artifact_recovery import prepare_recovery
from team_miner.ov_artifact_batch_orchestration import _ledger, _verify
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_body import PROTOCOL as INTEGRATED_PROTOCOL
from team_miner.skill_body import iter_integrated_skill, previous_body
from team_miner.skill_body_insert import PROTOCOL, SCHEMA, body_sha, project, validate
from team_miner.tests.test_artifact_batches import ArtifactOwner
from team_miner.tests.test_ov_staged_orchestration import drive, start

POLICY = {"language": "zh-CN"}


def delta(previous, ids):
    if "额外四十分钟须再次征得同意。" in previous["content"]:
        return {"schema_version": SCHEMA, "base_body_sha256": body_sha(previous), "input_ids": ids,
                "title": previous["title"], "insert": [], "no_change_reason": "本批复核未增加新的执行条件。"}
    return {"schema_version": SCHEMA, "base_body_sha256": body_sha(previous), "input_ids": ids,
            "title": previous["title"], "insert": [{"anchor_id": "s0000", "text": "额外四十分钟须再次征得同意。",
                                                    "support_ids": ids}], "no_change_reason": ""}


def test_large_projection_preserves_every_old_byte_and_checks_the_whole_chain(tmp_path):
    original = {"schema_version": planning.SECTION_SCHEMA, "input_ids": ["old"], "title": "沟通方法",
                "content": "# 沟通方法\n先征得同意，再开始二十分钟交流。\n" + "保留先前限定条件。" * 1108}
    _save(tmp_path / "sections/method/00000000.json", original)
    previous = previous_body(tmp_path, "method", 1)
    patch = delta(previous, ["current"])
    value = project(patch, previous, ["current"], POLICY)
    assert len(value["content"]) > 10000 and len(json.dumps(patch, ensure_ascii=False)) < 1000
    assert value["content"].startswith(original["content"])
    _save(tmp_path / "sections/method/00000001.json", value)
    assert previous_body(tmp_path, "method", 2)["content"] == value["content"]
    rendered = "".join(iter_integrated_skill({"name": "method", "task": "沟通方法"}, [original, value],
                      expected_input_ids=["old", "current"], source_refs=["viking://resources/source"]))
    assert rendered.count("先征得同意") == 1 and "额外四十分钟须再次征得同意" in rendered
    value["content"] = value["content"].replace("先征得同意", "直接开始")
    _save(tmp_path / "sections/method/00000001.json", value)
    with pytest.raises(ValueError, match="projection changed"):
        previous_body(tmp_path, "method", 2)


@pytest.mark.parametrize("change", ["source", "title", "hash", "anchor", "english", "duplicate", "delete", "oversize"])
def test_insert_rejects_changed_scope_old_conditions_language_and_capacity(change):
    previous = {"title": "沟通方法", "content": "# 沟通方法\n先征得同意。"}
    patch = delta(previous, ["current"])
    if change == "source":
        patch["insert"][0]["support_ids"] = ["other-customer"]
    elif change == "title":
        patch["title"] = "另一方法"
    elif change == "hash":
        patch["base_body_sha256"] = "0" * 64
    elif change == "anchor":
        patch["insert"][0]["anchor_id"] = "missing"
    elif change == "english":
        patch["insert"][0]["text"] = "## Consent\nRequest permission."
    elif change == "duplicate":
        patch["insert"][0]["text"] = "先征得同意。"
    elif change == "delete":
        patch["delete"] = ["s0000"]
    else:
        patch["insert"][0]["text"] = "限制" * 7000
    with pytest.raises(ValueError):
        validate(json.dumps(patch, ensure_ascii=False), previous, ["current"], POLICY)


def test_actual_capacity_failure_recovers_once_without_rewriting_frozen_work(tmp_path):
    async def check():
        owner = ArtifactOwner(tmp_path)
        owner.ov.candidate_count = 2
        original_new = owner.new

        def fresh(count=1):
            job, entries = original_new(count)
            return owner.repo.update_job(job["job_id"], staged_skill_body_protocol=INTEGRATED_PROTOCOL), entries

        owner.new = fresh
        original_request = owner.ov.request

        async def patched_request(account, method, path, **kwargs):
            result = await original_request(account, method, path, **kwargs)
            request = kwargs.get("json", {})
            if method == "POST" and path == "/api/v1/compile" and "/stages/artifact_batch-" in request["from"][0]:
                prefix = request["from"][0] + "/"
                for uri in sorted(owner.ov.contents):
                    if not uri.startswith(prefix):
                        continue
                    p = json.loads(owner.ov.contents[uri])
                    if "body_insert" in p["derived_controls"]:
                        prior = p["derived_controls"]["previous_body"]
                        owner.ov.contents[request["to"] + "/" + p["output_path"]] = json.dumps(
                            delta(prior, p["input_ids"]), ensure_ascii=False)
            return result

        owner.ov.request = patched_request
        job = await start(owner, 24)
        while (await owner._staged_load(job))[1].get("current", {}).get("stage") != "artifact_batch":
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        row = _read(root / state["current"]["packet_file"])[0]
        packet = _read(root / row["local_path"])
        name = row["context"]["skill_name"]
        first = {"schema_version": planning.SECTION_SCHEMA, "input_ids": packet["input_ids"],
                 "title": "沟通方法", "content": "# 沟通方法\n先征得同意。\n" + "需保留原文限定条件。" * 980}
        owner.ov.contents[state["current"]["output_uri"] + "/" + row["output_path"]] = json.dumps(
            first, ensure_ascii=False)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        for _ in range(3):
            root, state = await owner._staged_load(job)
            current = state["current"]
            row = next(r for r in _read(root / current["packet_file"]) if r["context"]["skill_name"] == name)
            bad = {**first, "input_ids": row["context"]["input_ids"], "content": first["content"] + "新条件" * 1200}
            owner.ov.contents[current["output_uri"] + "/" + row["output_path"]] = json.dumps(bad, ensure_ascii=False)
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "failed" and _ledger(root, name)["format_attempts"] == 3
        assert (root / current["compiler_file"]).exists()
        assert len((root / "children" / current["child"] / (row["output_path"] + ".raw")).read_text()) > 12000
        job = owner.repo.update_job(job["job_id"], ov_status="completed")
        saved = {p: p.read_bytes() for p in root.rglob("*") if p.is_file() and (
            "children" in p.parts or "sections" in p.parts or "outcomes" in p.parts)}
        job = owner.repo.resume_identity_failure(job["job_id"])
        assert job["status"] == "preparing"
        root, state = await owner._staged_load(job)
        assert state["body_insert_protocol"] == PROTOCOL and state["current"] is None
        journal = _read(root / "recovery/body-insert-v1.json")
        assert journal["failed_ledgers"][name]["format_attempts"] == 3
        assert all(p.read_bytes() == data for p, data in saved.items())
        job = await owner._staged_artifact_batch_next(job, root, state)
        root, state = await owner._staged_load(job)
        _verify(root, state["current"])
        row = next(r for r in _read(root / state["current"]["packet_file"]) if r["context"]["skill_name"] == name)
        before = row["context"]
        assert len(before["input_ids"]) > 1
        oversized = state["current"]["output_uri"] + "/" + row["output_path"]
        owner.ov.contents[oversized] = "过长的新增正文" * 2000
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        after = next(r["context"] for r in _read(root / state["current"]["packet_file"])
                     if r["context"]["skill_name"] == name)
        assert after["before"] == before["before"] and len(after["input_ids"]) < len(before["input_ids"])
        assert after["input_ids"] == before["input_ids"][:len(after["input_ids"])]
        assert _ledger(root, name)["capacity_split_count"] == 1
        assert _ledger(root, name)["cursor"] == before["before"]  # No oversized output was accepted.
        for r in _read(root / state["current"]["packet_file"]):
            p = _read(root / r["local_path"])
            if "body_insert" in p["derived_controls"]:
                prior = p["derived_controls"]["previous_body"]
                patch = delta(prior, p["input_ids"])
                owner.ov.contents[state["current"]["output_uri"] + "/" + r["output_path"]] = json.dumps(
                    patch, ensure_ascii=False)
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] != "failed", job.get("error")
        assert _ledger(root, name)["cursor"] > journal["failed_ledgers"][name]["cursor"]
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        content = (owner.repo.workspace(result) / "compiled_skill" / name / "SKILL.md").read_text()
        assert content.count("先征得同意") == 1 and content.count("额外四十分钟") == 1
        assert len(json.loads((owner.repo.workspace(result) / "compiled_skill" / name / "benchmark.json")
                             .read_text())["cases"]) == 12
        # The protocol transition is a one-time recovery, not an unlimited retry reset.
        again = owner.repo.update_job(job["job_id"], status="failed", error=journal["error"], finished_at="later")
        with pytest.raises(ValueError, match="本次失败"):
            prepare_recovery(root, state, again, apply=True)

    asyncio.run(check())
