"""Independent bounded source indexes through the unchanged public directory Compile API."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_extract_batch_orchestration import EXTRACT_BATCH_PROTOCOL, _verified
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.artifact_batches import ArtifactFake, ArtifactOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive


class ExtractFake(ArtifactFake):
    """Only model results are synthetic; directory admission and per-file persistence stay real."""

    def __init__(self):
        super().__init__()
        self.candidate_count = 1
        self.extract_batches = []
        self.break_indexes = {}
        self.extract_ack = ""
        self.extract_post_status = 0

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/extract_batch-" not in request["from"][0]:
            return await super().request(account, method, path, **kwargs)
        key = kwargs["extra_headers"]["Idempotency-Key"]
        assert set(request) == {"from", "to", "skill", "instruction"}
        assert len(request["from"]) == 1 and request["from"][0] in self.directories
        if self.extract_post_status:
            raise HTTPException(self.extract_post_status, "Compile request rejected")
        self.calls.append((key, copy.deepcopy(request)))
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert previous == request
        else:
            task_id = "cmp_extract_" + str(len(self.accepted))
            self.accepted[key] = (task_id, copy.deepcopy(request))
            prefix = request["from"][0] + "/"
            packets = [json.loads(self.contents[uri]) for uri in sorted(self.contents) if uri.startswith(prefix)]
            assert 1 <= len(packets) <= 8
            assert len({packet["output_path"] for packet in packets}) == len(packets)
            assert all(len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))) <= 32000
                       for packet in packets)
            self.extract_batches.append(copy.deepcopy(packets))
            self.tasks[task_id] = {"task_id": task_id, "status": "completed",
                                  "meta": {"pipeline": {"warnings": ["One independent output may be missing"]}}}
            for packet in packets:
                ordinal = int(packet["output_path"].split("-")[-1].split(".")[0])
                errors = self.break_indexes.get(ordinal, [])
                failure = errors.pop(0) if errors else ""
                if failure == "missing":
                    self.tasks[task_id]["meta"]["pipeline"]["errors"] = [
                        "File not found: viking://agent/skills/compiler/skill-miner.index.v2"
                    ]
                    continue
                refs = packet["derived_controls"]["allowed_source_refs"]
                output = {
                    "schema_version": planning.INDEX_SCHEMA_V2,
                    # Public native IDs are deliberately different from logical TE IDs.
                    "input_ids": ["0123456789abcdef01234567"],
                    "methods": [{"task": "退款复核", "boundary": "仅按原始政策复核", "source_refs": refs}],
                    "facts": [], "deferred": [], "context_refs": refs[:1], "context_uncertain": False,
                }
                if failure == "foreign":
                    output["methods"][0]["source_refs"] = ["viking://resources/unassigned.md"]
                if failure == "context_foreign":
                    output["context_refs"] = ["unassigned-scope"]
                if failure == "incomplete":
                    output["methods"][0]["source_refs"] = refs[:1]
                if failure == "deferred_text":
                    output["deferred"] = [{"text": "原文提及的附件未提供。", "source_refs": refs}]
                self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(
                    output, ensure_ascii=False
                )
        if self.extract_ack == "lost":
            self.extract_ack = ""
            raise httpx.ReadTimeout("Accepted extraction response was lost")
        if self.extract_ack == "malformed":
            self.extract_ack = ""
            return {}
        return {"task_id": task_id}


class ExtractOwner(ArtifactOwner):
    """Capture extraction completion by default; full=True runs actual remaining TE orchestration."""

    def __init__(self, root, *, full=False):
        super().__init__(root)
        self.fixture.ov = self.ov = ExtractFake()
        self.full = full

    def new(self, count=17):
        job, entries = self.fixture.new(count)
        job = self.repo.update_job(job["job_id"], staged_execution_protocol=admission.OPTIMIZED_PROTOCOL,
                                   staged_efficiency_protocol="te-mining-efficient.v2",
                                   staged_workflow_protocol="", staged_source_scope_protocol="",
                                   staged_planning_alias_protocol="", staged_case_group_protocol="",
                                   staged_compact_planning_protocol="", staged_quote_alias_protocol="",
                                   staged_identity_partition_protocol="", staged_uncertainty_scope_protocol="",
                                   staged_workflow_partition_protocol="")
        # This fixture covers the frozen efficient-v2 transport; fresh gates are tested independently.
        for entry in entries:
            # Eleven thousand Unicode characters fit one chunk, but only two fit one bounded packet.
            text = "按原文确认申请并复核退款政策；缺值时询问用户。" * 480
            path = Path(entry["local_path"])
            path.write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries

    async def _staged_catalog_ready(self, job, root, state):
        if self.full:
            return await super()._staged_catalog_ready(job, root, state)
        state["test_extraction_complete"] = True
        return await self._staged_save(job, root, state, status="waiting_human", ov_task_id="")


async def begin(owner, count=17):
    job, entries = owner.new(count)
    return await owner._prepare_staged_round(
        job, SKILL_BODY, entries, {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}, ""
    )


async def finish_extract(owner, job):
    for _ in range(30):
        if job["status"] in {"waiting_human", "failed", "stopped"}:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Bounded extract repair did not terminate")


def test_actual_public_map_batch_reduces_submissions_and_preserves_every_source(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path)
        job = await finish_extract(owner, await begin(owner, 17))
        assert job["status"] == "waiting_human", job.get("error")
        root, state = await owner._staged_load(job)
        packets = [packet for batch in owner.ov.extract_batches for packet in batch]
        assert len(packets) > 4
        assert len(owner.ov.calls) == (len(packets) + 7) // 8 < len(packets)
        assert state["extract_batch_protocol"] == EXTRACT_BATCH_PROTOCOL
        assert state["unit_count"] == len(packets)
        assert state["cursor"] == state["chunk_count"]
        assigned = [chunk["ordinal"] for packet in packets for chunk in packet["evidence"]]
        assert assigned == list(range(state["chunk_count"]))
        exact = []
        for path in sorted((root / "units/0").glob("*.json")):
            unit = _read(path)
            assert unit["value"]["schema_version"] == planning.INDEX_SCHEMA_V2
            exact.extend({ordinal for scope in unit["bindings"].values() for ordinal in scope})
            assert unit["value"]["context_refs"]
        assert sorted(exact) == list(range(state["chunk_count"]))
        origins = list((root / "extract-batches/accepted").glob("*.json"))
        assert len(origins) == state["unit_count"]
        for path in origins:
            origin = _read(path)
            assert _sha((root / origin["result_file"]).read_text()) == origin["result_sha256"]
            assert _sha((root / origin["unit_path"]).read_text()) == origin["unit_sha256"]
            child = _read(root / "children" / origin["child"] / "metadata.json")
            _verified(root, child)
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


def test_new_extract_batches_integrate_with_workflow_selection_and_complete_canonical_artifacts(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path, full=True)
        job = await drive(owner, await begin(owner, 8))
        assert job["status"] == "waiting_human" and job["candidate_count"] == 1, job.get("error")
        assert len(owner.ov.extract_batches) == 1 and len(owner.ov.extract_batches[0]) == 4
        dataset = json.loads((owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json").read_text())
        assert len(dataset["cases"]) == 12
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["missing", "foreign", "context_foreign"])
def test_repair_only_bad_index_retains_valid_siblings_and_does_not_skip_originals(tmp_path, failure):
    async def check():
        owner = ExtractOwner(tmp_path)
        owner.ov.break_indexes[1] = [failure]
        job = await begin(owner, 8)
        root, state = await owner._staged_load(job)
        original = copy.deepcopy(state["current"])
        frozen = (root / original["request_file"]).read_bytes()
        first = copy.deepcopy(owner.ov.extract_batches[0])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        state = (await owner._staged_load(job))[1]
        assert state["cursor"] == state["unit_count"] == 0
        assert len(owner.ov.extract_batches[1]) == 1
        assert owner.ov.extract_batches[1][0]["output_path"] == first[1]["output_path"]
        assert len(list((root / "extract-batches/accepted").glob("*.json"))) == len(first) - 1
        assert (root / original["request_file"]).read_bytes() == frozen
        final = await finish_extract(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        assignments = [packet["output_path"] for batch in owner.ov.extract_batches for packet in batch]
        assert assignments.count(first[1]["output_path"]) == 2
        assert assignments.count(first[0]["output_path"]) == 1

    asyncio.run(check())


def test_deferred_wrong_field_sends_precise_repair_without_rewriting_model_or_source(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path)
        owner.ov.break_indexes[1] = ["deferred_text"]
        job = await begin(owner, 8)
        root, state = await owner._staged_load(job)
        original = copy.deepcopy(state["current"])
        first = copy.deepcopy(owner.ov.extract_batches[0])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        repair = owner.ov.extract_batches[1][0]
        error = repair["derived_controls"]["repair_error"]
        assert "deferred[0]" in error and "missing=['reason']" in error and "unexpected=['text']" in error
        assert repair["evidence"] == first[1]["evidence"]
        assert repair["output_path"] == first[1]["output_path"]
        raw_path = root / "children" / original["child"] / (first[1]["output_path"] + ".raw")
        raw = raw_path.read_bytes()
        assert json.loads(raw)["deferred"][0].keys() == {"text", "source_refs"}
        assert len(list((root / "extract-batches/accepted").glob("*.json"))) == len(first) - 1
        final = await finish_extract(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        assert raw_path.read_bytes() == raw
        assert [len(batch) for batch in owner.ov.extract_batches] == [4, 1]

    asyncio.run(check())


def test_partial_native_failure_does_not_erase_previous_schema_repair_or_repeat_valid_siblings(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path)
        owner.ov.break_indexes[1] = ["deferred_text", "missing"]
        job = await begin(owner, 8)
        first = copy.deepcopy(owner.ov.extract_batches[0])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        repair = owner.ov.extract_batches[2][0]
        error = repair["derived_controls"]["repair_error"]
        assert "missing=['reason']" in error and "unexpected=['text']" in error
        assert "Native task-level diagnostics" in error and "File not found:" in error
        assert "skill-miner.index.v2" in error and len(error) <= 1000
        assert repair["evidence"] == first[1]["evidence"]
        final = await finish_extract(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        assert [len(batch) for batch in owner.ov.extract_batches] == [4, 1, 1]

    asyncio.run(check())


@pytest.mark.parametrize("failure", ["foreign", "incomplete"])
def test_unknown_source_and_missing_source_coverage_exhaust_only_the_bad_packet(tmp_path, failure):
    async def check():
        owner = ExtractOwner(tmp_path)
        owner.ov.break_indexes[0] = [failure] * 3
        job = await finish_extract(owner, await begin(owner, 8))
        root, state = await owner._staged_load(job)
        assert job["status"] == "failed" and "repair limit" in job["error"]
        assert state["cursor"] == 0
        assert [len(batch) for batch in owner.ov.extract_batches] == [4, 1, 1]
        assert len(list((root / "extract-batches/accepted").glob("*.json"))) == 3
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


@pytest.mark.parametrize("ack", ["lost", "malformed"])
def test_unknown_ack_restart_recovers_original_key_then_stop_never_starts_more_source(tmp_path, ack):
    async def check():
        owner = ExtractOwner(tmp_path)
        owner.ov.extract_ack = ack
        job = await begin(owner, 17)
        root, state = await owner._staged_load(job)
        old = copy.deepcopy(state["current"])
        assert not job.get("ov_task_id") and len(owner.ov.accepted) == 1
        frozen = (root / old["request_file"]).read_bytes()
        stopped = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user_stop")
        second = ExtractOwner(tmp_path)
        second.ov = second.fixture.ov = owner.ov
        job = await second._resume_staged_round(stopped)
        assert job["ov_task_id"] == owner.ov.accepted[old["key"]][0]
        assert job["status"] == "stopping"
        job = await second._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "stopped" and len(owner.ov.calls) == 1
        assert (root / old["request_file"]).read_bytes() == frozen
        assert not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())


def test_content_service_transient_retries_same_child_without_consuming_format_attempt(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path)
        job = await begin(owner, 8)
        root, state = await owner._staged_load(job)
        current = copy.deepcopy(state["current"])
        owner.ov.read_status = 503
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        ledger = _read(root / current["ledger_file"])
        assert all(slot["format_attempts"] == 0 for slot in ledger["slots"])
        assert (await owner._staged_load(job))[1]["current"]["key"] == current["key"]
        owner.ov.read_status = 0
        final = await finish_extract(owner, job)
        assert final["status"] == "waiting_human" and len(owner.ov.calls) == 1

    asyncio.run(check())


def test_accepted_unit_replays_after_state_commit_public_metadata_crash(tmp_path, monkeypatch):
    async def check():
        owner = ExtractOwner(tmp_path)
        job = await begin(owner, 8)
        root, _ = await owner._staged_load(job)
        old_id, update = job["ov_task_id"], owner.repo.update_job
        raised = False

        def fail_once(job_id, **changes):
            nonlocal raised
            if changes.get("ov_task_id") == "" and not raised:
                raised = True
                raise OSError("Public job metadata interrupted after durable extraction ledger")
            return update(job_id, **changes)

        monkeypatch.setattr(owner.repo, "update_job", fail_once)
        with pytest.raises(OSError):
            await owner._advance_staged_round(job, owner.ov.tasks[old_id])
        monkeypatch.setattr(owner.repo, "update_job", update)
        job = await owner._advance_staged_round(owner.repo.get_job(job["job_id"]), owner.ov.tasks[old_id])
        assert job["status"] == "waiting_human"
        assert len(owner.ov.calls) == 1
        assert len(list((root / "extract-batches/accepted").glob("*.json"))) == 4

    asyncio.run(check())


def test_durable_outcome_cannot_replace_validated_raw_with_different_business_content(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path)
        job = await begin(owner, 8)
        root, initial = await owner._staged_load(job)
        old = copy.deepcopy(initial["current"])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        # Model a replay after the outcome commit but before the first slot ledger commit.
        ledger = _read(root / old["ledger_file"])
        ledger["slots"][0]["status"] = "pending"
        _save(root / old["ledger_file"], ledger)
        outcome_path = root / "extract-batches/outcomes" / (old["child"] + "-index-00000000.json.json")
        outcome = _read(outcome_path)
        outcome["unit"]["value"]["methods"][0]["task"] = "Unverified changed business procedure"
        _save(outcome_path, outcome)
        initial["current"] = old
        _save(root / "state.json", initial)
        job = owner.repo.update_job(job["job_id"], status="running", ov_task_id=old["task_id"])
        job = await owner._advance_staged_round(job, owner.ov.tasks[old["task_id"]])
        assert job["status"] == "failed" and "no longer matches" in job["error"]
        assert len(owner.ov.calls) == 1

    asyncio.run(check())


def test_original_chunk_tamper_blocks_resume_without_reposting_or_accepting_the_result(tmp_path):
    async def check():
        owner = ExtractOwner(tmp_path)
        job = await begin(owner, 8)
        root, state = await owner._staged_load(job)
        path = root / "chunks/00000000.json"
        chunk = _read(path)
        chunk["content"] = "Changed original text"
        _save(path, chunk)
        with pytest.raises(ValueError, match="Evidence changed"):
            await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == 1 and not list((root / "extract-batches/accepted").glob("*.json"))
        assert state["current"]["task_id"] == job["ov_task_id"]

    asyncio.run(check())


@pytest.mark.parametrize("status", [400, 401, 403, 413, 422])
def test_definite_rejection_releases_unaccepted_global_slot(tmp_path, status):
    async def check():
        owner = ExtractOwner(tmp_path)
        owner.ov.extract_post_status = status
        with pytest.raises(HTTPException) as caught:
            await begin(owner, 8)
        assert caught.value.status_code == status
        assert not owner.ov.accepted and not admission.reservations(owner.repo.jobs_root)

    asyncio.run(check())
