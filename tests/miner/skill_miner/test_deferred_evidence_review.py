"""Inert Compile checks for original Evidence review and cross-fragment support."""

from __future__ import annotations

import asyncio
import copy
import re

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy
from team_miner.skill_miner import deferred_evidence_review as review

LANGUAGE = {"language": "zh-CN", "version": 1, "basis": "frozen_original_source"}


def candidate(name="fee-calc"):
    return {
        "name": name,
        "task": "根据数量与费率计算费用",
        "trigger": "收到费用计算请求",
        "inputs": ["数量", "适用费率"],
        "outputs": ["费用"],
        "boundary": "只使用提供的计算规则，调用时缺少参数应请求参数而非虚构数值",
        "rationale": "原文提供可执行计算规则",
    }


def response(packet, outcome="candidate", *, quote=None, reason="原文支持完整费用计算规则"):
    evidence = packet["evidence"]
    quote = quote if quote is not None else evidence[0]["content"][:80]
    quotes = [{"source_uri": evidence[0]["source_uri"], "text": quote}] if quote else []
    return {
        "schema_version": review.REVIEW_SCHEMA,
        "input_ids": packet["input_ids"],
        "reviews": [
            {
                "item_id": packet["derived_controls"]["items"][0]["item_id"],
                "outcome": outcome,
                "reason": reason,
                "source_refs": packet["derived_controls"]["allowed_source_refs"],
                "supporting_quotes": quotes,
                "partial_steps": [{"text": "保留原文中的运算条件", "quote_indexes": [0]}] if quotes else [],
                "candidate": candidate() if outcome == "candidate" else None,
            }
        ],
    }


def seed(tmp_path, texts, *, method_ordinals=None):
    chunks = []
    for index, text in enumerate(texts):
        uri = f"viking://resources/original/document-{index:04d}.md"
        for chunk in planning.source_chunks(text, uri):
            chunk["ordinal"] = len(chunks)
            chunks.append(chunk)
    files = {}
    for chunk in chunks:
        ordinal = chunk["ordinal"]
        review._save(tmp_path / "chunks" / f"{ordinal:08d}.json", chunk)
        metadata = {key: value for key, value in chunk.items() if key != "content"}
        metadata["serialized_chars"] = len(review._json(chunk))
        review._save(tmp_path / "chunk-metadata" / f"{ordinal:08d}.json", metadata)
        files.setdefault(chunk["source_uri"], []).append(ordinal)
    for uri, ordinals in files.items():
        review._save(tmp_path / "source-files" / (review._sha(uri) + ".json"), {"uri": uri, "ordinals": ordinals})
    record = {
        "item": {
            "item_id": "item-method-01",
            "kind": "method",
            "task": "按原文规则计算费用",
            "boundary": "按数量和费率计算，完整规则以原文为准",
            "source_files": list(files),
        },
        "leaf_items": ["item-leaf-01"],
        "ordinals": method_ordinals if method_ordinals is not None else list(range(len(chunks))),
    }
    gap = {"reason": "摘要中未提供费用规则", "source_refs": list(reversed(files))}
    review._save(tmp_path / "proposals" / "00000000.json", record)
    review._save(tmp_path / "method-decisions" / "item-leaf-01.json", {"target": "deferred", **gap})
    review._save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": [gap]})
    (tmp_path / "compiler-base.md").write_text("frozen base", encoding="utf-8")
    state = {
        "phase": review.STAGE,
        "proposal_count": 1,
        "language_policy": LANGUAGE,
        "completed_tasks": 0,
        "estimated_tasks": 1,
        "evidence_review_protocol": "literal-quotes.v1",
    }
    return state, chunks, gap


class Host(review.DeferredEvidenceReviewMixin):
    def __init__(self):
        self.assignments = []
        self.current_packet = None
        self.current = None
        self.compiler = None

    def _staged_stopped(self, job):
        return job.get("status") in {"stopped", "stopping"}

    async def _ensure_aux_compiler(self, root, state, stage, body):
        self.compiler = (stage, body)
        state.setdefault("aux_compilers", {})[stage] = review._sha(body)

    async def _staged_save(self, job, root, state, **changes):
        review._save(root / "state.json", state)
        return {**job, **changes}

    async def _staged_next(self, job, root, state):
        if state["phase"] == review.STAGE:
            return await self._staged_evidence_review_next(job, root, state)
        assert state["evidence_review_complete"]
        assert state["phase"] == "identity_group"
        return job

    async def _staged_submit(self, job, root, state, stage, output_path, chunks, controls, context):
        packet = planning.build_stage_packet(
            stage,
            output_path,
            evidence_chunks=chunks,
            controls=controls,
            language_policy=state["language_policy"],
        )
        assert len(review._json(packet)) <= 32000
        child = "review-" + review._sha(review._json([context, packet]))[:24]
        path = f"children/{child}/packet.json"
        review._save(root / path, packet)
        self.current = {
            "child": child,
            "stage": stage,
            "context": context,
            "packet_file": path,
            "packet_sha256": review._sha(review._json(packet)),
        }
        self.current_packet = packet
        self.assignments.append(
            {
                "mode": controls["review_mode"],
                "ids": packet["input_ids"],
                "source_refs": controls["allowed_source_refs"],
                "size": len(review._json(packet)),
                "scope": {key: controls[key] for key in (
                    "scope_protocol", "review_scope", "routed_scope_chunk_count",
                    "all_original_ranges_reviewed", "all_routed_ranges_reviewed",
                ) if key in controls},
            }
        )
        return job


def run_review(tmp_path, state, responder, monkeypatch):
    monkeypatch.setattr(
        compile_strategy,
        "render_single_stage_compiler",
        lambda base, stage, rules: "---\nname: inert-evidence-review\ndescription: Inert Evidence test\n---\n" + rules,
    )
    monkeypatch.setattr(compile_strategy, "compiler_business_policy", lambda base: {"common": "", "stages": {}})
    host = Host()
    job = {"status": "running"}

    async def run():
        for _ in range(10000):
            await host._staged_evidence_review_next(job, tmp_path, state)
            if state.get("evidence_review_complete"):
                return
            await host._staged_evidence_review_accept(
                job, tmp_path, state, host.current, responder(host.current_packet)
            )
            state["completed_tasks"] += 1
        pytest.fail("Evidence review did not converge")

    asyncio.run(run())
    return host, review._read(tmp_path / "evidence-review/results/00000000.json")


def test_current_original_quote_is_required_and_controls_are_not_evidence(tmp_path):
    state, chunks, _ = seed(tmp_path, ["原始规则：费用=数量×费率。"])
    descriptor = review._method_descriptor(tmp_path, 0, review._read(tmp_path / "proposals/00000000.json"))
    group, controls, _ = review._review_assignment(tmp_path, descriptor, 0, state["language_policy"])
    packet = planning.build_stage_packet(review.STAGE, "evidence-review.json", evidence_chunks=group, controls=controls)
    checked = review.validate_evidence_review(response(packet), items=controls["items"], evidence=chunks)
    assert checked["reviews"][0]["candidate"]["source_refs"] == controls["allowed_source_refs"]
    assert "调用时缺少参数" in checked["reviews"][0]["candidate"]["boundary"]
    invalid = response(packet, quote=controls["previous_deferral"])
    with pytest.raises(ValueError, match="literally occur"):
        review.validate_evidence_review(invalid, items=controls["items"], evidence=chunks)


@pytest.mark.parametrize("violation", ["item", "input", "uri", "duplicate", "enum", "quote", "step", "extra"])
def test_review_rejects_unknown_assignments_and_unproved_business_support(tmp_path, violation):
    state, _, _ = seed(tmp_path, ["原始规则：费用=数量×费率。"])
    descriptor = review._method_descriptor(tmp_path, 0, review._read(tmp_path / "proposals/00000000.json"))
    chunks, controls, _ = review._review_assignment(tmp_path, descriptor, 0, state["language_policy"])
    packet = planning.build_stage_packet(
        review.STAGE, "evidence-review.json", evidence_chunks=chunks, controls=controls
    )
    value = response(packet)
    row = value["reviews"][0]
    if violation == "item":
        row["item_id"] = "unassigned-method"
    if violation == "input":
        value["input_ids"] = ["native-unassigned-range"]
    if violation == "uri":
        row["source_refs"] = ["viking://unassigned/original"]
    if violation == "duplicate":
        value["reviews"].append(copy.deepcopy(row))
    if violation == "enum":
        row["outcome"] = "deferred"
    if violation == "quote":
        row["supporting_quotes"] = []
    if violation == "step":
        row["partial_steps"][0]["quote_indexes"] = [1]
    if violation == "extra":
        row["candidate"]["source_refs"] = controls["allowed_source_refs"]
    with pytest.raises(ValueError):
        review.validate_evidence_review(value, items=controls["items"], evidence=chunks)


def test_later_support_recovers_candidate_after_every_original_range_is_scanned(tmp_path, monkeypatch):
    state, chunks, old_gap = seed(tmp_path, ["背景资料。" * 6000, "原始规则：费用=数量×费率。"])

    def responder(packet):
        supported = any("费用=" in chunk["content"] for chunk in packet["evidence"])
        return response(
            packet,
            "candidate" if supported else "insufficient_this_fragment",
            quote=None if supported else "",
            reason="原文支持完整费用计算规则" if supported else "本片段只有背景，不能判断完整文件缺失规则",
        )

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "candidate"
    assert "支持完整" in final["reason"] and "只有背景" not in final["reason"]
    assert set(final["reviewed_input_ids"]) == {chunk["id"] for chunk in chunks}
    assert final["candidate"]["source_refs"] == final["source_refs"]
    assert final["previous_gap"] == old_gap
    assert len(host.assignments) > 1
    assert review._read(tmp_path / "plan.json")["deferred"] == [old_gap]


def test_method_range_is_read_first_but_all_other_ranges_remain_in_review(tmp_path, monkeypatch):
    state, chunks, _ = seed(tmp_path, ["背景资料。" * 6000, "原始规则：费用=数量×费率。"], method_ordinals=[3])
    # Compute the actual last ordinal; dense source chunking is byte-budget based.
    record = review._read(tmp_path / "proposals/00000000.json")
    record["ordinals"] = [len(chunks) - 1]
    review._save(tmp_path / "proposals/00000000.json", record)
    host, final = run_review(tmp_path, state, lambda packet: response(packet), monkeypatch)
    assert host.assignments[0]["ids"][0] == chunks[-1]["id"]
    assert set(final["reviewed_input_ids"]) == {chunk["id"] for chunk in chunks}


@pytest.mark.parametrize("count", [9, 17, 120, 1000])
def test_cross_fragment_rules_are_combined_in_finite_bounded_layers(tmp_path, monkeypatch, count):
    state, chunks, _ = seed(tmp_path, [f"原始规则{index}：数量和费率是运行时输入。" for index in range(count)])

    def responder(packet):
        outcome = (
            "candidate" if packet["derived_controls"]["review_mode"] == "combine" else "insufficient_this_fragment"
        )
        return response(
            packet, outcome, reason="组合原文条件形成完整方法" if outcome == "candidate" else "保留本片段的部分条件"
        )

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "candidate"
    assert any(row["mode"] == "combine" for row in host.assignments)
    assert set(final["reviewed_input_ids"]) == {chunk["id"] for chunk in chunks}
    assert len(final["candidate"]["source_refs"]) == count
    assert len(host.assignments) < 3 * count + 1
    assert all(row["size"] <= 32000 for row in host.assignments)


def test_all_fragments_insufficient_becomes_explicit_complete_scope_deferral(tmp_path, monkeypatch):
    state, chunks, _ = seed(tmp_path, ["定义如下，具体执行流程参见未附外部资料。" * 1200])
    _, final = run_review(
        tmp_path,
        state,
        lambda packet: response(packet, "insufficient_this_fragment", quote="", reason="所需外部执行流程未附"),
        monkeypatch,
    )
    assert final["outcome"] == "deferred"
    assert final["reason"].startswith(f"已扫描全部{len(chunks)}个原始片段")
    assert "外部执行流程未附" in final["reason"]
    assert "scope_protocol" not in final and "review_scope" not in final


def test_distributed_conditions_require_original_quote_combination_not_fragment_votes(tmp_path, monkeypatch):
    first, second = "规则A：数量至少10。", "规则B：适用费率2%，缺少数量时请求运行时输入。"
    state, chunks, _ = seed(tmp_path, [first + "背景资料。" * 7500 + second])

    def responder(packet):
        material = "\n".join(chunk["content"] for chunk in packet["evidence"])
        present = [text for text in [first, second] if text in material]
        complete = len(present) == 2
        value = response(
            packet,
            "candidate" if complete else "insufficient_this_fragment",
            quote="",
            reason="结合原文数量条件及费率后支持完整方法" if complete else "当前仅有部分原文条件，需结合其它片段",
        )
        row = value["reviews"][0]
        row["supporting_quotes"] = [
            {"source_uri": packet["evidence"][0]["source_uri"], "text": text} for text in present
        ]
        row["partial_steps"] = [{"text": text, "quote_indexes": [index]} for index, text in enumerate(present)]
        return value

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "candidate"
    assert "结合原文" in final["reason"]
    assert any(assignment["mode"] == "combine" for assignment in host.assignments)
    assert len(final["reviewed_input_ids"]) == len(chunks)


def test_blank_fold_results_converge_to_deferral_without_submitting_empty_evidence(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, [f"部分原文条件{index}。" + "x" * 11500 for index in range(8)])

    def responder(packet):
        combine = packet["derived_controls"]["review_mode"] == "combine"
        return response(
            packet,
            "insufficient_this_fragment",
            quote="" if combine else None,
            reason="外部关键执行流程未附" if combine else "当前原文支持部分条件，需要进一步组合",
        )

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "deferred"
    assert "跨片段支持综合复核" in final["reason"]
    assert sum(item["mode"] == "original" for item in host.assignments) == 4
    assert sum(item["mode"] == "combine" for item in host.assignments) == 2
    assert all(item["ids"] and item["source_refs"] for item in host.assignments)
    units = list((tmp_path / "evidence-review/synthesis/00000000/units/2").glob("*.json"))
    assert len(units) == 1 and len(review._read(units[0])["parents"]) == 2


def test_blank_and_supported_fold_preserve_all_scope_and_use_remaining_original_support(tmp_path, monkeypatch):
    state, chunks, _ = seed(tmp_path, [f"部分原文条件{index}。" + "x" * 11500 for index in range(8)])
    combine_count = 0

    def responder(packet):
        nonlocal combine_count
        if packet["derived_controls"]["review_mode"] != "combine":
            return response(packet, "insufficient_this_fragment")
        combine_count += 1
        return response(
            packet,
            "insufficient_this_fragment" if combine_count == 1 else "candidate",
            quote="" if combine_count == 1 else None,
            reason="当前单位没有完整执行支持" if combine_count == 1 else "其余原文包含完整计算规则",
        )

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "candidate" and combine_count == 3
    assert set(final["reviewed_input_ids"]) == {chunk["id"] for chunk in chunks}
    assert len(final["source_refs"]) == 8
    assert all(item["ids"] for item in host.assignments)


def test_pure_lookup_review_can_remain_knowledge_without_requesting_missing_rules(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, ["产品代码为X01，版本为3，无操作过程。"])
    _, final = run_review(
        tmp_path,
        state,
        lambda packet: response(packet, "knowledge", reason="该条目只查询代码和版本，原文未提出执行工作流"),
        monkeypatch,
    )
    assert final["outcome"] == "knowledge" and final["candidate"] is None
    assert "只查询" in final["reason"]


def test_acceptance_replay_is_idempotent_and_frozen_source_tampering_is_rejected(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, ["原始规则：费用=数量×费率。"])
    monkeypatch.setattr(compile_strategy, "render_single_stage_compiler", lambda base, stage, rules: rules)
    host, job = Host(), {"status": "running"}

    async def run():
        await host._staged_evidence_review_next(job, tmp_path, state)
        value = response(host.current_packet)
        await host._staged_evidence_review_accept(job, tmp_path, state, host.current, value)
        await host._staged_evidence_review_accept(job, tmp_path, state, host.current, value)
        assert len(list((tmp_path / "evidence-review/fragments/00000000").glob("*.json"))) == 1
        chunk_path = tmp_path / "chunks/00000000.json"
        changed = review._read(chunk_path)
        changed["content"] += "额外未授权规则"
        review._save(chunk_path, changed)
        with pytest.raises(ValueError, match="frozen original"):
            await host._staged_evidence_review_accept(job, tmp_path, state, host.current, value)

    asyncio.run(run())


def test_stop_does_not_submit_or_initialize_review(tmp_path):
    state, _, _ = seed(tmp_path, ["费用=数量×费率。"])
    host = Host()
    asyncio.run(host._staged_evidence_review_next({"status": "stopped"}, tmp_path, state))
    assert not host.assignments and not (tmp_path / "evidence-review").exists()


@pytest.mark.parametrize("target", ["candidate", "knowledge", "no_methods"])
def test_empty_review_queue_skips_aux_renderer_and_does_not_invent_a_gap(tmp_path, monkeypatch, target):
    state, _, _ = seed(tmp_path, ["产品代码X01，只有事实查询。"])
    if target == "no_methods":
        state["proposal_count"] = 0
    else:
        review._save(tmp_path / "method-decisions/item-leaf-01.json", {"target": target, "reason": "已有逐项处置"})
    original_plan = {"schema_version": planning.PLAN_SCHEMA, "skills": [], "deferred": []}
    review._save(tmp_path / "plan.json", original_plan)

    def forbidden_renderer(*args):
        pytest.fail("An empty review queue must not render or migrate an unused compiler")

    monkeypatch.setattr(compile_strategy, "render_single_stage_compiler", forbidden_renderer)
    host = Host()
    asyncio.run(host._staged_evidence_review_next({"status": "running"}, tmp_path, state))
    assert state["evidence_review_complete"] and state["evidence_review_method_count"] == 0
    assert host.compiler is None and not host.assignments
    assert review._read(tmp_path / "plan.json") == original_plan


@pytest.mark.parametrize("was_complete", [False, True])
def test_durable_empty_manifest_resume_needs_no_cursor_or_unused_compiler(tmp_path, monkeypatch, was_complete):
    state, _, _ = seed(tmp_path, ["Product code X01; no executable procedure."])
    state.update(evidence_review_initialized=True, evidence_review_complete=was_complete)
    review._prepare_review(tmp_path, 0)
    review._save(tmp_path / "state.json", state)
    (tmp_path / "compiler-base.md").unlink()
    before_plan = (tmp_path / "plan.json").read_bytes()

    def forbidden_renderer(*args):
        pytest.fail("An empty durable queue must not render an unused compiler during resume")

    monkeypatch.setattr(compile_strategy, "render_single_stage_compiler", forbidden_renderer)
    resumed = review._read(tmp_path / "state.json")
    assert "evidence_review_cursor" not in resumed
    host = Host()
    asyncio.run(host._staged_evidence_review_next({"status": "running"}, tmp_path, resumed))
    assert resumed["evidence_review_complete"] and resumed["phase"] == "identity_group"
    assert resumed["evidence_review_method_count"] == 0
    assert resumed["estimated_tasks"] == state["estimated_tasks"]
    assert not host.assignments and host.compiler is None
    assert (tmp_path / "plan.json").read_bytes() == before_plan


def test_english_small_input_deferral_and_truncation_follow_frozen_language(tmp_path, monkeypatch):
    state, chunks, _ = seed(tmp_path, ["The execution procedure is in an external attachment that is absent."])
    state["language_policy"] = {**LANGUAGE, "language": "en"}
    host, final = run_review(
        tmp_path,
        state,
        lambda packet: response(
            packet,
            "insufficient_this_fragment",
            quote="",
            reason="The required external execution procedure is absent. " * 12,
        ),
        monkeypatch,
    )
    assert len(host.assignments) == 1
    assert final["outcome"] == "deferred"
    assert final["reason"].startswith(f"Reviewed all {len(chunks)} original fragments")
    assert final["reason"].endswith("original Evidence review ledger.)")
    assert len(final["reason"]) <= 400
    assert all(not "\u4e00" <= char <= "\u9fff" for char in final["reason"])
    assert host.current_packet["output_language_policy"]["language"] == "en"


def test_english_zero_support_fold_uses_english_and_never_submits_empty_evidence(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, [f"Partial original condition {index}. " + "x" * 11500 for index in range(8)])
    state["language_policy"] = {**LANGUAGE, "language": "en"}

    def responder(packet):
        combine = packet["derived_controls"]["review_mode"] == "combine"
        return response(
            packet,
            "insufficient_this_fragment",
            quote="" if combine else None,
            reason=(
                "The external execution procedure is absent." if combine else "Only a partial condition is supported."
            ),
        )

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "deferred"
    assert "completed the cross-fragment support review" in final["reason"]
    assert "no combinable support for a complete method" in final["reason"]
    assert all(not "\u4e00" <= char <= "\u9fff" for char in final["reason"])
    assert all(assignment["ids"] and assignment["source_refs"] for assignment in host.assignments)
    assert sum(assignment["mode"] == "combine" for assignment in host.assignments) == 2


@pytest.mark.parametrize("language", ["zh-CN", "en"])
def test_routed_deferral_records_only_the_actual_scope_across_fragment_synthesis(tmp_path, monkeypatch, language):
    from team_miner.skill_miner.source_scope import EFFICIENCY_PROTOCOL, SOURCE_SCOPE_PROTOCOL

    state, chunks, _ = seed(tmp_path, ["Original partial procedure. " + "x" * 105000], method_ordinals=[4])
    state.update(efficiency_protocol=EFFICIENCY_PROTOCOL, chunk_count=len(chunks), unit_count=len(chunks),
                 language_policy={**LANGUAGE, "language": language})
    for index, chunk in enumerate(chunks):
        ref = "ref-" + str(index)
        value = {
            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": [chunk["id"]],
            "methods": [], "facts": [{"text": "Original content", "source_refs": [ref]}], "deferred": [],
            "context_refs": [ref] if index == len(chunks) - 1 else [], "context_uncertain": False,
        }
        from team_miner.skill_miner.ov_staged_orchestration import _bind_unit

        review._save(tmp_path / "units/0" / f"{index:08d}.json", _bind_unit(value, {ref: [index]}, f"source-{index}"))
    review._save(tmp_path / "state.json", state)

    def responder(packet):
        return response(packet, "insufficient_this_fragment", reason="Only partial supported procedure is present.")

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    descriptor = review._read(tmp_path / "evidence-review/methods/00000000.json")
    selected = descriptor["ordinals"]
    assert 4 in selected and len(chunks) - 1 in selected and len(selected) < len(chunks)
    assert final["outcome"] == "deferred"
    assert final["scope_protocol"] == SOURCE_SCOPE_PROTOCOL
    assert final["review_scope"] == "routed_original_ranges"
    assert final["routed_scope_chunk_count"] == len(selected)
    assert final["all_routed_ranges_reviewed"] is True and final["all_original_ranges_reviewed"] is False
    assert set(final["reviewed_input_ids"]) == {chunks[index]["id"] for index in selected}
    assert any(row["mode"] == "combine" for row in host.assignments)
    assert all(row["scope"]["all_original_ranges_reviewed"] is False for row in host.assignments)
    assert all(row["scope"].get("all_routed_ranges_reviewed") is True
               for row in host.assignments if row["mode"] == "combine")
    expected = (f"已复核路由范围内全部{len(selected)}个原始片段" if language == "zh-CN"
                else f"Reviewed all {len(selected)} original fragments in the routed scope")
    assert final["reason"].startswith(expected)
    assert "every remaining range of all related original files" not in host.compiler[1]
    manifest = review._quote_manifest(tmp_path, descriptor, 0)
    units, controls, _ = review._quote_assignment(tmp_path, descriptor, manifest, 0, state["language_policy"])
    assert units and controls["all_original_ranges_reviewed"] is False
    assert controls["routed_scope_chunk_count"] == len(selected)
    assert "within the assigned routed method" in controls["scope_rule"]


@pytest.mark.parametrize("quote_ids", [False, True])
def test_routed_rules_leave_legacy_compiler_contract_byte_compatible(quote_ids):
    from team_miner.skill_miner.source_scope import EFFICIENCY_PROTOCOL

    legacy = review.QUOTE_REVIEW_INSTRUCTIONS if quote_ids else review.REVIEW_INSTRUCTIONS
    assert review._review_instructions({}, quote_ids=quote_ids) == legacy
    assert review._review_instructions({"efficiency_protocol": "unknown"}, quote_ids=quote_ids) == legacy
    routed = review._review_instructions({"efficiency_protocol": EFFICIENCY_PROTOCOL}, quote_ids=quote_ids)
    assert "every remaining range of all related original files" not in routed
    assert "all related fragments were reviewed" not in routed
    assert "not a review of every\noriginal range" in routed
    assert "do not claim the complete source lacks a rule" in routed


def test_existing_quote_compiler_is_preserved_even_when_new_routing_rules_are_available(tmp_path, monkeypatch):
    from team_miner.skill_miner.source_scope import EFFICIENCY_PROTOCOL

    def forbidden_renderer(*args, **kwargs):
        pytest.fail("An existing frozen compiler must never be re-rendered during resume")

    monkeypatch.setattr(compile_strategy, "render_single_stage_compiler", forbidden_renderer)
    state = {"efficiency_protocol": EFFICIENCY_PROTOCOL,
             "aux_compilers": {review.quote_units.QUOTE_STAGE: "frozen-existing-body-sha"}}
    host = Host()
    asyncio.run(host._ensure_quote_review_compiler(tmp_path, state))
    assert state["aux_compilers"] == {review.quote_units.QUOTE_STAGE: "frozen-existing-body-sha"}
    assert host.compiler is None


def quote_fixture(text, proposed):
    evidence = planning.source_chunks(text, "viking://resources/original/refund-policy.md")
    items = [{"item_id": "item-refund-01"}]
    packet = {
        "evidence": evidence,
        "input_ids": [chunk["id"] for chunk in evidence],
        "derived_controls": {"items": items, "allowed_source_refs": [evidence[0]["source_uri"]]},
    }
    return evidence, items, response(packet, quote=proposed)


def test_unique_format_locator_restores_exact_original_span_without_changing_raw_result():
    excerpt = (
        "Refund rule: approve “partial refund” for amount ≤ 50.\n\uf0d8\n"
        "Use the customer’s signed receipt.\n•\nReturn charge."
    )
    original = "Introduction.\n" + excerpt + "\nEnd of policy."
    proposed = (
        'Refund rule: approve "partial refund" for amount ≤ 50. Use the customer\'s signed receipt. Return charge.'
    )
    evidence, items, value = quote_fixture(original, proposed)
    before = copy.deepcopy(value)
    audit = []
    checked = review.validate_evidence_review(value, items=items, evidence=evidence, restorations=audit)
    assert value == before
    assert checked["reviews"][0]["supporting_quotes"][0]["text"] == excerpt
    assert audit == [
        {
            "item_id": "item-refund-01",
            "quote_index": 0,
            "source_uri": evidence[0]["source_uri"],
            "start_char": len("Introduction.\n"),
            "end_char": len("Introduction.\n") + len(excerpt),
            "sha256": review._sha(excerpt),
            "model_quote_sha256": review._sha(proposed),
            "normalization": "unicode_quotes_whitespace_standalone_bullets",
        }
    ]


@pytest.mark.parametrize(
    "change",
    [
        lambda text: text.replace("50", "51"),
        lambda text: text.replace("≤", "≥"),
        lambda text: text.replace("approve", "reject"),
        lambda text: text.replace("rule:", "rule;"),
        lambda text: text.replace("• receipt", "receipt"),
    ],
    ids=["number", "comparison", "business-word", "other-punctuation", "inline-bullet"],
)
def test_format_locator_never_changes_business_characters(change):
    original = "Refund rule: approve “partial refund” for amount ≤ 50; use • receipt."
    proposed = change(original.replace("“", '"').replace("”", '"'))
    evidence, items, value = quote_fixture(original, proposed)
    audit = []
    with pytest.raises(ValueError, match="Supporting quote 0.*short continuous Unicode excerpt"):
        review.validate_evidence_review(value, items=items, evidence=evidence, restorations=audit)
    assert not audit


def test_format_locator_rejects_distinct_source_offsets_but_preserves_old_exact_quote_behavior():
    original = "Refund “approved”.\nOther procedure.\nRefund “approved”."
    evidence, items, value = quote_fixture(original, 'Refund "approved".')
    with pytest.raises(ValueError, match="ambiguous source offsets"):
        review.validate_evidence_review(value, items=items, evidence=evidence)
    value["reviews"][0]["supporting_quotes"][0]["text"] = "Refund “approved”."
    audit = []
    review.validate_evidence_review(value, items=items, evidence=evidence, restorations=audit)
    assert not audit


def test_format_locator_deduplicates_the_same_global_span_in_overlapping_chunks():
    original = "Prefix.\nRefund “approved”.\nSuffix."
    evidence, items, value = quote_fixture(original, 'Refund "approved".')
    start = original.index("Refund")
    overlap = planning.source_chunks(original[start:], evidence[0]["source_uri"])[0]
    overlap["start_char"] += start
    overlap["end_char"] += start
    evidence.append(overlap)
    value["input_ids"] = [chunk["id"] for chunk in evidence]
    audit = []
    checked = review.validate_evidence_review(value, items=items, evidence=evidence, restorations=audit)
    assert checked["reviews"][0]["supporting_quotes"][0]["text"] == "Refund “approved”."
    assert len(audit) == 1 and audit[0]["start_char"] == start


def test_format_locator_cannot_combine_text_from_different_sources():
    evidence, items, value = quote_fixture("Refund “approved”.", 'Refund "approved". Require receipt.')
    evidence.extend(planning.source_chunks("Require receipt.", "viking://resources/original/other-policy.md"))
    value["input_ids"] = [chunk["id"] for chunk in evidence]
    value["reviews"][0]["source_refs"] = [chunk["source_uri"] for chunk in evidence]
    with pytest.raises(ValueError, match="must literally occur"):
        review.validate_evidence_review(value, items=items, evidence=evidence)


def test_restored_original_excerpt_still_has_the_800_character_hard_limit():
    original = "Refund “approved”." + "\n" * 801 + "Require receipt."
    evidence, items, value = quote_fixture(original, 'Refund "approved". Require receipt.')
    with pytest.raises(ValueError, match="exceeding 800"):
        review.validate_evidence_review(value, items=items, evidence=evidence)


def test_accepted_wrapper_records_restoration_without_rewriting_packet_or_raw_result(tmp_path, monkeypatch):
    original = "Refund rule: approve “partial refund” when amount ≤ 50.\n\uf0d8\nRequire receipt."
    state, _, _ = seed(tmp_path, [original])
    monkeypatch.setattr(compile_strategy, "render_single_stage_compiler", lambda base, stage, rules: rules)
    host, job = Host(), {"status": "running"}

    async def run():
        await host._staged_evidence_review_next(job, tmp_path, state)
        proposed = re.sub(r"\s+", " ", original.replace("“", '"').replace("”", '"').replace("\uf0d8", ""))
        value = response(host.current_packet, quote=proposed)
        raw_path = tmp_path / "children" / host.current["child"] / "result.txt"
        raw_path.write_text(review._json(value), encoding="utf-8")
        packet_path = tmp_path / host.current["packet_file"]
        packet_before, raw_before = packet_path.read_bytes(), raw_path.read_bytes()
        await host._staged_evidence_review_accept(job, tmp_path, state, host.current, value)
        await host._staged_evidence_review_accept(job, tmp_path, state, host.current, value)
        fragment = review._read(tmp_path / "evidence-review/fragments/00000000/00000000.json")
        assert fragment["format_restoration"] is True
        assert fragment["quote_restorations"][0]["sha256"] == review._sha(original)
        assert fragment["review"]["supporting_quotes"][0]["text"] == original
        assert raw_path.read_bytes() == raw_before and packet_path.read_bytes() == packet_before

    asyncio.run(run())
