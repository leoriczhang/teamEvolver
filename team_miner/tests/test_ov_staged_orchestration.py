"""Exercise unchanged public Compile transport and durable TE phase control in private tmp directories."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from fastapi import HTTPException

from team_miner import bounded_compile_plan as planning
from team_miner.ov_compile_mining import RUNS_ROOT, SKILL_BODY, OvSkillMiningRepository
from team_miner.ov_staged_orchestration import (
    OvStagedOrchestrationMixin,
    _assignment_revision,
    _bind_unit,
    _canonical_extract_references,
    _canonical_planning_header,
    _project,
    _staged_execution_budget,
)
from team_miner.tests.dataset_fixtures import build_document


def _fake_merge_output(controls):
    grouped = {}
    for item in controls["items"]:
        grouped.setdefault((item["task"], item["boundary"]), []).append(item["item_id"])
    return {
        "schema_version": planning.MERGE_SCHEMA,
        "input_ids": controls["input_ids"],
        "methods": [{"task": task, "boundary": boundary, "derived_from": origins}
                    for (task, boundary), origins in grouped.items()],
        "facts": [], "deferred": [], "resolved": [],
    }


class FakeCompile:
    def __init__(self):
        self.contents, self.skills, self.accepted, self.tasks = {}, {}, {}, {}
        self.calls = []
        self.lose_ack = False
        self.bad_case = False
        self.defer_case = False
        self.duplicate_case = False
        self.missing_output = False
        self.extra_output = False
        self.partial = False
        self.read_status = 0
        self.candidate_count = 1
        self.extract_raw_refs = False
        self.extract_uri_refs = False
        self.extract_input_ids_style = "packet"
        self.directories = set()

    async def mkdir(self, uri):
        if uri.rsplit("/", 1)[0] not in self.directories:
            raise HTTPException(404, "parent directory is missing")
        self.directories.add(uri)

    async def request(self, account, method, path, **kwargs):
        if path.startswith("/api/v1/compile/submissions/"):
            key = path.rsplit("/", 1)[-1]
            if key not in self.accepted:
                raise HTTPException(404, "not found")
            return {"task_id": self.accepted[key][0]}
        if path == "/api/v1/content/read":
            if self.read_status:
                raise HTTPException(self.read_status, "simulated content endpoint error")
            uri = kwargs["params"]["uri"]
            if uri not in self.contents:
                raise HTTPException(404, "not found")
            return {"content": self.contents[uri]}
        assert path == "/api/v1/compile" and method == "POST"
        body, key = copy.deepcopy(kwargs["json"]), kwargs["extra_headers"]["Idempotency-Key"]
        assert set(body) == {"from", "to", "skill", "instruction"}
        # The public gateway validates directories before accepting a submission.
        # Internal per-file Map support does not make file-valued `from` valid.
        for uri in body["from"]:
            if uri not in self.directories:
                raise HTTPException(400, "Compile source must be a directory: " + uri)
        self.calls.append((key, body))
        if key in self.accepted:
            task_id, previous = self.accepted[key]
            assert body == previous
        else:
            task_id = "cmp_" + str(len(self.accepted))
            self.accepted[key] = (task_id, body)
            if "/stages/merge_batch-" in body["from"][0]:
                prefix = body["from"][0] + "/"
                packets = [json.loads(text) for uri, text in sorted(self.contents.items())
                           if uri.startswith(prefix) and uri.rsplit("/", 1)[-1].startswith("packet-")]
                assert 1 <= len(packets) <= 8 and all(packet["stage"] == "merge_index" for packet in packets)
                assert len({packet["output_path"] for packet in packets}) == len(packets)
                self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
                for packet in packets:
                    uri = body["to"] + "/" + packet["output_path"]
                    self.contents[uri] = json.dumps(_fake_merge_output(packet["derived_controls"]), ensure_ascii=False)
                    if self.missing_output:
                        self.contents.pop(uri)
                if self.extra_output:
                    self.contents[body["to"] + "/extra.md"] = "Unexpected extra file"
                if self.partial:
                    self.tasks[task_id]["meta"] = {
                        "pipeline": {"errors": ["one source was not processed"], "warnings": ["Partial output"]}
                    }
                if self.lose_ack:
                    self.lose_ack = False
                    raise httpx.ReadTimeout("acknowledgement lost after acceptance")
                return {"task_id": task_id}
            packet = json.loads(self.contents[body["from"][0] + "/packet.json"])
            controls = packet["derived_controls"]
            refs = controls["allowed_source_refs"]
            phase = packet["stage"]
            if phase == "extract":
                if self.extract_raw_refs:
                    refs = packet["input_ids"]
                if self.extract_uri_refs:
                    refs = list(dict.fromkeys(chunk["source_uri"] for chunk in packet["evidence"]))
                output = {
                    "schema_version": planning.INDEX_SCHEMA,
                    "input_ids": packet["input_ids"],
                    "methods": [
                        {
                            "task": "退款复核" if self.candidate_count == 1 else f"退款复核-{index}",
                            "boundary": "仅按文档规则复核",
                            "source_refs": refs,
                        }
                        for index in range(self.candidate_count)
                    ],
                    "facts": [],
                    "deferred": [],
                }
                if self.extract_input_ids_style == "native_hashes":
                    output["input_ids"] = [f"{index:032x}" for index in range(15)]
                elif self.extract_input_ids_style == "omitted":
                    output.pop("input_ids")
            elif phase == "merge_index":
                output = _fake_merge_output(controls)
            elif phase == "plan":
                output = {
                    "schema_version": controls.get("selection_schema", planning.SELECTION_SCHEMA),
                    "input_ids": controls["input_ids"],
                    "skills": [
                        {
                            "name": "refund-review"
                            if self.candidate_count == 1
                            else "refund-review-" + item["task"].rsplit("-", 1)[-1],
                            "task": item["task"],
                            "trigger": "用户提出退款请求",
                            "inputs": ["退款请求"],
                            "outputs": ["复核结论"],
                            "boundary": "只按提供的文档",
                            "rationale": "形成独立复核方法",
                            "derived_from": [item["item_id"]],
                        }
                        for item in controls["items"]
                    ],
                    "knowledge": [],
                    "deferred": [],
                    "resolved": [],
                }
            elif phase == "identity_group":
                output = {
                    "schema_version": "skill-miner.identity-groups.v1",
                    "candidate_ids": controls["required_candidate_ids"],
                    "groups": [{"candidate_ids": [identity], "reason": "独立方法"}
                               for identity in controls["required_candidate_ids"]],
                }
            elif phase == "skill_section":
                output = {
                    "schema_version": planning.SECTION_SCHEMA,
                    "input_ids": packet["input_ids"],
                    "title": "退款规则",
                    "content": "按照提供的 Evidence 复核退款资格并保留原文来源。",
                }
            elif phase == "evaluation":
                output = "# 评测说明\n\n验证文档中的退款规则、适用边界及流程。"
            else:
                assert phase == "case"
                if self.defer_case:
                    output = {
                        "schema_version": planning.DEFERRED_SCHEMA,
                        "reason": "Evidence 无法支持十二个独立检查项",
                        "source_refs": refs,
                    }
                else:
                    output = build_document(controls["skill_name"], [{"query": "Fixture case"}])["cases"][0]
                    output["case_id"] = controls["case_id"]
                    output["query"] = "根据来源复核退款请求：" + (
                        "同一个请求" if self.duplicate_case else controls["case_id"]
                    )
                    output["provenance"]["session_ids"] = refs
                    if self.bad_case:
                        output["checks"] = output["checks"][:1]
            self.contents[body["to"] + "/" + packet["output_path"]] = (
                output if isinstance(output, str) else json.dumps(output, ensure_ascii=False)
            )
            self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
            if self.missing_output:
                self.contents.pop(body["to"] + "/" + packet["output_path"])
            if self.extra_output:
                self.contents[body["to"] + "/extra.md"] = "Unexpected extra file"
            if self.partial:
                self.tasks[task_id]["meta"] = {
                    "pipeline": {"errors": ["one source was not processed"], "warnings": ["Partial output"]}
                }
        if self.lose_ack:
            self.lose_ack = False
            raise httpx.ReadTimeout("acknowledgement lost after acceptance")
        return {"task_id": task_id}


class Owner(OvStagedOrchestrationMixin):
    def _staged_cases_batched(self):
        # This fixture exercises legacy frozen single-Case transport and six-stage snapshots.
        return False

    def __init__(self, root):
        self.repo = OvSkillMiningRepository(root / "state", root)
        self.ov = FakeCompile()

    def _ov_skill_mining_repository(self):
        return self.repo

    def _round_client(self, job):
        return self.ov

    async def _round_provision_compiler(self, client, name, body):
        assert name.endswith(body.split("name: ", 1)[1].split("\n")[0].rsplit("-", 1)[-1])
        if name in client.skills:
            assert client.skills[name] == body
        client.skills[name] = body
        return {"ok": True}

    async def _round_write_immutable(self, account, uri, content):
        if uri in self.ov.contents:
            assert self.ov.contents[uri] == content
        self.ov.contents[uri] = content

    async def _knowledge_request(self, account, method, path, **kwargs):
        return await self.ov.request(account, method, path, **kwargs)

    async def _knowledge_tree(self, account, uri):
        return {
            "exists": True,
            "entries": [{"uri": value, "is_dir": False} for value in self.ov.contents if value.startswith(uri + "/")],
        }

    async def _round_submission_retry(self, repo, job, exc):
        return repo.update_job(job["job_id"], submission_error=str(exc))

    async def _record_round_acceptance(self, repo, job, accepted, started, revision=""):
        return repo.update_job(
            job["job_id"],
            ov_task_id=accepted["task_id"],
            status="stopping" if job.get("ov_cancel_reason") else "queued",
        )

    async def _complete_ov_skill_round(self, job):
        return self.repo.finish_materialized_round(job["job_id"])

    def new(self, count=1):
        job = self.repo.create_job(
            name="staged-test",
            account="default",
            source_uri="viking://resources/source",
            source_snapshot_uri=RUNS_ROOT + "/pending/source",
            output_uri=RUNS_ROOT + "/pending/output",
            document_count=count,
            max_rounds=1,
            human_checkpoints=True,
        )
        # This fixture intentionally covers the durable pre-batching protocol.
        # Optimized public Map batches have a separate transport/compatibility fixture.
        job = self.repo.update_job(
            job["job_id"], staged_execution_protocol="", staged_efficiency_protocol="",
            staged_source_unit_protocol="", staged_skill_body_protocol="",
        )
        self.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        frozen = self.repo._job_dir(job["job_id"]) / "round_inputs" / "round-1"
        frozen.mkdir(parents=True, exist_ok=True)
        entries = []
        for index in range(count):
            text = "退款规则：按用户申请与政策条件复核，缺乏证据时不得猜测。\n" * 2
            path = frozen / f"source-{index}.md"
            path.write_text(text)
            import hashlib

            entries.append(
                {
                    "kind": "source",
                    "relative_path": f"original/doc-{index}.md",
                    "local_path": str(path),
                    "sha256": hashlib.sha256(text.encode()).hexdigest(),
                    "content_bytes": len(text.encode()),
                }
            )
        return job, entries


async def start(owner, count=1, revision=""):
    job, entries = owner.new(count)
    return await owner._prepare_staged_round(
        job, SKILL_BODY, entries, {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}, revision
    )


async def drive(owner, job, maximum=5000):
    for _ in range(maximum):
        if job["status"] in {"waiting_human", "succeeded", "failed", "stopped"}:
            return job
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("staged flow failed to converge")


def test_complete_public_compile_flow_assembles_canonical_bundle_and_readonly_checkpoint(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        job = await drive(owner, await start(owner, 17, "R" * 30000))
        assert job["status"] == "waiting_human" and job["candidate_count"] == 1
        root = owner.repo.workspace(job)
        dataset = json.loads((root / "compiled_skill/refund-review/benchmark.json").read_text())
        assert len(dataset["cases"]) == 12 and (root / "compiled_skill/refund-review/BENCHMARK.md").is_file()
        assert job["materialized"] and not job["artifact_quality"]["can_submit"]
        assert len(owner.ov.accepted) < 100
        assert all("p.expand" not in body for body in owner.ov.skills.values())

    asyncio.run(check())


def test_lost_ack_restart_reuses_the_exact_request_and_recovers_before_writing(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.lose_ack = True
        job = await start(owner)
        assert not job["ov_task_id"] and len(owner.ov.accepted) == 1
        frozen = copy.deepcopy(job["compile_request"])
        second = Owner(tmp_path)
        second.ov = owner.ov
        job = await second._resume_staged_round(job)
        assert job["ov_task_id"] and job["compile_request"] == frozen and len(second.ov.calls) == 1

    asyncio.run(check())


def test_stop_after_unknown_acceptance_recovers_id_but_never_submits_a_new_phase(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.lose_ack = True
        job = await start(owner)
        job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user")
        job = await owner._resume_staged_round(job)
        assert job["status"] == "stopping" and job["ov_task_id"]
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "stopped" and len(owner.ov.calls) == 1

    asyncio.run(check())


def test_case_evidence_deferral_removes_candidate_without_padding_conditions(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.defer_case = True
        job = await drive(owner, await start(owner))
        assert job["status"] == "waiting_human" and job["candidate_count"] == 0
        assert not list((owner.repo.workspace(job) / "compiled_skill").iterdir())
        assert job["capability_plan"]["deferred"] and job["pending_checkpoint"]["questions"]

    asyncio.run(check())


def test_deferral_replayed_after_plan_write_before_state_commit_is_idempotent(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.defer_case = True
        job = await start(owner)
        while (await owner._staged_load(job))[1]["phase"] != "case":
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        root, state = await owner._staged_load(job)
        durable_state = (root / "state.json").read_text()
        await owner._staged_defer_candidate(job, root, state, "refund-review", "Evidence 无法支持十二个独立检查项")
        assert (root / "state.json").read_text() == durable_state
        assert json.loads((root / "plan.json").read_text())["skills"] == []
        restarted = Owner(tmp_path)
        restarted.ov = owner.ov
        job = await drive(restarted, job)
        assert job["status"] == "waiting_human" and job["candidate_count"] == 0
        assert len(job["capability_plan"]["deferred"]) == 1
        assert len(list((root / "deferred-candidates").glob("*.json"))) == 1

    asyncio.run(check())


def test_future_phase_compilers_are_frozen_before_the_first_submission(tmp_path, monkeypatch):
    async def check():
        owner = Owner(tmp_path)
        job = await start(owner)

        def changed_compiler(*args, **kwargs):
            raise AssertionError("active round must not rebuild a future phase from changed code")

        monkeypatch.setattr("team_miner.ov_staged_orchestration.stage_compiler_body", changed_compiler)
        job = await drive(owner, job)
        assert job["status"] == "waiting_human" and job["candidate_count"] == 1
        root, state = await owner._staged_load(job)
        assert set(state["compilers"]) == {"extract", "merge_index", "plan", "skill_section", "evaluation", "case"}
        assert all((root / "compilers" / f"{phase}.md").is_file() for phase in state["compilers"])

    asyncio.run(check())


def test_staged_budget_grows_for_eight_candidates_and_restarts_from_the_next_round_base(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.candidate_count = 8
        job, entries = owner.new(8)
        first_started = "2026-10-12T08:00:00Z"
        job = owner.repo.update_job(
            job["job_id"], runtime_timeout_seconds=60, max_rounds=2, round_started_at=first_started
        )
        language = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
        job = await owner._prepare_staged_round(job, SKILL_BODY, entries, language, "")
        initial_budget = job["round_timeout_seconds"]
        assert initial_budget == 3 * 180 and job["round_work_units"] == 3
        while (await owner._staged_load(job))[1]["phase"] != "skill_section":
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["round_work_units"] >= 4 + 8 * (1 + 1 + 12 + 2)
        assert job["round_timeout_seconds"] == job["round_work_units"] * 180 > initial_budget
        assert job["round_started_at"] == first_started and job["runtime_timeout_seconds"] == 60
        full_budget = job["round_timeout_seconds"]
        job = await drive(owner, job)
        assert job["round_timeout_seconds"] == full_budget
        job = owner.repo.prepare_next_round(job["job_id"], job["pending_checkpoint"]["id"], {}, [], {})
        second_started = "2026-10-12T09:00:00Z"
        job = owner.repo.update_job(job["job_id"], round_started_at=second_started)
        owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        owner.ov.candidate_count = 1
        job = await owner._prepare_staged_round(job, SKILL_BODY, entries, language, "")
        assert job["current_round"] == 2 and job["round_timeout_seconds"] == initial_budget < full_budget
        job = await drive(owner, job)
        assert job["round_work_units"] == 4 + 1 + 1 + 12 + 2
        assert job["round_timeout_seconds"] == 20 * 180
        assert all(
            json.loads(owner.ov.contents[request["from"][0] + "/packet.json"])["stage"] != "evidence_review"
            for _, request in owner.ov.calls
        )
        assert job["round_started_at"] == second_started and job["runtime_timeout_seconds"] == 60

    asyncio.run(check())


def test_budget_accounts_for_failed_attempts_without_inflating_same_request_retries():
    job = {"runtime_timeout_seconds": 60, "round_work_units": 1000000, "round_timeout_seconds": 999999999}
    state = {"estimated_tasks": 10, "completed_tasks": 2, "attempted_tasks": 5}
    first = _staged_execution_budget(job, state)
    assert first == {"round_work_units": 12, "round_timeout_seconds": 12 * 180}
    assert _staged_execution_budget(job, state) == first
    state.update(complete=True, estimated_tasks=3, completed_tasks=3)
    assert _staged_execution_budget(job, state) == first


def reference_index(refs):
    return {
        "schema_version": planning.INDEX_SCHEMA,
        "input_ids": ["src-a", "src-b"],
        "methods": [
            {"task": "金额复核", "boundary": "9000元低于12000元，保留原始例外。", "source_refs": [refs[0]]},
            {"task": "期限复核", "boundary": "按原文期限，不推测缺失日期。", "source_refs": [refs[1]]},
        ],
        "facts": [],
        "deferred": [],
    }


def checked_reference_index(value, reference_map=None, evidence=None):
    input_ids, source_refs = ["src-a", "src-b"], ["unit-a", "unit-b"]
    canonical = _canonical_extract_references(
        value,
        input_ids=input_ids,
        source_refs=source_refs,
        reference_map=reference_map if reference_map is not None else dict(zip(source_refs, input_ids)),
        evidence=evidence,
    )
    return planning.validate_index(canonical, input_ids=input_ids, source_refs=source_refs)


@pytest.mark.parametrize("refs", [["src-a", "src-b"], ["unit-a", "unit-b"], ["src-a", "unit-b"]])
def test_exact_extract_identifiers_canonicalize_without_changing_facts_or_lineage(refs):
    value = reference_index(refs)
    original = copy.deepcopy(value)
    canonical = checked_reference_index(value)
    assert value == original and canonical == reference_index(["unit-a", "unit-b"])
    unit = _bind_unit(canonical, {"unit-a": [1, 4], "unit-b": [2, 8]}, "assigned")
    for row, expected in zip(unit["value"]["methods"], [[1, 4], [2, 8]]):
        assert unit["bindings"][row["source_refs"][0]] == expected


@pytest.mark.parametrize("failure", ["foreign", "uri", "prefix", "missing"])
def test_extract_reference_canonicalization_keeps_unknown_duplicate_and_coverage_rejections(failure):
    value = reference_index(["src-a", "src-b"])
    if failure in {"foreign", "uri", "prefix"}:
        value["methods"][0]["source_refs"] = [
            {"foreign": "src-unassigned", "uri": "viking://resources/source-a", "prefix": "src-a-extra"}[failure]
        ]
    elif failure == "missing":
        value["methods"] = value["methods"][:1]
    with pytest.raises(ValueError):
        checked_reference_index(value)


def test_equivalent_assigned_pointers_are_stably_deduplicated_while_direct_validation_stays_strict():
    value = reference_index(["src-a", "src-b"])
    value["methods"][0]["source_refs"] = ["src-a", "unit-a", "src-a", "unit-a"]
    original = copy.deepcopy(value)
    assert checked_reference_index(value) == reference_index(["unit-a", "unit-b"])
    assert value == original
    duplicate = reference_index(["unit-a", "unit-b"])
    duplicate["methods"][0]["source_refs"] = ["unit-a", "unit-a"]
    with pytest.raises(ValueError, match="References"):
        planning.validate_index(duplicate, input_ids=["src-a", "src-b"], source_refs=["unit-a", "unit-b"])


def test_exact_original_file_uri_selects_only_all_ranges_of_that_file_in_the_current_packet():
    uri = "viking://resources/frozen/source-a.md"
    evidence = [{"id": "src-a", "source_uri": uri}, {"id": "src-b", "source_uri": uri}]
    value = reference_index([uri, "src-b"])
    value["methods"][0]["source_refs"] = [uri, "unit-b", "src-a", uri]
    canonical = checked_reference_index(value, evidence=evidence)
    assert canonical["methods"][0]["source_refs"] == ["unit-a", "unit-b"]
    assert canonical["methods"][1]["source_refs"] == ["unit-b"]
    unit = _bind_unit(canonical, {"unit-a": [1], "unit-b": [4]}, "assigned")
    assert unit["ordinals"] == [1, 4]
    assert unit["bindings"][unit["value"]["methods"][0]["source_refs"][0]] == [1, 4]


def test_control_unit_identity_cannot_be_used_as_a_row_evidence_scope():
    unit = _bind_unit(reference_index(["unit-a", "unit-b"]), {"unit-a": [1], "unit-b": [4]}, "assigned")
    assert unit["id"].startswith("unit-")
    scopes = list(unit["bindings"])
    assert all(ref.startswith("scope-") for ref in scopes) and unit["id"] not in scopes
    candidate = {
        "name": "refund-review",
        "task": "退款复核",
        "trigger": "收到申请",
        "inputs": ["申请"],
        "outputs": ["复核结论"],
        "boundary": "仅文档内规则",
        "rationale": "可复用的方法",
        "source_refs": scopes,
    }
    plan = {"schema_version": planning.PLAN_SCHEMA, "input_ids": [unit["id"]], "skills": [candidate], "deferred": []}
    planning.validate_plan(plan, source_refs=scopes, input_ids=[unit["id"]])
    candidate["source_refs"] = [unit["id"]]
    with pytest.raises(ValueError, match="References"):
        planning.validate_plan(plan, source_refs=scopes, input_ids=[unit["id"]])


def test_source_metadata_canonicalization_cannot_bypass_the_canonical_business_output_size_limit():
    value = reference_index(["src-a", "src-b"])
    value["facts"] = [{"text": "原始事实" * 237, "source_refs": ["src-a"]} for _ in range(16)]
    assert 12000 < len(json.dumps(value, ensure_ascii=False)) < 48000
    with pytest.raises(ValueError, match="12000"):
        checked_reference_index(value)


@pytest.mark.parametrize(
    "unknown", ["source-a.md", "viking://resources/other/source-a.md", "viking://resources/frozen"]
)
def test_original_uri_mapping_does_not_match_basenames_other_files_or_directories(unknown):
    evidence = [
        {"id": "src-a", "source_uri": "viking://resources/frozen/source-a.md"},
        {"id": "src-b", "source_uri": "viking://resources/frozen/source-b.md"},
    ]
    with pytest.raises(ValueError, match="References"):
        checked_reference_index(reference_index([unknown, "unit-b"]), evidence=evidence)


def test_uri_mapping_requires_the_exact_frozen_chunk_assignment():
    evidence = [
        {"id": "src-a", "source_uri": "viking://resources/frozen/source-a.md"},
        {"id": "src-other", "source_uri": "viking://resources/frozen/source-b.md"},
    ]
    with pytest.raises(ValueError, match="exact frozen"):
        checked_reference_index(reference_index(["unit-a", "unit-b"]), evidence=evidence)


@pytest.mark.parametrize("metadata", [None, ["0" * 32, "f" * 32], ["src-a"], ["src-b", "src-a"], "not-a-source"])
def test_extract_bookkeeping_is_derived_from_frozen_assignment_only_when_row_coverage_is_complete(metadata):
    value = reference_index(["src-a", "unit-b"])
    if metadata is None:
        value.pop("input_ids")
    else:
        value["input_ids"] = metadata
    original = copy.deepcopy(value)
    checked = checked_reference_index(value)
    assert checked["input_ids"] == ["src-a", "src-b"] and value == original
    value["methods"] = value["methods"][:1]
    with pytest.raises(ValueError, match="Every assigned source"):
        checked_reference_index(value)


@pytest.mark.parametrize(
    "reference_map",
    [
        {"unit-a": "src-a"},
        {"unit-a": "src-a", "unit-b": "src-a"},
        {"unit-a": "src-b", "unit-b": "src-a"},
        {"unit-a": "src-a", "unit-b": "src-b", "unit-other": "src-other"},
    ],
)
def test_extract_requires_the_exact_frozen_bijective_mapping(reference_map):
    with pytest.raises(ValueError, match="bijectively"):
        checked_reference_index(reference_index(["src-a", "src-b"]), reference_map)


def test_extract_rejects_ambiguous_raw_identifiers_colliding_with_other_allowed_aliases():
    with pytest.raises(ValueError, match="ambiguous"):
        _canonical_extract_references(
            reference_index(["unit-a", "unit-b"]),
            input_ids=["unit-b", "unit-a"],
            source_refs=["unit-a", "unit-b"],
            reference_map={"unit-a": "unit-b", "unit-b": "unit-a"},
        )


def test_raw_chunk_identifiers_are_accepted_without_retry_or_rewriting_frozen_audit(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.extract_raw_refs = True
        job = await drive(owner, await start(owner, 17))
        assert job["status"] == "waiting_human" and job["candidate_count"] == 1
        root = owner._staged_root(job)
        extract_children = list((root / "children").glob("extract-*"))
        assert len(extract_children) == 3
        for child in extract_children:
            metadata = json.loads((child / "metadata.json").read_text())
            packet_text = (child / "packet.json").read_text()
            packet = json.loads(packet_text)
            raw = (child / "result.txt").read_text()
            assert metadata["attempt"] == 0
            assert json.loads(raw)["methods"][0]["source_refs"] == packet["input_ids"]
            assert raw == owner.ov.contents[metadata["output_uri"] + "/index.json"]
            assert packet_text == owner.ov.contents[metadata["input_uri"] + "/packet.json"]

    asyncio.run(check())


def test_original_source_uris_are_accepted_without_retry_or_rewriting_frozen_audit(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.extract_uri_refs = True
        job = await drive(owner, await start(owner, 17))
        assert job["status"] == "waiting_human" and job["candidate_count"] == 1
        for child in (owner._staged_root(job) / "children").glob("extract-*"):
            metadata = json.loads((child / "metadata.json").read_text())
            packet_text = (child / "packet.json").read_text()
            packet = json.loads(packet_text)
            raw = (child / "result.txt").read_text()
            assert metadata["attempt"] == 0
            assert json.loads(raw)["methods"][0]["source_refs"] == [row["source_uri"] for row in packet["evidence"]]
            assert raw == owner.ov.contents[metadata["output_uri"] + "/index.json"]
            assert packet_text == owner.ov.contents[metadata["input_uri"] + "/packet.json"]

    asyncio.run(check())


@pytest.mark.parametrize("metadata_style", ["native_hashes", "omitted"])
def test_complete_extract_flow_ignores_model_bookkeeping_but_retains_unchanged_raw_audit(tmp_path, metadata_style):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.extract_input_ids_style = metadata_style
        job = await drive(owner, await start(owner, 17))
        assert job["status"] == "waiting_human" and job["candidate_count"] == 1
        root = owner._staged_root(job)
        extract_children = list((root / "children").glob("extract-*"))
        assert len(extract_children) == 3
        for child in extract_children:
            metadata = json.loads((child / "metadata.json").read_text())
            raw = (child / "result.txt").read_text()
            assert metadata["attempt"] == 0
            assert raw == owner.ov.contents[metadata["output_uri"] + "/index.json"]
            model_ids = json.loads(raw).get("input_ids")
            assert model_ids is None if metadata_style == "omitted" else len(model_ids) == 15
        units = sorted((root / "units" / "0").glob("*.json"))
        for unit, child in zip(
            units,
            sorted(
                extract_children,
                key=lambda path: min(json.loads((path / "metadata.json").read_text())["context"]["ordinals"]),
            ),
        ):
            assigned = json.loads((child / "packet.json").read_text())["input_ids"]
            assert json.loads(unit.read_text())["value"]["input_ids"] == assigned

    asyncio.run(check())


@pytest.mark.parametrize("kind", ["bad_case", "duplicate_case"])
def test_bad_or_duplicate_cases_stop_after_bounded_repair_with_no_canonical_dataset(tmp_path, kind):
    async def check():
        owner = Owner(tmp_path)
        setattr(owner.ov, kind, True)
        job = await drive(owner, await start(owner))
        assert job["status"] == "failed"
        assert not (owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json").exists()
        root = owner._staged_root(job)
        state = json.loads((root / "state.json").read_text())
        assert state["current"]["attempt"] == 2

    asyncio.run(check())


def test_row_scopes_keep_unrelated_originals_separate_through_multiple_layers():
    value = {
        "schema_version": planning.INDEX_SCHEMA,
        "input_ids": ["a", "b"],
        "facts": [],
        "deferred": [],
        "methods": [
            {"task": "任务甲", "boundary": "范围甲", "source_refs": ["a"]},
            {"task": "任务乙", "boundary": "范围乙", "source_refs": ["b"]},
        ],
    }
    first = _bind_unit(value, {"a": [0, 2], "b": [1, 3]}, "first")
    projected = _project(first["value"], first["id"])
    refs = [row["source_refs"][0] for row in projected["methods"]]
    assert refs[0] != refs[1] and first["bindings"][refs[0]] == [0, 2]
    second = _bind_unit(projected, first["bindings"], "second")
    assert second["bindings"][refs[0]] == [0, 2] and second["bindings"][refs[1]] == [1, 3]


@pytest.mark.parametrize("failure", ["missing_output", "extra_output", "partial"])
def test_completed_missing_extra_or_partial_output_is_bounded_repaired_not_transport_loop(tmp_path, failure):
    async def check():
        owner = Owner(tmp_path)
        setattr(owner.ov, failure, True)
        job = await drive(owner, await start(owner))
        assert job["status"] == "failed" and len(owner.ov.accepted) == 3
        assert json.loads((owner._staged_root(job) / "state.json").read_text())["current"]["attempt"] == 2
        assert not job.get("materialized")

    asyncio.run(check())


@pytest.mark.parametrize("status", [401, 403, 503])
def test_completed_content_auth_failure_is_terminal_but_unavailability_reuses_the_same_task(tmp_path, status):
    async def check():
        owner = Owner(tmp_path)
        job = await start(owner)
        task_id = job["ov_task_id"]
        owner.ov.read_status = status
        job = await owner._advance_staged_round(job, owner.ov.tasks[task_id])
        assert len(owner.ov.accepted) == 1 and job["ov_task_id"] == task_id
        if status in {401, 403}:
            assert job["status"] == "failed"
        else:
            assert job["status"] == "running"
            owner.ov.read_status = 0
            job = await owner._advance_staged_round(job, owner.ov.tasks[task_id])
            assert job["status"] == "queued" and job["ov_task_id"] != task_id

    asyncio.run(check())


@pytest.mark.parametrize("phase", ["skill_section", "case"])
def test_one_unrelated_source_group_does_not_discard_a_method_supported_by_other_groups(tmp_path, phase):
    async def check():
        owner = Owner(tmp_path)
        job = await start(owner, 17)
        while True:
            root, state = await owner._staged_load(job)
            if state["phase"] == phase:
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        current = state["current"]
        uri = current["output_uri"] + "/" + current["expected_path"]
        owner.ov.contents[uri] = json.dumps(
            {
                "schema_version": planning.DEFERRED_SCHEMA,
                "reason": "当前片段只有背景信息",
                "source_refs": current["context"]["source_refs"],
            },
            ensure_ascii=False,
        )
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        job = await drive(owner, job)
        assert job["status"] == "waiting_human" and job["candidate_count"] == 1
        assert not job["capability_plan"]["deferred"]
        if phase == "skill_section":
            first = json.loads((root / "sections/refund-review/00000000.json").read_text())
            assert first["schema_version"] == planning.SECTION_SKIP_SCHEMA
            assert (
                "当前片段只有背景信息"
                not in (owner.repo.workspace(job) / "compiled_skill/refund-review/SKILL.md").read_text()
            )
        else:
            assert len(list((root / "case-group-deferrals/refund-review").glob("*.json"))) == 1
        assert (owner.repo.workspace(job) / "semantic_reports/method-dispositions.md").is_file()

    asyncio.run(check())


def test_all_skipped_sections_defer_candidate_without_committing_partial_skill(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        job = await start(owner)
        while True:
            root, state = await owner._staged_load(job)
            if state["phase"] == "skill_section":
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        current = state["current"]
        owner.ov.contents[current["output_uri"] + "/" + current["expected_path"]] = json.dumps(
            {
                "schema_version": planning.SECTION_SKIP_SCHEMA,
                "input_ids": current["context"]["input_ids"],
                "reason": "完整来源只有背景信息，没有执行步骤",
            },
            ensure_ascii=False,
        )
        job = await drive(owner, await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]]))
        assert job["status"] == "waiting_human" and job["candidate_count"] == 0
        assert job["capability_plan"]["deferred"]
        workspace = owner.repo.workspace(job)
        assert not (workspace / "compiled_skill/refund-review").exists()
        assert not list(workspace.rglob(".te-stream-*"))

    asyncio.run(check())


def test_local_prior_catalog_projects_only_matching_assignments_and_preserves_own_case_ids(tmp_path):
    import hashlib

    root = tmp_path / "round-2/staged"
    folder = root.parent / "previous-candidates"
    folder.mkdir(parents=True)
    refs = "viking://resources/frozen/round-2/source"
    locators = []
    for index in range(20):
        name = f"previous-method-{index}"
        row = {
            "name": name,
            "case_ids": [f"custom-case-{index}-{case}" for case in range(12)],
            "task": "复核",
            "boundary": "原文范围",
            "source_refs": ["viking://resources/previous"],
            "scope_relative_paths": ["original/relevant.md" if index % 2 == 0 else "original/other.md"],
            "skill_body_excerpt": "旧的英文内容" * 5000,
            "skill_body_sha256": "a" * 64,
            "skill_body_path": "private-archive",
        }
        path = folder / f"{name}.json"
        text = json.dumps(row, ensure_ascii=False)
        path.write_text(text)
        locators.append({"name": name, "local_path": str(path), "sha256": hashlib.sha256(text.encode()).hexdigest()})
    (folder / "manifest.json").write_text(json.dumps(locators))
    context = {"source_refs": [refs + "/original/relevant.md"]}
    view = json.loads(_assignment_revision(root, {"source_snapshot": refs}, {}, context, 6000))
    assert view["other_candidates_omitted"] and len(view["assignment_previous_candidates"]) == 8
    assert all(int(row["name"].rsplit("-", 1)[-1]) % 2 == 0 for row in view["assignment_previous_candidates"])
    context["skill_name"] = "previous-method-2"
    own = json.loads(_assignment_revision(root, {"source_snapshot": refs}, {}, context, 6000))
    row = own["assignment_previous_candidates"][0]
    assert row["case_ids"] == [f"custom-case-2-{case}" for case in range(12)]
    assert own["revision_context_is_evidence"] is False and row["skill_body_excerpt_truncated"]


def test_two_round_flow_preserves_reviewed_custom_case_ids_through_final_assembly(tmp_path):
    async def check():
        from team_miner.ov_round_orchestration import OvRoundOrchestrationMixin

        owner = Owner(tmp_path)
        job, entries = owner.new()
        first_manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        first_manifest.write_text(json.dumps(entries), encoding="utf-8")
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(first_manifest))
        policy = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, policy, ""))
        assert job["status"] == "waiting_human" and job["current_round"] == 1
        dataset_path = owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json"
        document = json.loads(dataset_path.read_text())
        custom_ids = [f"reviewed-refund:{index:03d}" for index in range(12)]
        for case, case_id in zip(document["cases"], custom_ids, strict=True):
            case["case_id"] = case_id
        # The human-reviewed archive is the authoritative prior identity source.
        dataset_path.write_text(json.dumps(document, ensure_ascii=False), encoding="utf-8")
        job = owner.repo.prepare_next_round(
            job["job_id"], job["pending_checkpoint"]["id"], answers={}, unresolved=[], references={}
        )
        second_entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, job, staged=True)
        job_dir = owner.repo._job_dir(job["job_id"])
        job = owner.repo.update_job(job["job_id"], input_manifest=str(job_dir / "round_inputs/round-2/manifest.json"))
        prior = json.loads((job_dir / "round_inputs/round-2/previous-candidates/refund-review.json").read_text())
        assert prior["case_ids"] == custom_ids
        owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        revision = OvRoundOrchestrationMixin._staged_revision_instruction(job, 1)
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, second_entries, policy, revision))
        assert job["status"] == "waiting_human" and job["current_round"] == 2
        dataset_path = owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json"
        result = json.loads(dataset_path.read_text())
        assert [case["case_id"] for case in result["cases"]] == custom_ids
        assert len(result["cases"]) == 12
        second_root = job_dir / "round_inputs/round-2/staged"
        packets = [json.loads(path.read_text()) for path in second_root.glob("children/*/packet.json")]
        case_packets = [packet for packet in packets if packet["stage"] == "case"]
        assert len(case_packets) == 12
        assert {packet["derived_controls"]["case_id"] for packet in case_packets} == set(custom_ids)
        finished = owner.repo.finish_job(job["job_id"], job["pending_checkpoint"]["id"])
        assert finished["status"] == "succeeded" and len(finished["rounds_history"]) == 2
        assert json.loads(dataset_path.read_text()) == result

    asyncio.run(check())


def test_a_later_name_conflict_does_not_partially_write_an_earlier_candidate_scope(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.candidate_count = 8
        job = await start(owner)
        while True:
            root, state = await owner._staged_load(job)
            if state["phase"] == "selection":
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        current = state["current"]
        raw = json.loads(owner.ov.contents[current["output_uri"] + "/" + current["expected_path"]])
        prior = {key: value for key, value in raw["skills"][1].items() if key != "derived_from"}
        prior["task"] = "已经用于另一种操作方法"
        prior["source_refs"] = [job["source_snapshot"] + "/original/doc-0.md"]
        plan = {"schema_version": planning.PLAN_SCHEMA, "skills": [prior], "deferred": []}
        (root / "plan.json").write_text(json.dumps(plan, ensure_ascii=False))
        original = (root / "plan.json").read_text()
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "queued"
        assert (root / "plan.json").read_text() == original
        assert not (root / "scopes/refund-review-0.json").exists()
        assert json.loads((root / "state.json").read_text())["current"]["attempt"] == 1

    asyncio.run(check())


@pytest.mark.parametrize("header", [["unit-a", "unit-b"], ["item-c", "item-a", "item-b"]])
def test_complete_planning_bookkeeping_header_is_canonicalized_without_changing_dispositions(header):
    value = {"input_ids": header, "skills": [{"derived_from": ["item-a"], "task": "Exact original text"}]}
    original = copy.deepcopy(value)
    accepted = _canonical_planning_header(
        value, input_ids=["unit-a", "unit-b"], item_ids=["item-a", "item-b", "item-c"]
    )
    assert accepted["input_ids"] == ["unit-a", "unit-b"] and accepted["skills"] == value["skills"]
    assert value == original


@pytest.mark.parametrize(
    "header",
    [
        [],
        ["item-a"],
        ["item-a", "item-b", "item-c", "item-c"],
        ["unit-a", "item-a"],
        ["readonly-fact"],
        ["unknown"],
        "item-a",
    ],
)
def test_unknown_partial_duplicate_or_context_planning_headers_are_rejected(header):
    with pytest.raises(ValueError, match="exactly cover"):
        _canonical_planning_header(
            {"input_ids": header}, input_ids=["unit-a", "unit-b"], item_ids=["item-a", "item-b", "item-c"]
        )


def test_merge_and_selection_accept_exact_method_headers_and_preserve_original_raw(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        owner.ov.candidate_count = 8
        job = await start(owner)
        originals = []
        while job["status"] not in {"waiting_human", "failed"}:
            root, state = await owner._staged_load(job)
            current = state["current"]
            if current["context"].get("typed_planning"):
                packet = json.loads((root / current["packet_file"]).read_text())
                uri = current["output_uri"] + "/" + current["expected_path"]
                output = json.loads(owner.ov.contents[uri])
                output["input_ids"] = [row["item_id"] for row in packet["derived_controls"]["items"]]
                raw = json.dumps(output, ensure_ascii=False)
                owner.ov.contents[uri] = raw
                originals.append((root / "children" / current["child"] / "result.txt", raw))
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "waiting_human" and job["candidate_count"] == 8
        assert len(originals) == 2
        assert all(path.read_text() == raw for path, raw in originals)
        assert len(owner.ov.accepted) == json.loads((root / "state.json").read_text())["completed_tasks"]

    asyncio.run(check())


def test_readonly_planning_notes_expose_no_disposition_identities():
    item = {
        "item_id": "readonly-fact",
        "kind": "fact",
        "text": "原文中有退款规则。",
        "source_refs": ["scope-secret"],
        "source_files": ["viking://resources/file.md"],
        "derived_from": ["parent-id"],
        "unit_id": "unit-id",
    }
    original = copy.deepcopy(item)
    note = OvStagedOrchestrationMixin._staged_readonly_note({"item": item})
    assert set(note) == {"text", "source_file_ids", "readonly"} and note["readonly"]
    assert note["text"] == item["text"] and len(note["source_file_ids"]) == 1
    assert item == original


def test_actual_selection_packet_separates_method_catalog_from_readonly_fact_and_gap_notes(tmp_path):
    async def check():
        owner = Owner(tmp_path)
        job = await start(owner)
        root, state = await owner._staged_load(job)
        current = state["current"]
        uri = current["output_uri"] + "/" + current["expected_path"]
        index = json.loads(owner.ov.contents[uri])
        refs = index["methods"][0]["source_refs"]
        index["facts"] = [{"text": "该政策版本只用于原文范围。", "source_refs": refs}]
        index["deferred"] = [{"reason": "仅本片段未列出发布日期，需要全文核对。", "source_refs": refs}]
        owner.ov.contents[uri] = json.dumps(index, ensure_ascii=False)
        while True:
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
            root, state = await owner._staged_load(job)
            if state["phase"] == "selection":
                break
        packet = json.loads((root / state["current"]["packet_file"]).read_text())
        controls = packet["derived_controls"]
        assert len(controls["items"]) == 1 and controls["items"][0]["item_id"]
        for field, content_field in (("related_facts", "text"), ("provisional_gaps", "reason")):
            assert len(controls[field]) == 1
            assert set(controls[field][0]) == {content_field, "source_file_ids", "readonly"}
        fact = json.loads((root / "catalog/fact/00000000.json").read_text())
        assert fact["item"]["item_id"] and fact["bindings"]  # The private lineage was never stripped.
        job = await drive(owner, job)
        assert job["candidate_count"] == 1 and not job["capability_plan"]["deferred"]

    asyncio.run(check())


def test_extra_readonly_dispositions_are_rejected_even_with_a_valid_method_header():
    unit = _bind_unit(
        {"methods": [{"task": "复核", "boundary": "仅原文范围", "source_refs": ["raw"]}]}, {"raw": [0]}, "fixture"
    )
    items = planning.planning_items([unit])
    value = {
        "schema_version": planning.SELECTION_SCHEMA,
        "input_ids": [items[0]["item_id"]],
        "skills": [],
        "knowledge": [
            {"reason": "查表", "derived_from": [items[0]["item_id"]]},
            {"reason": "只读信息不能消费", "derived_from": ["readonly-fact"]},
        ],
        "deferred": [],
        "resolved": [],
    }
    canonical = _canonical_planning_header(value, input_ids=[unit["id"]], item_ids=[items[0]["item_id"]])
    with pytest.raises(ValueError, match=r'Planning dispositions.*unknown=\["readonly-fact"\]'):
        planning.validate_selection(canonical, items=items, input_ids=[unit["id"]])
