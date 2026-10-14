"""Independent public file Map outputs and durable single-current artifact batches."""

from __future__ import annotations

import asyncio
import copy
import json
from concurrent.futures import ThreadPoolExecutor

import httpx
import pytest
from fastapi import HTTPException

from team_miner import bounded_compile_plan as planning
from team_miner import compile_admission as admission
from team_miner.ov_artifact_batch_orchestration import ARTIFACT_BATCH_STAGE, _ledger
from team_miner.ov_staged_orchestration import _read, _save
from team_miner.tests.test_ov_case_batch_orchestration import BatchFake, BatchOwner
from team_miner.tests.test_ov_staged_orchestration import drive, start


class ArtifactFake(BatchFake):
    def __init__(self):
        super().__init__()
        self.artifacts = []
        self.break_artifact = {}
        self.lose_artifact_ack = False
        self.candidate_count = 4

    async def request(self, account, method, path, **kwargs):
        if path.startswith("/api/v1/tasks/"):
            return self.tasks[path.rsplit("/", 1)[-1]]
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/artifact_batch-" not in request["from"][0]:
            return await super().request(account, method, path, **kwargs)
        assert len(request["from"]) == 1 and request["from"][0] in self.directories
        key = kwargs["extra_headers"]["Idempotency-Key"]
        self.calls.append((key, copy.deepcopy(request)))
        if key not in self.accepted:
            task_id = "cmp_artifact_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents) if uri.startswith(prefix)]
            assert 1 <= len(packets) <= 8
            assert len({packet["output_path"] for packet in packets}) == len(packets)
            self.artifacts.append(copy.deepcopy(packets))
            self.tasks[task_id] = {
                "task_id": task_id,
                "status": "completed",
                "meta": {"pipeline": {"warnings": ["Partial output"], "errors": []}},
            }
            for packet in packets:
                name = packet["derived_controls"]["skill_name"]
                failures = self.break_artifact.get((packet["stage"], name), [])
                failure = failures.pop(0) if failures else ""
                if failure == "missing":
                    continue
                if packet["stage"] == "skill_section":
                    value = {
                        "schema_version": planning.SECTION_SCHEMA,
                        "input_ids": packet["input_ids"],
                        "title": "执行步骤",
                        "content": "根据原始政策复核申请，证据不足时向用户核实。",
                    }
                    if failure == "foreign":
                        value["input_ids"] = ["unknown-source"]
                    if failure == "english_title":
                        value["title"] = "Execution steps"
                    output = json.dumps(value, ensure_ascii=False)
                else:
                    output = "# 评测说明\n按已提供原文验证该候选的独立执行边界。"
                self.contents[request["to"] + "/" + packet["output_path"]] = output
        else:
            task_id, previous = self.accepted[key]
            assert previous == request
        if self.lose_artifact_ack:
            self.lose_artifact_ack = False
            raise httpx.ReadTimeout("Lost artifact acceptance")
        return {"task_id": task_id}


class ArtifactOwner(BatchOwner):
    def __init__(self, root):
        super().__init__(root)
        self.fixture.ov = ArtifactFake()
        self.ov = self.fixture.ov

    def new(self, count=1):
        job, entries = self.fixture.new(count)
        return self.repo.update_job(job["job_id"], staged_execution_protocol=admission.OPTIMIZED_PROTOCOL), entries


async def first_batch(owner):
    job = await start(owner, 8)
    while True:
        root, state = await owner._staged_load(job)
        if state.get("current", {}).get("stage") == ARTIFACT_BATCH_STAGE:
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])


def test_independent_candidates_use_four_file_maps_and_publish_skill_before_cases(tmp_path):
    async def check():
        owner = ArtifactOwner(tmp_path)
        job, root, state = await first_batch(owner)
        assert len(owner.ov.artifacts[0]) == 4
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        workspace = owner.repo.workspace(job)
        assert len(list(workspace.glob("compiled_skill/*/SKILL.md"))) == 4
        assert not list(workspace.glob("compiled_skill/*/benchmark.json"))
        assert (await owner._staged_load(job))[1]["current"]["artifact_stage"] == "evaluation"
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        assert result["candidate_count"] == 4
        assert len(owner.ov.artifacts) == 2  # Four sections and four evaluations, two Native submissions.
        for name in (await owner._staged_load(result))[1]["candidate_names"]:
            assert _ledger(root, name)["evaluation_ready"]
            dataset = json.loads((workspace / "compiled_skill" / name / "benchmark.json").read_text())
            assert len(dataset["cases"]) == 12
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["missing", "foreign", "english_title"])
def test_bad_artifact_repairs_only_its_own_assignment_and_keeps_valid_siblings(tmp_path, failure):
    async def check():
        owner = ArtifactOwner(tmp_path)
        owner.ov.break_artifact[("skill_section", "refund-review-1")] = [failure]
        job, root, state = await first_batch(owner)
        original = copy.deepcopy(state["current"])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert len(owner.ov.artifacts[1]) == 1
        assert owner.ov.artifacts[1][0]["derived_controls"]["skill_name"] == "refund-review-1"
        assert len(list(owner.repo.workspace(job).glob("compiled_skill/*/SKILL.md"))) == 3
        assert _read(root / original["request_file"])["from"] == [original["input_uri"]]
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")

    asyncio.run(check())


def test_lost_acceptance_recovers_exact_key_and_stop_does_not_submit_more_files(tmp_path):
    async def check():
        owner = ArtifactOwner(tmp_path)
        owner.ov.lose_artifact_ack = True
        job, root, state = await first_batch(owner)
        assert not job.get("ov_task_id")
        old = copy.deepcopy(state["current"])
        frozen = (root / old["request_file"]).read_bytes()
        stopped = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        restored = ArtifactOwner(tmp_path)
        restored.ov = owner.ov
        restored.fixture.ov = owner.ov
        job = await restored._resume_staged_round(stopped)
        assert job["ov_task_id"] == owner.ov.accepted[old["key"]][0]
        assert job["status"] == "stopping"
        assert len(owner.ov.artifacts) == 1 and (root / old["request_file"]).read_bytes() == frozen
        job = await restored._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "stopped"
        assert len(owner.ov.artifacts) == 1 and not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


def test_artifact_acceptance_replays_durable_sibling_outcome_after_job_update_crash(tmp_path, monkeypatch):
    async def check():
        owner = ArtifactOwner(tmp_path)
        job, root, state = await first_batch(owner)
        old_id = job["ov_task_id"]
        original = owner.repo.update_job
        raised = False

        def crash(job_id, **changes):
            nonlocal raised
            if changes.get("ov_task_id") == "" and not raised:
                raised = True
                raise OSError("Public metadata lost after durable state")
            return original(job_id, **changes)

        monkeypatch.setattr(owner.repo, "update_job", crash)
        with pytest.raises(OSError):
            await owner._advance_staged_round(job, owner.ov.tasks[old_id])
        monkeypatch.setattr(owner.repo, "update_job", original)
        job = await owner._advance_staged_round(owner.repo.get_job(job["job_id"]), owner.ov.tasks[old_id])
        assert (await owner._staged_load(job))[1]["current"]["artifact_stage"] == "evaluation"
        assert len(owner.ov.artifacts) == 2
        assert len(list((root / "artifact-batches/outcomes").glob("*.json"))) == 4

    asyncio.run(check())


def test_global_admission_is_two_across_instances_and_never_expires_pending_ack(tmp_path):
    class Repository:
        jobs_root = tmp_path

        def list_jobs(self):
            return []

    repo = Repository()
    jobs = [{"job_id": f"job-{i}", "current_round": 1, "account": "default"} for i in range(8)]
    with ThreadPoolExecutor(max_workers=8) as executor:
        accepted = list(executor.map(lambda job: admission.acquire(repo, job, {"key": job["job_id"]}), jobs))
    assert sum(accepted) == 2 and len(admission.reservations(tmp_path)) == 2
    pending = admission.reservations(tmp_path)[0]
    assert admission.acquire(repo, jobs[int(pending["job_id"].rsplit("-", 1)[-1])], {"key": pending["key"]})
    assert len(admission.reservations(tmp_path)) == 2
    admission.accepted(tmp_path, pending["key"], "cmp-exact")
    admission.release(tmp_path, task_id="cmp-other")
    assert len(admission.reservations(tmp_path)) == 2
    admission.release(tmp_path, task_id="cmp-exact")
    assert len(admission.reservations(tmp_path)) == 1


def test_batch_packet_tampering_fails_before_submission(tmp_path):
    async def check():
        owner = ArtifactOwner(tmp_path)
        job, root, state = await first_batch(owner)
        current = state["current"]
        rows = _read(root / current["packet_file"])
        path = root / rows[0]["local_path"]
        packet = json.loads(path.read_text())
        packet["evidence"][0]["content"] = "Changed original"
        _save(path, packet)
        count = len(owner.ov.calls)
        with pytest.raises(ValueError, match="exact source|Frozen|packet"):
            await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == count

    asyncio.run(check())


@pytest.mark.parametrize("code", [400, 401, 403, 413, 422])
def test_definite_native_rejection_releases_pending_reservation(tmp_path, code, monkeypatch):
    async def check():
        owner = ArtifactOwner(tmp_path)
        original = owner._knowledge_request

        async def rejected(account, method, path, **kwargs):
            if method == "POST" and path == "/api/v1/compile":
                raise HTTPException(code, "Definitive request rejection")
            return await original(account, method, path, **kwargs)

        monkeypatch.setattr(owner, "_knowledge_request", rejected)
        with pytest.raises(HTTPException):
            await start(owner)
        assert not admission.reservations(owner.repo.jobs_root)
        assert not owner.ov.accepted

    asyncio.run(check())


@pytest.mark.parametrize("accepted", [False, True])
def test_native_409_resolves_exact_key_before_releasing_or_recovering(tmp_path, accepted, monkeypatch):
    async def check():
        owner = ArtifactOwner(tmp_path)
        original = owner._knowledge_request

        async def conflict(account, method, path, **kwargs):
            if method == "POST" and path == "/api/v1/compile":
                if accepted:
                    await original(account, method, path, **kwargs)
                raise HTTPException(409, "Request identity conflict")
            return await original(account, method, path, **kwargs)

        monkeypatch.setattr(owner, "_knowledge_request", conflict)
        if accepted:
            job = await start(owner)
            root, state = await owner._staged_load(job)
            assert job["ov_task_id"] == state["current"]["task_id"]
            assert admission.reservations(owner.repo.jobs_root)[0]["task_id"] == job["ov_task_id"]
            assert _read(root / state["current"]["request_file"])["from"] == [state["current"]["input_uri"]]
        else:
            with pytest.raises(HTTPException):
                await start(owner)
            assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


def test_provisioning_failure_before_post_releases_reservation(tmp_path, monkeypatch):
    async def check():
        owner = ArtifactOwner(tmp_path)

        async def broken(*args):
            raise ValueError("Immutable compiler package mismatch")

        monkeypatch.setattr(owner, "_round_provision_compiler", broken)
        with pytest.raises(ValueError):
            await start(owner)
        assert not admission.reservations(owner.repo.jobs_root) and not owner.ov.calls

    asyncio.run(check())


def test_two_live_native_jobs_bound_third_and_stop_never_submits_waiting_child(tmp_path):
    async def check():
        owner = ArtifactOwner(tmp_path)
        first = await start(owner)
        owner.ov.tasks[first["ov_task_id"]]["status"] = "running"
        second = await start(owner)
        owner.ov.tasks[second["ov_task_id"]]["status"] = "running"
        third = await start(owner)
        assert len(owner.ov.accepted) == 2
        assert third["status"] == "preparing" and not third.get("ov_task_id")
        _, state = await owner._staged_load(third)
        assert not state["current"].get("submission_started_at")
        third = owner.repo.update_job(third["job_id"], status="stopping", ov_cancel_reason="user_stop")
        third = await owner._resume_staged_round(third)
        assert third["status"] == "stopped" and len(owner.ov.accepted) == 2
        assert len(admission.reservations(owner.repo.jobs_root)) == 2
        owner.ov.tasks[first["ov_task_id"]]["status"] = "cancelled"
        first = owner.repo.update_job(first["job_id"], status="stopping", ov_cancel_reason="user_stop")
        await owner._advance_staged_round(first, owner.ov.tasks[first["ov_task_id"]])
        assert len(admission.reservations(owner.repo.jobs_root)) == 1

    asyncio.run(check())


@pytest.mark.parametrize("stage,invalid_ack", [("extract", {}), ("artifact_batch", []), ("case_batch", {})])
@pytest.mark.parametrize("stop", [False, True])
def test_malformed_success_ack_keeps_exact_key_recoverable_and_stop_observable(
    tmp_path, monkeypatch, stage, invalid_ack, stop
):
    async def check():
        owner = ArtifactOwner(tmp_path)
        original = owner._knowledge_request
        broken = False

        async def incomplete(account, method, path, **kwargs):
            nonlocal broken
            accepted = await original(account, method, path, **kwargs)
            if method == "POST" and path == "/api/v1/compile":
                phase = kwargs["json"]["from"][0].split("/stages/", 1)[1].split("-", 1)[0].split("/", 1)[0]
                if phase == stage and not broken:
                    broken = True
                    return invalid_ack
            return accepted

        monkeypatch.setattr(owner, "_knowledge_request", incomplete)
        job = await start(owner)
        while job.get("ov_task_id"):
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        current = copy.deepcopy(state["current"])
        assert current["stage"] == stage and job["status"] == "preparing"
        assert current.get("submission_started_at")
        assert len(admission.reservations(owner.repo.jobs_root)) == 1
        frozen = (root / current["request_file"]).read_bytes()
        calls = len(owner.ov.calls)
        if stop:
            job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        job = await owner._resume_staged_round(job)
        assert job["ov_task_id"] == owner.ov.accepted[current["key"]][0]
        assert len(owner.ov.calls) == calls
        assert (root / current["request_file"]).read_bytes() == frozen
        if stop:
            assert job["status"] == "stopping"
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
            assert job["status"] == "stopped" and len(owner.ov.calls) == calls
            assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())
