# ruff: noqa: F401
"""Exercise bounded TE planning with inert Compile results and complete source Evidence."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from team_miner.skill_miner import bounded_compile_plan as staged
from team_miner.skill_miner import engine_polling as mining_bridge
from team_miner.skill_miner import ov_round_orchestration
from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT, OvSkillMiningRepository
from team_miner.skill_miner.ov_staged_orchestration import _revision_view
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend
from tests.miner.skill_miner.support.ov_round_bridge import FakeCompileClient, FakeOV, Owner

LANGUAGE = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}

def original_documents(count: int) -> dict[str, str]:
    """Include long tables and adjacent conditions, rather than empty count-only fixtures."""
    return {
        f"viking://resources/team/source/document-{index:04d}.md": (
            f"# 原始 Evidence {index}\n\n"
            "执行前核对来源中的地区、单位、阈值与例外。\n"
            "| 输入条件 | 必须保留的处理要求 |\n| --- | --- |\n"
            + "| 北京地区，人民币9000元 | 与12000元阈值比较，保留3000元差额；缺少地区时暂缓。 |\n" * 310
            + f"\n原始文档 {index} 的结束条件：不能从相邻文档推断本文件缺失的事实。\n"
        )
        for index in range(count)
    }

def all_chunks(documents: dict[str, str]) -> list[dict]:
    return [chunk for uri, text in documents.items() for chunk in staged.source_chunks(text, uri)]

def candidate_plan(source_uri: str, *, names: tuple[str, ...] = ("policy-review",)) -> dict:
    return {
        "schema_version": "skill-miner.plan.v1",
        "skills": [
            {
                "name": name,
                "task": "核验申报金额与地区条件",
                "trigger": "收到带金额和地区的申请",
                "inputs": ["申请及原始 Evidence"],
                "outputs": ["有来源的核验结果"],
                "boundary": "只适用原始条款规定的地区与条件",
                "rationale": "该方法有独立输入、输出及验证边界",
                "source_refs": [source_uri],
            }
            for name in names
        ],
        "deferred": [] if names else [{"reason": "原文只有事实，缺少可执行方法", "source_refs": [source_uri]}],
    }

def canonical_case(case_id: str, source_uri: str, *, skill_name: str = "policy-review") -> dict:
    """Explicit synthetic conditions are fixture content, never production padding."""
    scenario = int(case_id.rsplit("-", 1)[-1])
    amount = 9000 - (scenario - 1) * 100
    difference = 12000 - amount
    conditions = [
        "保留申请编号 TE-001。", "地区为北京。", "金额单位为人民币元。",
        f"申报金额为{amount}元。", "规则阈值为12000元。", f"{amount}元低于12000元。",
        f"阈值与申报金额的差额为{difference}元。", "申请日期为2026-10-12。",
        "申请人为条款规定的提交方。", "引用提供的原始条款URI。",
        "不将北京条款推广到未规定的地区。", "保留条款规定的人工确认步骤。",
    ]
    return {
        "case_id": case_id,
        "query": f"核验合成申请 TE-001：北京，人民币{amount}元，阈值12000元，日期2026-10-12；保留人工确认步骤。",
        "skill_ids": [skill_name],
        "checks": [
            {"id": f"out-{index:02d}", "kind": "output", "text": text}
            for index, text in enumerate(conditions, 1)
        ] + [{"id": "source-read", "kind": "trajectory", "text": "读取并核对提供的原始 Evidence。"}],
        "materials": [],
        "provenance": {"session_ids": [source_uri]},
        "replay": {
            "progressive_disclosure": {
                "enabled": True, "initial_visibility": "query_only", "batch_size": 4,
                "stop_when": "all_checklist_items_satisfied",
            },
        },
        "metadata": {"split": "test", "minimum_requirement_target": 12},
    }

class StagedCompileTransport(FakeOV):
    """Only implement the existing public API; model outputs are explicit inert fixtures."""

    def __init__(self, *, candidate_count=1, defer_case=False):
        super().__init__()
        self.lookup_supported = True
        self.candidate_names = tuple(f"policy-review-{index}" for index in range(candidate_count))
        self.defer_case = defer_case
        self.packets = []
        self.output_paths = []
        self.envelopes = []
        self.bad_case_attempts = 0

    def result(self, packet):
        controls = packet["derived_controls"]
        refs = controls.get("allowed_source_refs") or sorted({
            ref for item in controls.get("items", []) for ref in item["source_refs"]
        })
        stage = packet["stage"]
        if stage == "extract":
            return {
                "schema_version": "skill-miner.index.v1",
                "input_ids": packet["input_ids"],
                "methods": [
                    {"task": name, "boundary": "仅原始条款范围", "source_refs": refs}
                    for name in self.candidate_names
                ],
                "facts": [{"text": "9000元低于12000元，差额3000元", "source_refs": refs}],
                "deferred": [],
            }
        if stage == "merge_index":
            by_method = {}
            for item in controls["items"]:
                assert item["kind"] == "method"
                by_method.setdefault((item["task"], item["boundary"]), []).append(item["item_id"])
            return {
                "schema_version": staged.MERGE_SCHEMA, "input_ids": controls["input_ids"],
                "methods": [
                    {"task": task, "boundary": boundary, "derived_from": identities}
                    for (task, boundary), identities in by_method.items()
                ],
                "facts": [], "deferred": [], "resolved": [],
            }
        if stage == "plan":
            if "items" in controls:
                by_method = {}
                for item in controls["items"]:
                    assert item["kind"] == "method"
                    by_method.setdefault(item["task"], []).append(item["item_id"])
                skills, knowledge = [], []
                for task, identities in by_method.items():
                    if self.candidate_names:
                        name = task if re.fullmatch(r"[a-z0-9]+(?:-[a-z0-9]+)*", task) else self.candidate_names[0]
                        previous = next((row for row in skills if row["name"] == name), None)
                        if previous is not None:
                            previous["derived_from"].extend(identities)
                        else:
                            row = candidate_plan(refs[0], names=(name,))["skills"][0]
                            row.pop("source_refs")
                            skills.append({**row, "derived_from": identities})
                    else:
                        knowledge.extend(
                            {"reason": "合成场景只支持规则查表，没有独立操作流程", "derived_from": [identity]}
                            for identity in identities
                        )
                return {
                    "schema_version": controls.get("selection_schema", staged.SELECTION_SCHEMA),
                    "input_ids": controls["input_ids"],
                    "skills": skills, "knowledge": knowledge, "deferred": [], "resolved": [],
                }
            plan = candidate_plan(refs[0], names=self.candidate_names)
            for row in plan["skills"] + plan["deferred"]:
                row["source_refs"] = refs
            return {**plan, "input_ids": controls["input_ids"]}
        if stage == "identity_group":
            identities = controls["required_candidate_ids"]
            return {"schema_version": "skill-miner.identity-groups.v1", "candidate_ids": identities,
                    "groups": [{"candidate_ids": [identity], "reason": "Independent fixture workflow"}
                               for identity in identities]}
        if stage in {"evidence_review", "evidence_review_quotes"}:
            quote_ids = stage == "evidence_review_quotes"
            schema = "skill-miner.evidence-review-quotes.v1" if quote_ids else "skill-miner.evidence-review.v1"
            quote_field = "supporting_quote_ids" if quote_ids else "supporting_quotes"
            return {"schema_version": schema, "input_ids": packet["input_ids"],
                    "reviews": [{"item_id": item["item_id"], "outcome": "insufficient_this_fragment",
                                 "reason": item.get("task", "Fixture method") + " requires missing fixture conditions",
                                 "source_refs": refs, quote_field: [], "partial_steps": [], "candidate": None}
                                for item in controls["items"]]}
        if stage == "skill_section":
            return {
                "schema_version": "skill-miner.section.v1",
                "input_ids": packet["input_ids"],
                "title": "原始范围核验",
                "content": "核对提供的来源，保留其适用条件。\n\n" + "\n".join(packet["input_ids"]),
            }
        if stage == "evaluation":
            return "# 评测说明\n\n核对地区、金额与原始条款适用条件。"
        assert stage == "case"
        if self.defer_case:
            return {
                "schema_version": "skill-miner.deferred.v1",
                "reason": "当前原文不能支持十二条独立的完成条件，暂缓形成 Skill Candidate。",
                "source_refs": refs,
            }
        case = canonical_case(controls["case_id"], refs[0], skill_name=controls["skill_name"])
        if self.bad_case_attempts:
            self.bad_case_attempts -= 1
            case["checks"] = case["checks"][:3]
        return case

    async def request(self, account, method, path, **kwargs):
        if path != "/api/v1/compile" or method != "POST":
            return await super().request(account, method, path, **kwargs)
        request = kwargs["json"]
        assert set(request) == {"from", "to", "skill", "instruction"}
        assert len(request["from"]) == 1
        packet_uri = request["from"][0] + "/packet.json"
        assert self.tree(request["from"][0])["entries"] == [
            {"uri": packet_uri, "is_dir": False, "size": len(self.content[packet_uri].encode())}
        ]
        packet = json.loads(self.content[packet_uri])
        assert len(self.content[packet_uri]) <= 32_000
        assert packet["derived_controls_are_evidence"] is False
        compiler = self.compiler_skills[request["skill"].rsplit("/", 1)[-1]]
        plans = re.findall(r"```python\n(.*?)\n```", compiler, flags=re.DOTALL)
        assert plans == ["files = p.map(sources, task=contract.synthesize)\np.finalize(files, into=target)"]
        contract = json.loads(re.findall(r"```json\n(.*?)\n```", compiler, flags=re.DOTALL)[-1])
        assert set(contract) == {"extract", "synthesize", "output_format"}
        assert set(contract["extract"]) == {"instructions", "output"}
        assert set(contract["synthesize"]) == {"instructions", "output", "execution", "input_unit"}
        envelope = len(compiler) + len(self.content[packet_uri]) + len(request["instruction"])
        assert envelope <= 48_000
        key = kwargs["extra_headers"]["Idempotency-Key"]
        new = key not in self.accepted
        if new:
            result = self.result(packet)
            output = (result if isinstance(result, str)
                      else json.dumps(result, ensure_ascii=False, separators=(",", ":")))
            assert len(output) <= (48_000 if packet["stage"] == "extract" else 12_000)
            self.content[request["to"] + "/" + packet["output_path"]] = output
            self.packets.append(copy.deepcopy(packet))
            self.output_paths.append(packet["output_path"])
            self.envelopes.append(envelope)
        try:
            accepted = await super().request(account, method, path, **kwargs)
        finally:
            if key in self.accepted:
                self.tasks[self.accepted[key][0]] = {"status": "completed", "stage": "compile: completed"}
        return accepted

class StagedOwner(Owner):
    def _staged_cases_batched(self):
        # Preserve legacy single-Case freeze/resume fixture semantics; batches have dedicated coverage.
        return False

    def __init__(self, repository, remote):
        super().__init__(repository, remote)
        self.compiler_body = ov_round_orchestration.SKILL_BODY

    async def _complete_ov_skill_round(self, job):
        return self.repository.update_job(job["job_id"], status="succeeded")

def staged_rig(tmp_path, monkeypatch, *, candidate_count=1, defer_case=False):
    remote = StagedCompileTransport(candidate_count=candidate_count, defer_case=defer_case)
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner",
        backend=MemoryMetadataBackend.shared(tmp_path / "state"))
    monkeypatch.setattr(ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(remote, **kwargs))
    owner = StagedOwner(repository, remote)
    return owner

def frozen_job(owner, tmp_path, documents, *, human_text=""):
    job = owner.repository.create_job(
        name="bounded-fixture", account="default", source_uri="viking://resources/team/source",
        source_snapshot_uri=f"{RUNS_ROOT}/pending/source", output_uri=f"{RUNS_ROOT}/pending/output",
        document_count=len(documents), max_rounds=1, compiler_body=owner.compiler_body,
    )
    # This transport exercises the existing frozen single-packet protocol;
    # fresh optimized Map batches have their own real directory transport tests.
    # This fixture exercises frozen single-packet requests; fresh efficient batches have their own HTTP fixture.
    job = owner.repository.update_job(job["job_id"], staged_execution_protocol="", staged_efficiency_protocol="")
    sources = list(documents.items())
    if human_text:
        sources.append(("human/addendum.md", human_text))
    entries = []
    for index, (_, text) in enumerate(sources):
        path = tmp_path / "frozen" / f"document-{index:04d}.md"
        path.parent.mkdir(exist_ok=True)
        path.write_text(text, encoding="utf-8")
        entries.append({
            "local_path": str(path), "sha256": hashlib.sha256(text.encode()).hexdigest(),
            "relative_path": f"original/document-{index:04d}.md" if index < len(documents) else "human/addendum.md",
            "kind": "original" if index < len(documents) else "human",
        })
    return job, entries

async def finish_staged(owner, job, *, limit=2500):
    for _ in range(limit):
        if job["status"] in {"succeeded", "failed", "stopped"}:
            return job
        if not job.get("ov_task_id"):
            job = await owner._resume_staged_round(job)
        else:
            job = await owner._advance_staged_round(job, owner.remote.tasks[job["ov_task_id"]])
    raise AssertionError("The finite staged queue did not finish")

class ScopedCompileTransport(StagedCompileTransport):
    """Two unrelated methods share packets, but their original ranges must stay separate."""

    def result(self, packet):
        controls = packet["derived_controls"]
        if packet["stage"] == "extract":
            aliases = {chunk_id: alias for alias, chunk_id in controls["reference_map"].items()}
            methods = []
            for chunk in packet["evidence"]:
                original_index = int(chunk["source_uri"].rsplit("-", 1)[-1].split(".", 1)[0])
                name = "region-a" if original_index % 2 == 0 else "region-b"
                methods.append({"task": name, "boundary": "只适用该原始条款", "source_refs": [aliases[chunk["id"]]]})
            return {
                "schema_version": "skill-miner.index.v1", "input_ids": packet["input_ids"],
                "methods": methods, "facts": [], "deferred": [],
            }
        if packet["stage"] == "plan":
            if "items" in controls:
                return super().result(packet)
            by_name = {}
            for unit in controls["inputs"]:
                value = unit["value"]
                for row in value.get("methods", value.get("skills", [])):
                    name = row.get("name", row["task"])
                    by_name.setdefault(name, []).extend(row["source_refs"])
            rows = []
            for name, refs in sorted(by_name.items()):
                row = candidate_plan(refs[0], names=(name,))["skills"][0]
                rows.append({**row, "task": name, "source_refs": list(dict.fromkeys(refs))})
            return {
                "schema_version": "skill-miner.plan.v1", "input_ids": controls["input_ids"],
                "skills": rows, "deferred": [],
            }
        return super().result(packet)

class TypedDispositionCompileTransport(StagedCompileTransport):
    """Thirty-eight explicit proposals include supported workflows, lookup and genuine gaps."""

    method_counts = (2, 10, 6, 7, 6, 7)

    def __init__(self):
        super().__init__(candidate_count=3)
        self.candidate_names = tuple(f"execution-workflow-{index}" for index in range(3))

    def result(self, packet):
        controls = packet["derived_controls"]
        if packet["stage"] == "extract":
            aliases = {chunk_id: alias for alias, chunk_id in controls["reference_map"].items()}
            by_document = {}
            for chunk in packet["evidence"]:
                document = int(chunk["source_uri"].rsplit("-", 1)[-1].split(".", 1)[0])
                by_document.setdefault(document, []).append(aliases[chunk["id"]])
            methods = []
            for document, refs in by_document.items():
                start = sum(self.method_counts[:document])
                for index in range(start, start + self.method_counts[document]):
                    task = (f"execution-workflow-{index % 3}-step-{index:02d}" if index < 20
                            else f"lookup-{index:02d}" if index < 33 else f"unsupported-{index:02d}")
                    methods.append({"task": task, "boundary": "仅核验该文件的明确操作范围", "source_refs": refs})
            return {
                "schema_version": staged.INDEX_SCHEMA, "input_ids": packet["input_ids"],
                "methods": methods, "facts": [], "deferred": [],
            }
        if packet["stage"] == "plan" and "items" in controls:
            skills, knowledge, deferred = [], [], []
            workflows = {}
            for item in controls["items"]:
                assert item["kind"] == "method"
                task = item["task"]
                if task.startswith("execution-workflow-"):
                    workflows.setdefault(task.split("-step-", 1)[0], []).append(item["item_id"])
                else:
                    target = knowledge if task.startswith("lookup-") else deferred
                    reason = (f"{task}仅提供代码查表，没有执行流程" if target is knowledge
                              else f"{task}所需的操作步骤确实没有提供")
                    target.append({"reason": reason, "derived_from": [item["item_id"]]})
            for name, identities in workflows.items():
                row = candidate_plan("fixture-reference", names=(name,))["skills"][0]
                row.pop("source_refs")
                skills.append({**row, "derived_from": identities})
            return {
                "schema_version": controls.get("selection_schema", staged.SELECTION_SCHEMA),
                "input_ids": controls["input_ids"],
                "skills": skills, "knowledge": knowledge, "deferred": deferred, "resolved": [],
            }
        return super().result(packet)

class SparseMethodCompileTransport(StagedCompileTransport):
    marker = "SOURCE_METHOD_MARKER：末尾明确给出地区、金额核验步骤与人工确认条件。"

    def result(self, packet):
        text = "".join(chunk["content"] for chunk in packet["evidence"])
        if packet["stage"] == "extract" and self.marker not in text:
            return {
                "schema_version": staged.INDEX_SCHEMA, "input_ids": packet["input_ids"],
                "methods": [], "deferred": [],
                "facts": [{"text": "此前片段只有版本背景，与末尾操作流程无关。",
                           "source_refs": packet["derived_controls"]["allowed_source_refs"]}],
            }
        if packet["stage"] == "case" and self.marker not in text:
            return {
                "schema_version": staged.DEFERRED_SCHEMA,
                "reason": "该片段只有背景，不足以支持这个操作流程的独立场景；不能据此推断整份文件缺少方法。",
                "source_refs": packet["derived_controls"]["allowed_source_refs"],
            }
        return super().result(packet)

class ManyScopesCompileTransport(StagedCompileTransport):
    def result(self, packet):
        if packet["stage"] != "extract":
            return super().result(packet)
        refs = packet["derived_controls"]["allowed_source_refs"]
        assert len(refs) == 8
        masks = [*range(1, 64), 255]
        scopes = [[ref for index, ref in enumerate(refs) if mask & (1 << index)] for mask in masks]
        return {
            "schema_version": "skill-miner.index.v1", "input_ids": packet["input_ids"],
            "methods": [{"task": "流程", "boundary": "适用条件", "source_refs": scope} for scope in scopes[:16]],
            "facts": [{"text": "明确事实", "source_refs": scope} for scope in scopes[16:48]],
            "deferred": [{"reason": "待补条件", "source_refs": scope} for scope in scopes[48:]],
        }

class NativeHeaderCompileTransport(StagedCompileTransport):
    """Match a real OV output: native range IDs in the redundant index header."""

    native_ids = [
        "dc9899095388a7ffc69bdae9", "5624d5d6bf7c0e9620ad0f8f", "e05469e47f2fe250540d600d",
        "e48cf9b4e2d850ae7cbac710", "b93ba268061d43c743e99d35", "61d7450f0861a5bab52a8564",
        "ed8914c74e02e91bb69d32c4", "c48a16a0b180ad79f8e77ee7", "0a2dfc06024d87f4ef0d3f01",
        "98e05f775edd03e2d3de2588", "0f3b2a61f00874548f26d347", "3af7b3d5c579e17a2f1f0edd",
        "7d5cde86c2bfe6012a323f96", "b755665a7df14c0ec5888412", "60b158807274bd6f83e96281",
    ]

    def __init__(self, *, omit_one_reference=False):
        super().__init__()
        self.omit_one_reference = omit_one_reference

    def result(self, packet):
        value = super().result(packet)
        if packet["stage"] == "extract":
            value["input_ids"] = self.native_ids[:]
            if self.omit_one_reference:
                for category in ("methods", "facts", "deferred"):
                    for row in value[category]:
                        row["source_refs"] = row["source_refs"][:1]
        return value

class FileCitationCompileTransport(NativeHeaderCompileTransport):
    def __init__(self, *, foreign_uri=False):
        super().__init__()
        self.foreign_uri = foreign_uri

    def result(self, packet):
        value = super().result(packet)
        if packet["stage"] != "extract":
            return value
        uri = packet["evidence"][0]["source_uri"]
        if self.foreign_uri:
            uri += ".unassigned"
        return {
            "schema_version": "skill-miner.index.v1", "input_ids": self.native_ids[:],
            "methods": [{"task": f"金额核验流程{index}", "boundary": "仅依照原始文件明确的地区和金额条件。",
                         "source_refs": [uri]} for index in range(16)],
            "facts": [{"text": "原文说明北京人民币9000元，阈值12000元，差額3000元。",
                       "source_refs": [uri]} for _ in range(32)],
            "deferred": [{"reason": "原文引用的扩展附件未在本次原始材料提供，不能推断附件事实。",
                          "source_refs": [uri]} for _ in range(16)],
        }
