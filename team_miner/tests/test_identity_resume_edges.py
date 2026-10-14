"""Independent identity-resume regressions using only synthetic local Compile transport."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.tests.test_ov_staged_orchestration import FakeCompile, Owner, drive


class SeparateSourceCompile(FakeCompile):
    """Two independent methods; their writer Evidence must never cross sources."""

    def __init__(self):
        super().__init__()
        self.candidate_count = 2
        self.packets = []
        self.reuse_prior = False
        self.reuse_responses = []

    async def request(self, account, method, path, **kwargs):
        fresh = method == "POST" and path == "/api/v1/compile" and (
            kwargs["extra_headers"]["Idempotency-Key"] not in self.accepted
        )
        accepted = await super().request(account, method, path, **kwargs)
        if not fresh:
            return accepted
        request = kwargs["json"]
        packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
        self.packets.append(copy.deepcopy(packet))
        controls = packet["derived_controls"]
        uri = request["to"] + "/" + packet["output_path"]
        if packet["stage"] == "extract":
            refs_by_id = {identity: alias for alias, identity in controls["reference_map"].items()}
            methods, facts = [], []
            for chunk in packet["evidence"]:
                if not chunk["source_uri"].rsplit("/", 1)[-1].startswith("doc-"):
                    facts.append({
                        "text": "补证回执记录，未提出新增可执行方法", "source_refs": [refs_by_id[chunk["id"]]],
                    })
                    continue
                index = int(chunk["source_uri"].rsplit("doc-", 1)[-1].split(".", 1)[0])
                methods.append({
                    "task": f"独立复核方法-{index}",
                    "boundary": f"仅适用文档 {index} 的操作条件",
                    "source_refs": [refs_by_id[chunk["id"]]],
                })
            self.contents[uri] = json.dumps({
                "schema_version": planning.INDEX_SCHEMA,
                "input_ids": packet["input_ids"],
                "methods": methods, "facts": facts, "deferred": [],
            }, ensure_ascii=False)
        elif packet["stage"] == "plan" and self.reuse_prior:
            output = json.loads(self.contents[uri])
            by_name = {row["name"]: row for row in controls["known_candidates"]}
            assert output["schema_version"] in {planning.SELECTION_SCHEMA_V2, planning.SELECTION_SCHEMA_V3}
            output["skills"] = [
                {"derived_from": row["derived_from"],
                 "reuse_candidate_id": by_name[row["name"]]["candidate_id"],
                 "reason": "同一独立工作流，沿用已复核身份"}
                for row in output["skills"]
            ]
            self.reuse_responses.append(copy.deepcopy(output))
            self.contents[uri] = json.dumps(output, ensure_ascii=False)
        return accepted


class PausingOwner(Owner):
    """Pause at the durable no-child boundary before one old writer submission."""

    def __init__(self, root, *, pause_stage=""):
        super().__init__(root)
        self.ov = SeparateSourceCompile()
        self.pause_stage = pause_stage
        self.paused = False

    async def _staged_submit(self, job, root, state, stage, output_path, chunks, controls, context):
        if self.pause_stage == stage and context.get("skill_name") == "refund-review-0":
            self.pause_stage = ""
            self.paused = True
            assert state["current"] is None
            return await self._staged_save(job, root, state, status="preparing", ov_task_id="")
        return await super()._staged_submit(job, root, state, stage, output_path, chunks, controls, context)


LANGUAGE_POLICY = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}


@pytest.mark.parametrize("resume_stage", ["evaluation", "case"])
def test_identity_resume_does_not_leak_current_candidate_source_to_the_next_candidate(tmp_path, resume_stage):
    async def check():
        owner = PausingOwner(tmp_path, pause_stage=resume_stage)
        job, entries = owner.new(2)
        job = await owner._prepare_staged_round(job, SKILL_BODY, entries, LANGUAGE_POLICY, "")
        root, state = await owner._staged_load(job)
        # An older controller has already admitted the planning output but has
        # not performed the new auxiliary identity review yet.
        state.pop("identity_protocol", None)
        state["identity_review_complete"] = True
        _save(root / "state.json", state)
        for _ in range(100):
            if owner.paused:
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert owner.paused, "The old writer must reach its durable evaluation/case boundary"
        root, state = await owner._staged_load(job)
        assert state["phase"] == resume_stage and state["current"] is None
        assert state["section_count"] == 1 and state["candidate_names"] == ["refund-review-0", "refund-review-1"]
        before = {path: path.read_bytes() for path in root.glob("children/*/request.json")}
        state.pop("identity_review_complete")
        _save(root / "state.json", state)

        # A new process loads the journal rather than keeping the prior in-memory
        # owner. Accepted requests remain byte-for-byte untouched.
        restored = PausingOwner(tmp_path)
        restored.ov = owner.ov
        job = await restored._resume_staged_round(job)
        for _ in range(100):
            _, state = await restored._staged_load(job)
            if state.get("identity_review_complete"):
                break
            job = await restored._advance_staged_round(job, restored.ov.tasks[job["ov_task_id"]])
        assert state["writer_resume"]["phase"] == resume_stage
        assert state["phase"] == resume_stage
        assert "writer_pending_ordinals" not in state
        assert all(path.read_bytes() == content for path, content in before.items())

        job = await drive(restored, job)
        assert job["status"] == "waiting_human", job.get("error")
        _, state = await restored._staged_load(job)
        assert "writer_pending_ordinals" not in state
        plan = _read(root / "plan.json")
        assert [row["name"] for row in plan["skills"]] == ["refund-review-0", "refund-review-1"]
        for index in (0, 1):
            name = f"refund-review-{index}"
            expected_uri = job["source_snapshot"] + f"/original/doc-{index}.md"
            scope = _read(root / "scopes" / (name + ".json"))
            assert len(scope["ordinals"]) == 1
            packets = [packet for packet in restored.ov.packets
                       if packet["stage"] in {"skill_section", "evaluation", "case"}
                       and packet["derived_controls"]["skill_name"] == name]
            assert {packet["stage"] for packet in packets} == {"skill_section", "evaluation", "case"}
            for packet in packets:
                assert {chunk["source_uri"] for chunk in packet["evidence"]} == {expected_uri}
                assert packet["derived_controls"]["allowed_source_refs"] == [expected_uri]
            document = json.loads((restored.repo.workspace(job) / f"compiled_skill/{name}/benchmark.json").read_text())
            assert len(document["cases"]) == 12
            assert all(case["provenance"]["session_ids"] == [expected_uri] for case in document["cases"])

    asyncio.run(check())


def test_second_round_explicit_reuse_preserves_each_candidates_twelve_reviewed_case_ids(tmp_path):
    async def check():
        owner = PausingOwner(tmp_path)
        job, entries = owner.new(2)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        manifest.write_text(json.dumps(entries), encoding="utf-8")
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, LANGUAGE_POLICY, ""))
        assert job["status"] == "waiting_human"
        reviewed = {}
        for index in (0, 1):
            name = f"refund-review-{index}"
            path = owner.repo.workspace(job) / f"compiled_skill/{name}/benchmark.json"
            document = json.loads(path.read_text())
            reviewed[name] = [f"reviewed-{index}:{case}" for case in range(12)]
            for case, identity in zip(document["cases"], reviewed[name], strict=True):
                case["case_id"] = identity
            path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        first_plan = _read((await owner._staged_load(job))[0] / "plan.json")
        job = owner.repo.prepare_next_round(job["job_id"], job["pending_checkpoint"]["id"], {}, [], {})
        entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, job, staged=True)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-2/manifest.json"
        job = owner.repo.update_job(job["job_id"], input_manifest=str(manifest))
        owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        owner.ov.reuse_prior = True
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, LANGUAGE_POLICY, ""))
        assert job["status"] == "waiting_human", job.get("error")
        assert owner.ov.reuse_responses
        assert all(set(row) == {"derived_from", "reuse_candidate_id", "reason"}
                   for response in owner.ov.reuse_responses for row in response["skills"])
        plan = _read((await owner._staged_load(job))[0] / "plan.json")
        assert len(plan["skills"]) == 2
        previous = {row["name"]: row for row in first_plan["skills"]}
        for candidate in plan["skills"]:
            name = candidate["name"]
            assert {key: value for key, value in candidate.items() if key != "source_refs"} == {
                key: value for key, value in previous[name].items() if key != "source_refs"
            }
            document = json.loads((owner.repo.workspace(job) / f"compiled_skill/{name}/benchmark.json").read_text())
            assert [case["case_id"] for case in document["cases"]] == reviewed[name]

    asyncio.run(check())
