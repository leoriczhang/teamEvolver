"""Exercise bounded TE planning with inert Compile results and complete source Evidence."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
import re
from datetime import datetime, timedelta, timezone

import pytest

from team_miner import bounded_compile_plan as staged
from team_miner import bridge as mining_bridge
from team_miner import ov_round_orchestration
from team_miner.ov_compile_mining import RUNS_ROOT, OvSkillMiningRepository
from team_miner.ov_staged_orchestration import _revision_view
from team_miner.tests.test_ov_round_bridge import FakeCompileClient, FakeOV, Owner

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


@pytest.mark.parametrize("document_count", [120, 1000])
def test_large_sources_keep_every_original_character_and_each_compile_packet_is_bounded(document_count):
    documents = original_documents(document_count)
    chunks = all_chunks(documents)
    assert len({chunk["id"] for chunk in chunks}) == len(chunks)
    assert len(chunks) > document_count
    for uri, text in documents.items():
        parts = [chunk for chunk in chunks if chunk["source_uri"] == uri]
        cursor = 0
        for chunk in parts:
            assert chunk["start_char"] == cursor
            assert chunk["content"] == text[chunk["start_char"] : chunk["end_char"]]
            assert chunk["sha256"] == hashlib.sha256(chunk["content"].encode()).hexdigest()
            cursor = chunk["end_char"]
        assert cursor == len(text)
        assert "".join(part["content"] for part in parts) == text
    groups = staged.pack_chunks(chunks)
    assert [chunk["id"] for group in groups for chunk in group] == [chunk["id"] for chunk in chunks]
    for group in groups:
        packet = staged.build_stage_packet(
            "index", "index.json", evidence_chunks=group, controls={}, revision_context="",
            language_policy=LANGUAGE,
        )
        assert len(json.dumps(packet, ensure_ascii=False)) <= 32_000
        assert packet["derived_controls_are_evidence"] is False
    assert len(groups) > 1


@pytest.mark.parametrize(
    "text", ['"\\' * 15_000, "a\n" * 20_000, "|" + "超长表格条件" * 6000 + "|\n"],
    ids=["json-escaping", "dense-lines", "long-table-row"],
)
def test_one_large_dense_or_escaped_source_can_be_packed_without_discarding_content(text):
    uri = "viking://resources/team/source/large.md"
    chunks = staged.source_chunks(text, uri)
    groups = staged.pack_chunks(chunks)
    assert "".join(chunk["content"] for group in groups for chunk in group) == text
    for group in groups:
        packet = staged.build_stage_packet(
            "index", "index.json", evidence_chunks=group, controls={}, revision_context="",
            language_policy=LANGUAGE,
        )
        assert len(json.dumps(packet, ensure_ascii=False)) <= 32_000


def test_case_validation_does_not_pad_insufficient_evidence_or_accept_foreign_sources():
    uri = "viking://resources/team/source/policy.md"
    case = canonical_case("policy-review-case-01", uri)
    case["checks"] = case["checks"][:3]
    unchanged = copy.deepcopy(case)
    with pytest.raises(ValueError):
        staged.validate_case(case, case_id=case["case_id"], skill_name="policy-review", source_refs=[uri])
    assert case == unchanged
    foreign = canonical_case("policy-review-case-01", "viking://resources/team/unassigned.md")
    with pytest.raises(ValueError):
        staged.validate_case(foreign, case_id=foreign["case_id"], skill_name="policy-review", source_refs=[uri])


def test_twelve_validated_cases_are_assembled_without_rewriting_queries_or_checklists():
    from team_miner.benchmark_format import validate_document as validate_mining_document
    from team_replay.datasets.schema import validate_document

    uri = "viking://resources/team/source/policy.md"
    cases = [canonical_case(f"policy-review-case-{index:02d}", uri) for index in range(1, 13)]
    original = copy.deepcopy(cases)
    for case in cases:
        staged.validate_case(case, case_id=case["case_id"], skill_name="policy-review", source_refs=[uri])
    document = staged.assemble_dataset(cases, skill_name="policy-review", task="核验条款", identity="isolated-test")
    assert document["cases"] == cases == original
    assert validate_document(document) == []
    assert validate_mining_document(document, expected_skill_name="policy-review") == []
    assert document["source"]["kind"] == "skill_miner"
    assert all(case["provenance"]["session_ids"] == [uri] for case in document["cases"])


def test_zero_candidates_remain_a_grounded_result_without_generating_placeholder_packages():
    uri = "viking://resources/team/source/facts.md"
    plan = candidate_plan(uri, names=())
    assert staged.validate_plan(plan, source_refs=[uri]) == plan
    missing_reason = {**plan, "deferred": []}
    with pytest.raises(ValueError):
        staged.validate_plan(missing_reason, source_refs=[uri])


def test_staged_plan_rejects_shortened_or_unassigned_provenance():
    uri = "viking://resources/team/source/policy.md"
    plan = candidate_plan(uri)
    for foreign in ("policy.md", "viking://resources/team/unassigned.md"):
        changed = copy.deepcopy(plan)
        changed["skills"][0]["source_refs"] = [foreign]
        with pytest.raises(ValueError):
            staged.validate_plan(changed, source_refs=[uri])


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
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")
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


@pytest.mark.parametrize("document_count,candidate_count", [(9, 1), (17, 8), (120, 8), (1000, 1)])
def test_existing_compile_api_scales_and_preserves_all_chunks_and_candidate_scopes(
    tmp_path, monkeypatch, document_count, candidate_count,
):
    owner = staged_rig(tmp_path, monkeypatch, candidate_count=candidate_count)
    # 9/17 files deliberately create one extract unit each, including singleton tails.
    body = ("北京人民币9000元，阈值12000元，差额3000元，保留例外与人工确认。\n" * 480
            if document_count in {9, 17} else "北京人民币9000元，阈值12000元，差额3000元。\n" * 25)
    documents = {str(index): f"# 来源 {index}\n{body}" for index in range(document_count)}
    job, entries = frozen_job(owner, tmp_path, documents)

    async def run():
        started = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, started)

    final = asyncio.run(run())
    assert final["status"] == "succeeded", final.get("error")
    assert final["materialized"] is True
    root = owner._staged_root(final)
    chunks = [json.loads(path.read_text()) for path in sorted((root / "chunks").glob("*.json"))]
    extraction = [packet for packet in owner.remote.packets if packet["stage"] == "extract"]
    assert [chunk["id"] for packet in extraction for chunk in packet["evidence"]] == [chunk["id"] for chunk in chunks]
    assert len(extraction) > 8
    if document_count in {9, 17}:
        assert len(extraction) == document_count
    plan = json.loads((root / "plan.json").read_text())
    real_refs = list(dict.fromkeys(chunk["source_uri"] for chunk in chunks))
    assert len(plan["skills"]) == candidate_count
    assert all(row["source_refs"] == real_refs for row in plan["skills"])
    workspace = owner.repository.workspace(final)
    for name in owner.remote.candidate_names:
        packets = [
            packet for packet in owner.remote.packets
            if packet["stage"] == "skill_section" and packet["derived_controls"]["skill_name"] == name
        ]
        assert [chunk["id"] for packet in packets for chunk in packet["evidence"]] == [chunk["id"] for chunk in chunks]
        skill = (workspace / "compiled_skill" / name / "SKILL.md").read_text()
        assert all(chunk["id"] in skill for chunk in chunks)
        dataset = json.loads((workspace / "compiled_skill" / name / "benchmark.json").read_text())
        assert len(dataset["cases"]) == 12
        assert all(case["skill_ids"] == [name] and len(case["checks"]) == 13 for case in dataset["cases"])
    state_text = (root / "state.json").read_text()
    assert len(state_text) < 16_000
    assert "9000元" not in state_text
    assert max(owner.remote.envelopes) <= 48_000


@pytest.mark.parametrize("candidate_count,defer_case", [(0, False), (1, True)])
def test_small_evidence_can_finish_without_a_skill_or_padded_test_dataset(
    tmp_path, monkeypatch, candidate_count, defer_case,
):
    owner = staged_rig(tmp_path, monkeypatch, candidate_count=candidate_count, defer_case=defer_case)
    job, entries = frozen_job(owner, tmp_path, {"short": "原文只声明北京地区，缺少可执行方法和独立完成条件。"})

    async def run():
        started = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, started)

    final = asyncio.run(run())
    assert final["status"] == "succeeded"
    workspace = owner.repository.workspace(final)
    assert list((workspace / "compiled_skill").iterdir()) == []
    plan = json.loads((workspace / "semantic_reports" / "capability-plan.json").read_text())
    assert plan["skills"] == []
    if defer_case:
        assert plan["deferred"] and plan["deferred"][0]["source_refs"] == [
            job["source_snapshot"] + "/original/document-0000.md"
        ]
    else:
        assert plan["deferred"] == []
    case_packets = [packet for packet in owner.remote.packets if packet["stage"] == "case"]
    assert len(case_packets) == (1 if defer_case else 0)
    assert final["artifact_quality"]["can_submit"] is False


def test_large_revision_is_bounded_context_and_human_original_evidence_is_fully_scanned(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    human = "补充人工 Evidence：北京地区适用，金额人民币9000元。\n" * 1900
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"}, human_text=human)
    case_ids = [f"policy-review-0-case-{index:02d}" for index in range(1, 13)]
    revision = "上轮修订上下文：\n" + json.dumps({
        "previous_candidates": [{"skill_name": "policy-review-0", "case_ids": case_ids,
                                 "skill_body": "PRIVATE_OLD_BODY" * 1500}],
        "feedback": "修订意见，保留人工 Evidence 的边界。" * 1500,
        "review_questions": [{"qid": "region", "question": "是否只支持北京？"}],
        "previous_capability_plan": {"private": "PRIVATE_OLD_PLAN" * 1000},
    }, ensure_ascii=False)
    assert len(revision) > 30_000

    async def run():
        started = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, revision)
        return await finish_staged(owner, started)

    final = asyncio.run(run())
    assert final["status"] == "succeeded"
    assert (owner._staged_root(final) / "revision.txt").read_text() == revision
    human_chunks = [
        chunk for packet in owner.remote.packets if packet["stage"] == "extract" for chunk in packet["evidence"]
        if chunk["source_uri"].endswith("/human/addendum.md")
    ]
    assert "".join(chunk["content"] for chunk in human_chunks) == human
    for _, request in owner.remote.compile_calls:
        assert "PRIVATE_OLD_BODY" not in request["instruction"]
        assert "PRIVATE_OLD_PLAN" not in request["instruction"]
        assert '"revision_context_is_evidence":false' in request["instruction"]
        assert '"feedback_truncated":true' in request["instruction"]
        view = json.loads(request["instruction"].rsplit("\n", 1)[-1])
        identity = view["previous_candidates"][0]
        recovered = identity.get("case_ids") or [
            identity["case_id_pattern"].format(index=index) for index in identity["case_id_indexes"]
        ]
        assert recovered == case_ids


@pytest.mark.parametrize("lookup_supported", [True, False])
def test_restart_resolves_lost_acceptance_using_the_same_immutable_child_request(
    tmp_path, monkeypatch, lookup_supported,
):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote.lookup_supported = lookup_supported
    owner.remote.lose_next_acceptance = True
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})

    async def run():
        uncertain = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        assert uncertain["ov_task_id"] == ""
        assert len(owner.remote.accepted) == 1
        first_key, first_request = owner.remote.compile_calls[0]
        resumed = StagedOwner(owner.repository, owner.remote)
        resolved = await resumed._resume_staged_round(owner.repository.get_job(job["job_id"]))
        assert resolved["ov_task_id"] == owner.remote.accepted[first_key][0]
        assert len(owner.remote.accepted) == 1
        assert resolved.get("round_work_units") == uncertain.get("round_work_units")
        assert resolved.get("round_timeout_seconds") == uncertain.get("round_timeout_seconds")
        assert owner.remote.compile_calls == [(first_key, first_request)] * (1 if lookup_supported else 2)
        return await finish_staged(resumed, resolved)

    final = asyncio.run(run())
    assert final["status"] == "succeeded"


def test_stop_after_lost_acceptance_finds_and_cancels_the_only_active_child(
    tmp_path, monkeypatch,
):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote.lose_next_acceptance = True
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})

    async def run():
        uncertain = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        task_id = next(iter(owner.remote.tasks))
        owner.remote.tasks[task_id] = {"status": "running", "stage": "compile: running"}
        stopped = owner.repository.update_job(
            uncertain["job_id"], status="stopping", ov_cancel_reason="用户手动停止",
        )
        resumed = StagedOwner(owner.repository, owner.remote)
        resolved = await resumed._resume_staged_round(stopped)
        assert resolved["ov_task_id"] == task_id
        cancelled = await resumed._ov_enforce_deadline(owner.repository, resolved, "running")
        final = await resumed._advance_staged_round(cancelled, owner.remote.tasks[task_id])
        assert owner.remote.cancelled == [task_id]
        assert len(owner.remote.accepted) == 1
        assert len(owner.remote.packets) == 1
        return final

    final = asyncio.run(run())
    assert final["status"] == "stopped"


def test_stop_with_unavailable_submission_lookup_does_not_create_a_new_child(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote.lookup_supported = False
    owner.remote.lose_next_acceptance = True
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})

    async def run():
        uncertain = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        stopped = owner.repository.update_job(
            uncertain["job_id"], status="stopping", ov_cancel_reason="用户手动停止",
        )
        return await owner._resume_staged_round(stopped)

    final = asyncio.run(run())
    assert final["status"] == "stopped"
    assert len(owner.remote.compile_calls) == len(owner.remote.accepted) == 1
    assert owner.remote.cancelled == []


@pytest.mark.parametrize("invalid_count,expected_status", [(2, "succeeded"), (3, "failed")])
def test_invalid_case_outputs_are_retried_with_bounded_original_evidence_and_never_padded(
    tmp_path, monkeypatch, invalid_count, expected_status,
):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote.bad_case_attempts = invalid_count
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})

    async def run():
        started = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, started)

    final = asyncio.run(run())
    assert final["status"] == expected_status
    attempts = [packet for packet in owner.remote.packets if packet["stage"] == "case"]
    assert len(attempts) == (14 if expected_status == "succeeded" else 3)
    assert attempts[0]["evidence"] == attempts[1]["evidence"] == attempts[2]["evidence"]
    assert attempts[0]["derived_controls"] == attempts[1]["derived_controls"] == attempts[2]["derived_controls"]
    if expected_status == "failed":
        assert not list(owner.repository.workspace(final).glob("compiled_skill/*/benchmark.json"))
    else:
        assert final["round_work_units"] >= len(owner.remote.accepted)


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


def test_multi_level_planning_keeps_each_methods_exact_original_scope(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote = ScopedCompileTransport(candidate_count=2)
    monkeypatch.setattr(
        ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(owner.remote, **kwargs),
    )
    body = "北京人民币9000元，阈值12000元，差额3000元，保留例外与人工确认。\n" * 480
    documents = {str(index): f"# 独立方法 {index}\n{body}" for index in range(17)}
    job, entries = frozen_job(owner, tmp_path, documents)

    async def run():
        started = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, started)

    final = asyncio.run(run())
    assert final["status"] == "succeeded"
    root = owner._staged_root(final)
    plan = json.loads((root / "plan.json").read_text())
    for row in plan["skills"]:
        expected_indexes = list(range(0 if row["name"] == "region-a" else 1, 17, 2))
        expected_refs = [job["source_snapshot"] + f"/original/document-{index:04d}.md" for index in expected_indexes]
        assert row["source_refs"] == expected_refs
        actual = {
            chunk["source_uri"] for packet in owner.remote.packets
            if packet["stage"] == "skill_section" and packet["derived_controls"]["skill_name"] == row["name"]
            for chunk in packet["evidence"]
        }
        assert actual == set(expected_refs)


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


def test_all_thirty_eight_proposals_receive_a_disposition_without_becoming_thirty_eight_skills(
    tmp_path, monkeypatch,
):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote = TypedDispositionCompileTransport()
    monkeypatch.setattr(
        ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(owner.remote, **kwargs),
    )
    documents = {str(index): "原文保留核验步骤、限制与例外。" * 800 for index in range(6)}
    job, entries = frozen_job(owner, tmp_path, documents)

    async def run():
        started = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, started)

    final = asyncio.run(run())
    assert final["status"] == "succeeded", final.get("error")
    root = owner._staged_root(final)
    original_methods = [
        method for packet in owner.remote.packets if packet["stage"] == "extract"
        for method in owner.remote.result(packet)["methods"]
    ]
    assert len(original_methods) == 38
    assert len({method["task"] for method in original_methods}) == 38
    plan = json.loads((root / "plan.json").read_text())
    assert {row["name"] for row in plan["skills"]} == set(owner.remote.candidate_names)
    assert len(plan["skills"]) == 3
    assert len(plan["deferred"]) == 5
    selections = [packet for packet in owner.remote.packets if packet["stage"] == "plan"]
    assert len(selections) > 1
    assert all(len(packet["derived_controls"]["items"]) <= 8 for packet in selections)
    assert sum(len(owner.remote.result(packet)["knowledge"]) for packet in selections) == 13
    for packet in selections:
        output = owner.remote.result(packet)
        disposed = [identity for group in ("skills", "knowledge", "deferred", "resolved")
                    for row in output[group] for identity in row["derived_from"]]
        assert len(disposed) == len(set(disposed))
        assert set(disposed) == {item["item_id"] for item in packet["derived_controls"]["items"]}
    allowed = {job["source_snapshot"] + f"/original/document-{index:04d}.md" for index in range(4)}
    assert all(set(row["source_refs"]) <= allowed for row in plan["skills"])
    assert {ref for row in plan["skills"] for ref in row["source_refs"]} == allowed


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


def test_large_file_background_fragments_do_not_defer_a_method_supported_at_the_end(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote = SparseMethodCompileTransport()
    monkeypatch.setattr(
        ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(owner.remote, **kwargs),
    )
    original = "仅介绍版本背景，不包含操作规则。\n" * 3000 + owner.remote.marker
    job, entries = frozen_job(owner, tmp_path, {"long-source": original})

    async def run():
        active = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, active)

    final = asyncio.run(run())
    assert final["status"] == "succeeded", final.get("error")
    extraction = [packet for packet in owner.remote.packets if packet["stage"] == "extract"]
    assert len(extraction) > 2
    assert "".join(chunk["content"] for packet in extraction for chunk in packet["evidence"]) == original
    plan = json.loads((owner._staged_root(final) / "plan.json").read_text())
    assert [row["name"] for row in plan["skills"]] == list(owner.remote.candidate_names)
    cases = [packet for packet in owner.remote.packets if packet["stage"] == "case"]
    assert len(cases) == 12
    assert all(owner.remote.marker in "".join(chunk["content"] for chunk in packet["evidence"]) for packet in cases)


def test_dataset_assembly_rejects_repeated_normalized_case_queries_without_rewriting_them():
    uri = "viking://resources/team/source/policy.md"
    cases = [canonical_case(f"policy-review-case-{index:02d}", uri) for index in range(1, 13)]
    cases[1]["query"] = "  " + cases[0]["query"] + "\n"
    original = copy.deepcopy(cases)
    with pytest.raises(ValueError):
        staged.assemble_dataset(cases, skill_name="policy-review", task="核验条款", identity="duplicate-test")
    assert cases == original


def test_restart_rejects_a_changed_frozen_child_request_before_any_remote_effect(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote.lose_next_acceptance = True
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})

    async def run():
        uncertain = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        root, state = await owner._staged_load(uncertain)
        request_file = root / state["current"]["request_file"]
        changed = json.loads(request_file.read_text())
        changed["instruction"] = "Tampered stage instruction"
        request_file.write_text(json.dumps(changed), encoding="utf-8")
        with pytest.raises(ValueError, match="冻结的阶段请求"):
            await owner._resume_staged_round(uncertain)

    asyncio.run(run())
    assert len(owner.remote.compile_calls) == 1
    assert len(owner.remote.published) == 1


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


def test_planning_budget_includes_all_private_scope_allowlists_not_only_projected_values(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote = ManyScopesCompileTransport(candidate_count=0)
    monkeypatch.setattr(
        ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(owner.remote, **kwargs),
    )
    documents = {str(index): f"来源{index}：北京人民币9000元，阈值12000元。" for index in range(64)}
    job, entries = frozen_job(owner, tmp_path, documents)

    async def run():
        started = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, started)

    final = asyncio.run(run())
    assert final["status"] == "succeeded"
    assert max(owner.remote.envelopes) <= 48_000


def test_eight_maximum_names_keep_all_case_identities_in_bounded_two_round_context(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch, candidate_count=8)
    names = tuple(f"skill-{index}-" + "a" * 56 for index in range(8))
    assert all(len(name) == 64 for name in names)
    owner.remote.candidate_names = names
    body = "北京人民币9000元，阈值12000元，差额3000元，保留例外与人工确认。\n" * 480
    documents = {str(index): f"# 来源 {index}\n{body}" for index in range(17)}
    job, entries = frozen_job(owner, tmp_path, documents)
    owner.repository.update_job(job["job_id"], max_rounds=2, human_checkpoints_effective=False)

    async def run():
        first = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        first = await finish_staged(owner, first)
        assert first["status"] == "succeeded"
        workspace = owner.repository.workspace(first)
        previous = []
        for name in names:
            dataset = json.loads((workspace / "compiled_skill" / name / "benchmark.json").read_text())
            previous.append({
                "skill_name": name, "case_ids": [case["case_id"] for case in dataset["cases"]],
                "skill_body": "NON_EVIDENCE_PREVIOUS_BODY" * 200,
            })
        revision = "本轮修订上下文：\n" + json.dumps({
            "previous_candidates": previous, "feedback": "保留原始 Evidence 范围。" * 1000,
        }, ensure_ascii=False)
        global_view = _revision_view(revision, 6000)
        assert len(global_view) <= 6000
        global_identities = json.loads(global_view.rsplit("\n", 1)[-1])["previous_candidates"]
        for row, archived in zip(global_identities, previous):
            recovered = [row["case_id_pattern"].format(index=index) for index in row["case_id_indexes"]]
            assert recovered == archived["case_ids"]
        # Exercise the actual repository transition: previous output is archived and
        # the new round clears the active child and materialized workspace.
        first = owner.repository.update_job(first["job_id"], status="running")
        first = owner.repository.finish_materialized_round(first["job_id"])
        second = owner.repository.prepare_automatic_next_round(first["job_id"])
        assert second["current_round"] == 2 and not second["staged_state_path"]
        packet_start = len(owner.remote.packets)
        call_start = len(owner.remote.compile_calls)
        started = await owner._prepare_staged_round(second, owner.compiler_body, entries, LANGUAGE, revision)
        second = await finish_staged(owner, started)
        assert second["status"] == "succeeded"
        assert (owner._staged_root(second) / "revision.txt").read_text() == revision
        assert json.loads(revision.split("\n", 1)[-1])["previous_candidates"] == previous
        for packet, (_, request) in zip(owner.remote.packets[packet_start:], owner.remote.compile_calls[call_start:]):
            assert all("NON_EVIDENCE_PREVIOUS_BODY" not in chunk["content"] for chunk in packet["evidence"])
            if packet["stage"] in {"identity_group", "evidence_review"}:
                assert "NON_EVIDENCE_PREVIOUS_BODY" not in request["instruction"]
                continue
            view = json.loads(request["instruction"].rsplit("\n", 1)[-1])
            assert view["revision_context_is_evidence"] is False
            if packet["stage"] in {"skill_section", "evaluation", "case"}:
                name = packet["derived_controls"]["skill_name"]
                assert view["previous_candidates"] == [{
                    "skill_name": name,
                    "case_ids": next(row["case_ids"] for row in previous if row["skill_name"] == name),
                }]
        return first, second

    first, second = asyncio.run(run())
    for round_job in (first, second):
        output = owner.repository._job_dir(round_job["job_id"]) / "rounds" / "round-1" / "output"
        if round_job["current_round"] == 2:
            output = owner.repository.workspace(round_job)
        for name in names:
            dataset = json.loads((output / "compiled_skill" / name / "benchmark.json").read_text())
            assert [case["case_id"] for case in dataset["cases"]] == [
                f"{name}-case-{index:02d}" for index in range(1, 13)
            ]


def test_round_deadline_includes_all_candidate_outputs_without_resetting_started_at(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch, candidate_count=8)
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})
    origin = datetime(2026, 10, 12, 8, tzinfo=timezone.utc)
    job = owner.repository.update_job(
        job["job_id"], runtime_timeout_seconds=120, round_timeout_seconds=120,
        round_started_at=origin.isoformat(),
    )

    async def run():
        active = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        source_timeout = active["round_timeout_seconds"]
        for _ in range(10):
            active = await owner._advance_staged_round(active, owner.remote.tasks[active["ov_task_id"]])
            _, state = await owner._staged_load(active)
            if state["phase"] == "skill_section":
                break
        assert state["phase"] == "skill_section"
        # Every supported candidate has a section, evaluation and twelve independent cases.
        assert active["round_work_units"] >= 8 * 14 + 2
        assert active["round_timeout_seconds"] > source_timeout
        assert active["round_started_at"] == origin.isoformat()
        monkeypatch.setattr(
            mining_bridge, "utc_now", lambda: (origin + timedelta(seconds=source_timeout + 1)).isoformat(),
        )
        running = await owner._ov_enforce_deadline(owner.repository, active, "running")
        assert running["ov_cancel_reason"] == ""
        assert owner.remote.cancelled == []
        monkeypatch.setattr(
            mining_bridge, "utc_now",
            lambda: (origin + timedelta(seconds=active["round_timeout_seconds"] + 1)).isoformat(),
        )
        expired = await owner._ov_enforce_deadline(owner.repository, running, "running")
        assert expired["status"] == "stopping"
        assert owner.remote.cancelled == [active["ov_task_id"]]
        assert expired["round_started_at"] == origin.isoformat()

    asyncio.run(run())


def test_new_staged_round_recomputes_budget_instead_of_inheriting_previous_work(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})
    job = owner.repository.update_job(
        job["job_id"], current_round=2, max_rounds=2, runtime_timeout_seconds=120,
        round_timeout_seconds=999_999, round_work_units=9999,
        source_snapshot=job["source_snapshot"].replace("round-1", "round-2"),
    )
    active = asyncio.run(owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, ""))
    assert 120 <= active["round_timeout_seconds"] < 999_999
    assert 1 <= active["round_work_units"] < 9999


def test_restart_reconciles_advanced_durable_state_when_job_update_failed(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})
    original_update = owner.repository.update_job
    armed = True

    async def run():
        nonlocal armed
        accepted = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")

        def interrupted_update(job_id, **changes):
            nonlocal armed
            if armed and "staged_progress" in changes and changes.get("ov_task_id") == "":
                armed = False
                raise OSError("Simulate interruption between the durable stage and job writes")
            return original_update(job_id, **changes)

        monkeypatch.setattr(owner.repository, "update_job", interrupted_update)
        with pytest.raises(OSError, match="durable stage"):
            await owner._advance_staged_round(accepted, owner.remote.tasks[accepted["ov_task_id"]])
        stale = owner.repository.get_job(job["job_id"])
        _, durable = await owner._staged_load(stale)
        assert durable["completed_tasks"] == 1 and durable["current"] is None
        assert stale["ov_task_id"] == accepted["ov_task_id"]
        # Reproduce the monitor's next action: it sees the old known task ID,
        # reads that task's terminal status and invokes the staged advancement.
        resumed = StagedOwner(owner.repository, owner.remote)
        reconciled = await resumed._advance_staged_round(stale, owner.remote.tasks[stale["ov_task_id"]])
        assert reconciled["ov_task_id"] != stale["ov_task_id"]
        final = await finish_staged(resumed, reconciled)
        assert len([packet for packet in owner.remote.packets if packet["stage"] == "extract"]) == 1
        return final

    assert asyncio.run(run())["status"] == "succeeded"


def test_failed_child_retry_remains_recoverable_when_job_update_is_interrupted(tmp_path, monkeypatch):
    owner = staged_rig(tmp_path, monkeypatch)
    job, entries = frozen_job(owner, tmp_path, {"policy": "北京人民币9000元，阈值12000元。"})
    original_update = owner.repository.update_job
    armed = True

    async def run():
        nonlocal armed
        accepted = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        owner.remote.tasks[accepted["ov_task_id"]] = {
            "status": "failed", "stage": "compile: failed", "error": {"message": "Transient model output error"},
        }

        def interrupted_update(job_id, **changes):
            nonlocal armed
            if armed and "staged_progress" in changes and changes.get("ov_task_id") == "":
                armed = False
                raise OSError("Simulate interruption after the new child was frozen")
            return original_update(job_id, **changes)

        monkeypatch.setattr(owner.repository, "update_job", interrupted_update)
        with pytest.raises(OSError, match="new child"):
            await owner._refresh_ov_skill_job(accepted)
        stale = owner.repository.get_job(job["job_id"])
        _, durable = await owner._staged_load(stale)
        assert durable["current"]["attempt"] == 1
        assert durable["current"]["task_id"] == ""
        assert stale["ov_task_id"] == accepted["ov_task_id"]
        assert stale["status"] in {"running", "queued", "preparing"}
        assert stale["finished_at"] == ""
        # The restarted monitor still selects this active parent, then reconciles
        # its old failed task with the new child already recorded on disk.
        resumed = StagedOwner(owner.repository, owner.remote)
        await resumed._refresh_ov_skill_job(stale)
        recovered = owner.repository.get_job(job["job_id"])
        assert recovered["ov_task_id"] != stale["ov_task_id"]
        final = await finish_staged(resumed, recovered)
        assert len([packet for packet in owner.remote.packets if packet["stage"] == "extract"]) == 2
        return final

    assert asyncio.run(run())["status"] == "succeeded"


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


@pytest.mark.parametrize("omit_one_reference", [False, True])
def test_extract_bookkeeping_uses_frozen_assignment_only_after_complete_row_provenance(
    tmp_path, monkeypatch, omit_one_reference,
):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote = NativeHeaderCompileTransport(omit_one_reference=omit_one_reference)
    monkeypatch.setattr(
        ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(owner.remote, **kwargs),
    )
    job, entries = frozen_job(owner, tmp_path, {
        "policy": "北京人民币9000元，阈值12000元。",
        "boundary": "提交方需要保留地区边界并完成原文要求的人工确认步骤。",
    })

    async def run():
        active = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, active)

    final = asyncio.run(run())
    root = owner._staged_root(final)
    raw_results = []
    for path in (root / "children").glob("*/result.txt"):
        metadata = json.loads((path.parent / "metadata.json").read_text())
        if metadata["stage"] == "extract":
            raw_results.append(json.loads(path.read_text()))
    assert raw_results and all(row["input_ids"] == owner.remote.native_ids for row in raw_results)
    if omit_one_reference:
        assert final["status"] == "failed"
        assert list(root.glob("units/0/*.json")) == []
        assert len(raw_results) == 3
    else:
        assert final["status"] == "succeeded"
        accepted = json.loads((root / "units" / "0" / "00000000.json").read_text())
        extraction = next(packet for packet in owner.remote.packets if packet["stage"] == "extract")
        assert accepted["value"]["input_ids"] == extraction["input_ids"]
        assert accepted["ordinals"] == [0, 1]


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


@pytest.mark.parametrize("foreign_uri", [False, True])
def test_exact_file_citation_can_cover_only_its_assigned_ranges_and_long_uri_metadata_is_bounded(
    tmp_path, monkeypatch, foreign_uri,
):
    owner = staged_rig(tmp_path, monkeypatch)
    owner.remote = FileCitationCompileTransport(foreign_uri=foreign_uri)
    monkeypatch.setattr(
        ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(owner.remote, **kwargs),
    )
    text = "北京人民币9000元，阈值12000元，差额3000元，保留例外与人工确认。\n" * 480
    job, entries = frozen_job(owner, tmp_path, {"policy": text})
    # Long URI prefixes are metadata, and every actual path component remains short.
    entries[0]["relative_path"] = "original/" + ("source-folder-" + "x" * 35 + "/") * 6 + "policy.md"

    async def run():
        active = await owner._prepare_staged_round(job, owner.compiler_body, entries, LANGUAGE, "")
        return await finish_staged(owner, active)

    final = asyncio.run(run())
    root = owner._staged_root(final)
    extraction = next(packet for packet in owner.remote.packets if packet["stage"] == "extract")
    assert len(extraction["evidence"]) == 2
    assert len({chunk["source_uri"] for chunk in extraction["evidence"]}) == 1
    raw_results = []
    for path in root.glob("children/*/result.txt"):
        metadata = json.loads((path.parent / "metadata.json").read_text())
        if metadata["stage"] == "extract":
            text = path.read_text()
            assert 12_000 < len(text) <= 48_000
            raw_results.append(json.loads(text))
    assert raw_results and all(row["input_ids"] == owner.remote.native_ids for row in raw_results)
    if foreign_uri:
        assert final["status"] == "failed"
        assert list(root.glob("units/0/*.json")) == []
    else:
        assert final["status"] == "succeeded"
        accepted = json.loads((root / "units" / "0" / "00000000.json").read_text())
        assert accepted["ordinals"] == [0, 1]
        assert list(accepted["bindings"].values()) == [[0, 1]]
        assert len(json.dumps(accepted["value"], ensure_ascii=False, separators=(",", ":"))) <= 12_000
        assert accepted["value"]["input_ids"] == extraction["input_ids"]
        assert all(row["source_refs"] == [extraction["evidence"][0]["source_uri"]]
                   for value in raw_results for key in ("methods", "facts", "deferred") for row in value[key])
