# ruff: noqa: F401
"""Exercise unchanged public Compile transport and durable TE phase control in private tmp directories."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest
from starlette.exceptions import HTTPException

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT, SKILL_BODY, OvSkillMiningRepository
from team_miner.skill_miner.ov_staged_orchestration import (
    OvStagedOrchestrationMixin,
    _assignment_revision,
    _bind_unit,
    _canonical_extract_references,
    _canonical_planning_header,
    _project,
    _staged_execution_budget,
)
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_document


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
        self.repo = OvSkillMiningRepository(root / "state", root, backend=MemoryMetadataBackend.shared(root))
        self.ov = FakeCompile()

    async def _acquire_staged_compile_slot(self, job, state, current):
        return True

    async def _mark_staged_compile_slot(self, job, current):
        pass

    async def _release_staged_compile_slot(self, job, task_id="", *, key=""):
        pass

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
