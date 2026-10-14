"""A method keeps one integrated body and an exact, recoverable source-batch journal."""

import asyncio
import copy
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.compile_strategy import render_stage_batch_compiler
from team_miner.ov_artifact_batch_orchestration import _verify
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_body import PROTOCOL, RULE, iter_integrated_skill, previous_body
from team_miner.tests.test_artifact_batches import ArtifactOwner
from team_miner.tests.test_compile_strategy import embedded_contract
from team_miner.tests.test_ov_staged_orchestration import drive, start


def section(identity, content):
    return {"schema_version": planning.SECTION_SCHEMA, "input_ids": [identity],
            "title": "完整面谈方法", "content": content}


def test_latest_revision_renders_once_while_all_batches_including_skips_are_checked(tmp_path):
    rows = [section("a", "先征得同意。"), section("b", "先征得同意，再询问需求。"),
            {"schema_version": planning.SECTION_SKIP_SCHEMA, "input_ids": ["c"], "reason": "重复示例"}]
    for i, row in enumerate(rows):
        _save(tmp_path / "sections/method" / f"{i:08d}.json", row)
    prior = previous_body(tmp_path, "method", 3)
    assert prior["section_index"] == 1 and prior["content"] == rows[1]["content"]
    result = "".join(iter_integrated_skill({"name": "method", "task": "面谈"}, rows,
                                          expected_input_ids=["a", "b", "c"], source_refs=["viking://resources/original"]))
    assert result.count("先征得同意") == 1 and "再询问需求" in result
    for bad in (rows[:-1], rows + [rows[0]]):
        with pytest.raises(ValueError):
            list(iter_integrated_skill({"name": "method", "task": "面谈"}, bad,
                                       expected_input_ids=["a", "b", "c"], source_refs=["viking://resources/original"]))


def test_writer_integrates_with_explicit_conditions_without_the_append_section_instruction():
    text = embedded_contract(render_stage_batch_compiler(SKILL_BODY, "skill_section", integrated_body=True))
    instructions = text["synthesize"]["instructions"]
    assert instructions.count(RULE) == 1
    assert "Write only a source-backed section" not in instructions
    assert "added durations\nstay additions" in instructions


@pytest.mark.parametrize("tamper", [False, True])
def test_multiple_batches_use_exact_previous_body_and_final_output_is_not_concatenated(tmp_path, tamper):
    async def check():
        owner = ArtifactOwner(tmp_path)
        owner.ov.candidate_count = 1
        original_new = owner.new

        def fresh(count=1):
            job, entries = original_new(count)
            return owner.repo.update_job(job["job_id"], staged_skill_body_protocol=PROTOCOL), entries

        owner.new = fresh
        job = await start(owner, 24)
        while (await owner._staged_load(job))[1].get("current", {}).get("stage") != "artifact_batch":
            assert job["status"] not in {"failed", "stopped"}, job.get("error")
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        assert state["skill_body_protocol"] == PROTOCOL
        first = copy.deepcopy(state["current"])
        assert all(not packet["derived_controls"]["previous_body"]["content"] for packet in owner.ov.artifacts[0])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        assert state["current"]["artifact_stage"] == "skill_section"
        rows = _read(root / state["current"]["packet_file"])
        name = rows[0]["context"]["skill_name"]
        packet = _read(root / rows[0]["local_path"])
        assert packet["derived_controls"]["previous_body"] == previous_body(root, name, 1)
        assert packet["derived_controls"]["previous_body"]["content"]
        assert first["skill_body_protocol"] == PROTOCOL
        if tamper:
            path = root / "sections" / name / "00000000.json"
            value = _read(path)
            value["content"] += "擅自更改。"
            _save(path, value)
            with pytest.raises(ValueError, match="working copy changed"):
                _verify(root, state["current"])
            return
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        body = (owner.repo.workspace(result) / "compiled_skill" / name / "SKILL.md").read_text()
        assert body.count("根据原始政策复核申请") == 1
        assert all(row["source_uri"] in body for row in [
            _read(path) for path in (root / "chunk-metadata").glob("*.json")
        ])
        assert len(list((root / "sections" / name).glob("*.json"))) == 3
        for batch in owner.ov.artifacts:
            for item in batch:
                assert len(json.dumps(item, ensure_ascii=False, separators=(",", ":"))) <= 32000

    asyncio.run(check())
