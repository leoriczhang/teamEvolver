"""Inert tests for source-ID citations, complete coverage and one audited protocol upgrade."""

from __future__ import annotations

import asyncio
import copy
import tracemalloc

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import compile_strategy
from team_miner.skill_miner import deferred_evidence_review as review
from team_miner.skill_miner import evidence_quote_units as units
from tests.miner.skill_miner.support.deferred_evidence_review import LANGUAGE, Host, response, run_review, seed


def id_response(packet, outcome="candidate", *, selected=None, reason="Original rules support this procedure."):
    value = response(packet, outcome, reason=reason)
    value["schema_version"] = units.QUOTE_SCHEMA
    row = value["reviews"][0]
    row.pop("supporting_quotes")
    row["supporting_quote_ids"] = [packet["evidence"][0]["id"]] if selected is None else selected
    if not row["supporting_quote_ids"]:
        row["partial_steps"] = []
    else:
        row["partial_steps"] = [{"text": "Retain the original decision condition.", "quote_indexes": [0]}]
    return value


def packet_for(original):
    evidence = [units.source_quote_unit(original, index) for index in range(units.quote_unit_count(original))]
    return {
        "input_ids": [unit["id"] for unit in evidence],
        "evidence": evidence,
        "derived_controls": {"items": [{"item_id": "method-01"}], "allowed_source_refs": [original["source_uri"]]},
    }


def test_unicode_source_units_are_lossless_and_their_offsets_and_hashes_are_exact():
    text = "Refund “approved” ≤ 50.\n\uf0d8\n" + "中文😀\r\n" * 350
    original = planning.source_chunks(text, "viking://resources/refund.md")[0]
    original["ordinal"] = 9
    packet = packet_for(original)
    assert "".join(unit["content"] for unit in packet["evidence"]) == original["content"]
    assert all(len(unit["content"]) <= 768 for unit in packet["evidence"])
    planning.validate_chunks(packet["evidence"])
    for unit in packet["evidence"]:
        units.validate_source_quote_unit(unit, original)
    value = id_response(packet, selected=[packet["evidence"][-1]["id"]])
    before = copy.deepcopy(value)
    checked, audit = review.validate_quote_id_review(
        value, items=packet["derived_controls"]["items"], evidence=packet["evidence"]
    )
    assert value == before
    assert checked["reviews"][0]["supporting_quotes"][0]["text"] == packet["evidence"][-1]["content"]
    assert audit[0]["start_char"] == packet["evidence"][-1]["start_char"]
    assert audit[0]["original_ordinal"] == 9


@pytest.mark.parametrize("violation", ["unknown", "duplicate", "raw_text", "header", "source", "tamper"])
def test_quote_id_protocol_rejects_unassigned_ids_and_untrusted_source_mutation(violation):
    original = planning.source_chunks("Refund requires the original receipt.", "viking://resources/refund.md")[0]
    original["ordinal"] = 0
    packet = packet_for(original)
    value = id_response(packet)
    if violation == "unknown":
        value["reviews"][0]["supporting_quote_ids"] = ["src-unassigned"]
    if violation == "duplicate":
        value["reviews"][0]["supporting_quote_ids"] *= 2
    if violation == "raw_text":
        value["reviews"][0]["supporting_quotes"] = [{"source_uri": original["source_uri"], "text": "Invented rule"}]
    if violation == "header":
        value["input_ids"] = ["native-unassigned-range"]
    if violation == "source":
        value["reviews"][0]["source_refs"] = ["viking://unassigned/refund.md"]
    if violation == "tamper":
        packet["evidence"][0]["content"] += "Invented rule"
    with pytest.raises(ValueError):
        review.validate_quote_id_review(value, items=packet["derived_controls"]["items"], evidence=packet["evidence"])


def test_host_quote_expansion_does_not_raise_the_model_raw_output_limit():
    uri = "viking://resources/" + "scope-" * 40 + "document.md"
    original = planning.source_chunks("a" * 3072, uri)[0]
    original["ordinal"] = 0
    packet = packet_for(original)
    value = id_response(packet, selected=packet["input_ids"])
    row = value["reviews"][0]
    row["reason"] = "r" * 900
    row["partial_steps"] = [{"text": "p" * 400, "quote_indexes": [index % 4]} for index in range(8)]
    row["candidate"] = {
        "name": "procedure-review",
        "task": "t" * 400,
        "trigger": "t" * 400,
        "inputs": ["i" * 229 + str(index) for index in range(8)],
        "outputs": ["o" * 229 + str(index) for index in range(8)],
        "boundary": "b" * 400,
        "rationale": "r" * 400,
    }
    assert len(review._json(value)) < 12000
    checked, _ = review.validate_quote_id_review(
        value, items=packet["derived_controls"]["items"], evidence=packet["evidence"]
    )
    assert len(review._json(checked)) > 12000
    too_large = copy.deepcopy(value)
    too_large["reviews"][0]["reason"] = "r" * 13000
    with pytest.raises(ValueError, match="raw output"):
        review.validate_quote_id_review(
            too_large, items=packet["derived_controls"]["items"], evidence=packet["evidence"]
        )
    with pytest.raises(ValueError, match="within 12000"):
        review.validate_evidence_review(checked, items=packet["derived_controls"]["items"], evidence=packet["evidence"])


@pytest.mark.parametrize("count", [1, 9, 17, 120])
def test_new_review_defaults_to_ids_and_preserves_every_original_range(tmp_path, monkeypatch, count):
    state, chunks, _ = seed(
        tmp_path, [f"Rule {index}: refund equals eligible amount less prior refund." for index in range(count)]
    )
    state.pop("evidence_review_protocol")
    host, final = run_review(tmp_path, state, id_response, monkeypatch)
    assert state["evidence_review_protocol"] == units.QUOTE_PROTOCOL
    assert host.current["stage"] == units.QUOTE_STAGE
    assert final["outcome"] == "candidate"
    assert set(final["reviewed_input_ids"]) == {chunk["id"] for chunk in chunks}
    assert final["reviewed_quote_unit_count"] == count
    assert all(assignment["size"] <= 32000 for assignment in host.assignments)
    assert not (tmp_path / "evidence-review/fragments").exists()


def test_v2_complete_scope_deferral_and_zero_support_fold_never_submit_empty_source(tmp_path, monkeypatch):
    state, chunks, _ = seed(tmp_path, [f"Partial condition {index}." + "x" * 11500 for index in range(8)])
    state.pop("evidence_review_protocol")

    def responder(packet):
        combine = packet["derived_controls"]["review_mode"] == "combine"
        return id_response(packet, "insufficient_this_fragment", selected=[] if combine else None)

    host, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "deferred"
    assert set(final["reviewed_input_ids"]) == {chunk["id"] for chunk in chunks}
    assert all(assignment["ids"] and assignment["source_refs"] for assignment in host.assignments)
    assert any(assignment["mode"] == "combine" for assignment in host.assignments)


def test_v2_selected_repeated_text_keeps_selected_source_offsets_in_synthesis(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, ["x" * 768 + "a" * 768 + "a" * 768])
    state.pop("evidence_review_protocol")

    def responder(packet):
        selected = packet["evidence"][-1]["id"]
        return id_response(packet, "insufficient_this_fragment", selected=[selected])

    _, final = run_review(tmp_path, state, responder, monkeypatch)
    assert final["outcome"] == "deferred"
    initial = review._read(tmp_path / "evidence-review/synthesis/00000000/units/0/00000000.json")
    assert initial["quotes"][0]["start_char"] == 1536
    assert initial["quotes"][0]["end_char"] == 2304


def test_custom_business_policy_uses_smaller_full_packet_budget_and_retains_both_stage_policies(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, ["Refund rule. " * 900])
    state.pop("evidence_review_protocol")
    policy = {
        "common": "Common business rule. " * 1400,
        "stages": {review.STAGE: "Old review policy.", units.QUOTE_STAGE: "New ID policy."},
    }
    monkeypatch.setattr(compile_strategy, "compiler_business_policy", lambda base: policy)
    captured = []

    def render(base, stage, rules):
        body = (
            "---\nname: inert-policy-review\ndescription: Inert policy test\n---\n"
            + policy["common"]
            + policy["stages"][stage]
            + rules
        )
        captured.append(body)
        return body

    monkeypatch.setattr(compile_strategy, "render_single_stage_compiler", render)
    host = Host()
    asyncio.run(host._staged_evidence_review_next({"status": "running"}, tmp_path, state))
    assert captured[0].count("Old review policy.") == captured[0].count("New ID policy.") == 1
    assert state["evidence_review_quote_packet_budget"] < 32000
    assert len(review._json(host.current_packet)) <= state["evidence_review_quote_packet_budget"]


def test_twenty_megabyte_single_workflow_reads_bounded_original_chunks_and_packets(tmp_path, monkeypatch):
    state, chunks, _ = seed(tmp_path, ["Original refund procedure.\n" + "x" * (20 * 1024 * 1024)])
    state.pop("evidence_review_protocol")
    tracemalloc.start()
    host, final = run_review(tmp_path, state, id_response, monkeypatch)
    _, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    assert peak < 12 * 1024 * 1024
    assert len(final["reviewed_input_ids"]) == len(chunks)
    assert final["reviewed_quote_unit_count"] > len(chunks)
    assert all(assignment["size"] <= 32000 for assignment in host.assignments)


class UpgradeHost(Host):
    def __init__(self, *, fresh_stop=False):
        super().__init__()
        self.fresh_stop = fresh_stop
        self.row = {"job_id": "test-job", "status": "running"}

    def _ov_skill_mining_repository(self):
        return self

    def get_job(self, job_id):
        return {**self.row, "status": "stopping"} if self.fresh_stop else dict(self.row)

    def update_job(self, job_id, **changes):
        self.row.update(changes)
        return dict(self.row)


def failed_legacy_current(tmp_path, state, host, monkeypatch):
    monkeypatch.setattr(
        compile_strategy,
        "render_single_stage_compiler",
        lambda base, stage, rules: "---\nname: inert-legacy-review\ndescription: Inert legacy test\n---\n" + rules,
    )
    monkeypatch.setattr(compile_strategy, "compiler_business_policy", lambda base: {"common": "", "stages": {}})
    job = dict(host.row)
    asyncio.run(host._staged_evidence_review_next(job, tmp_path, state))
    current = host.current
    folder = tmp_path / "children" / current["child"]
    request, compiler = {"from": ["viking://frozen/input"], "to": "viking://frozen/output"}, "frozen legacy compiler"
    review._save(folder / "request.json", request)
    (folder / "compiler.md").write_text(compiler, encoding="utf-8")
    raw = review._json(response(host.current_packet, quote="Invented unsupported refund rule."))
    (folder / "result.txt").write_text(raw, encoding="utf-8")
    error = "Supporting quote 0 must literally occur in current original Evidence"
    current.update(
        attempt=2,
        task_id="completed-old-native-task",
        request_file=str((folder / "request.json").relative_to(tmp_path)),
        compiler_file=str((folder / "compiler.md").relative_to(tmp_path)),
        request_sha256=review._sha(review._json(request)),
        compiler_sha256=review._sha(compiler),
        context={**current["context"], "repair_error": error},
    )
    state["current"] = copy.deepcopy(current)
    predecessor = {
        **current,
        "child": "legacy-attempt-1",
        "attempt": 1,
        "context": {**current["context"], "repair_error": error},
    }
    review._save(tmp_path / "children/legacy-attempt-1/metadata.json", predecessor)
    immutable = {path: path.read_bytes() for path in folder.iterdir()}
    return job, error, immutable


def test_legacy_three_quote_failures_upgrade_once_with_old_artifacts_preserved(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, ["Refund rule requires the original receipt."])
    host = UpgradeHost()
    job, error, immutable = failed_legacy_current(tmp_path, state, host, monkeypatch)
    resumed = asyncio.run(host._staged_evidence_review_upgrade(job, tmp_path, state, error))
    assert resumed is not None and state["evidence_review_protocol"] == units.QUOTE_PROTOCOL
    assert host.current["stage"] == units.QUOTE_STAGE
    audit = review._read(tmp_path / state["evidence_review_quote_upgrade"])
    assert audit["old_result_status"] == "rejected"
    assert audit["old_current"]["task_id"] == "completed-old-native-task"
    assert all(path.read_bytes() == content for path, content in immutable.items())
    assert asyncio.run(host._staged_evidence_review_upgrade(resumed, tmp_path, state, error)) is None


def test_stop_during_upgrade_proof_returns_stopped_without_a_new_compile_or_audit(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, ["Refund rule requires the original receipt."])
    host = UpgradeHost(fresh_stop=True)
    job, error, immutable = failed_legacy_current(tmp_path, state, host, monkeypatch)
    previous = copy.deepcopy(state["current"])
    assignments = len(host.assignments)
    stopped = asyncio.run(host._staged_evidence_review_upgrade(job, tmp_path, state, error))
    assert stopped["status"] == "stopped" and stopped["finished_at"]
    assert state["current"] == previous
    assert len(host.assignments) == assignments
    assert not (tmp_path / "evidence-review/quote-upgrade.json").exists()
    assert all(path.read_bytes() == content for path, content in immutable.items())


def test_incomplete_quote_unit_journal_cannot_claim_the_original_chunk_is_fully_reviewed(tmp_path):
    state, _, _ = seed(tmp_path, ["Refund rule requires an original receipt. " * 260])
    review._prepare_review(tmp_path, 1)
    descriptor = review._read(tmp_path / "evidence-review/methods/00000000.json")
    manifest = review._quote_manifest(tmp_path, descriptor, 0)
    evidence, controls, after = review._quote_assignment(tmp_path, descriptor, manifest, 0, LANGUAGE, 4000)
    assert after < manifest["unit_count"]
    host = Host()
    context = {
        "review_proposal_index": 0,
        "review_item_id": descriptor["item"]["item_id"],
        "quote_unit_cursor": 0,
        "quote_unit_after": after,
        "input_ids": [unit["id"] for unit in evidence],
        "source_refs": controls["allowed_source_refs"],
        "ordinals": [0],
    }
    state["evidence_review_quote_packet_budget"] = 4000
    state["evidence_review_fragment_cursor"] = 0

    async def check():
        await host._staged_submit(
            {"status": "running"},
            tmp_path,
            state,
            units.QUOTE_STAGE,
            "evidence-review.json",
            evidence,
            controls,
            context,
        )
        value = id_response(host.current_packet)
        await host._staged_evidence_review_accept({"status": "running"}, tmp_path, state, host.current, value)
        await host._staged_evidence_review_accept({"status": "running"}, tmp_path, state, host.current, value)

    asyncio.run(check())
    with pytest.raises(ValueError, match="every ordered original unit"):
        review._finish_method(tmp_path, descriptor)


@pytest.mark.parametrize("budget", [32000, 16000])
def test_legal_large_parent_reviews_fold_with_bounded_control_projection_and_all_source_quotes(tmp_path, budget):
    state, originals, _ = seed(tmp_path, ["a" * 3072, "b" * 3072])
    descriptor = review._method_descriptor(tmp_path, 0, review._read(tmp_path / "proposals/00000000.json"))
    paths, source_ids = [], set()
    for index, original in enumerate(originals):
        packet = packet_for(original)
        value = id_response(packet, selected=packet["input_ids"])
        row = value["reviews"][0]
        row["reason"] = "r" * 900
        row["partial_steps"] = [{"text": "p" * 400, "quote_indexes": [i % 4]} for i in range(8)]
        row["candidate"] = {
            "name": "procedure-review",
            "task": "t" * 400,
            "trigger": "t" * 400,
            "inputs": ["i" * 229 + str(i) for i in range(11)],
            "outputs": ["o" * 229 + str(i) for i in range(11)],
            "boundary": "b" * 400,
            "rationale": "r" * 400,
        }
        checked, audit = review.validate_quote_id_review(
            value, items=packet["derived_controls"]["items"], evidence=packet["evidence"]
        )
        unit = review._support_unit(
            checked["reviews"][0],
            packet["evidence"],
            str(index),
            [],
            quotes=review._selected_source_quotes(packet["evidence"], audit),
        )
        path = tmp_path / "evidence-review/synthesis/00000000/units/0" / f"{index:08d}.json"
        review._save_immutable(path, unit)
        paths.append((path, path.read_bytes()))
        source_ids.update(packet["input_ids"])
    evidence, controls, after, _ = review._synthesis_assignment(
        tmp_path, descriptor, 0, 0, 2, LANGUAGE, units.QUOTE_STAGE, budget
    )
    packet = planning.build_stage_packet(
        units.QUOTE_STAGE,
        "evidence-review.json",
        evidence_chunks=evidence,
        controls=controls,
        language_policy=LANGUAGE,
        max_chars=budget,
    )
    assert len(review._json(packet)) <= budget and after == 2
    assert {unit["id"] for unit in evidence} == source_ids
    assert all(context["full_review_archived"] for context in controls["support_units"])
    assert all(path.read_bytes() == original for path, original in paths)


def test_v2_can_finish_after_legacy_prefix_without_rewriting_accepted_fragments(tmp_path, monkeypatch):
    state, originals, _ = seed(tmp_path, ["Initial rule.", "Remaining original condition. " * 180])
    review._prepare_review(tmp_path, 1)
    descriptor = review._read(tmp_path / "evidence-review/methods/00000000.json")
    checked = response(
        {
            "evidence": originals[:1],
            "input_ids": [originals[0]["id"]],
            "derived_controls": {"items": [descriptor["item"]], "allowed_source_refs": [originals[0]["source_uri"]]},
        }
    )
    checked = review.validate_evidence_review(checked, items=[descriptor["item"]], evidence=originals[:1])
    accepted = {
        "input_ids": [originals[0]["id"]],
        "ordinals": [0],
        "review": checked["reviews"][0],
        "child": "accepted-legacy-prefix",
    }
    path = tmp_path / "evidence-review/fragments/00000000/00000000.json"
    review._save_immutable(path, accepted)
    before = path.read_bytes()
    state.update(
        evidence_review_initialized=True,
        evidence_review_cursor=0,
        evidence_review_fragment_cursor=1,
        evidence_review_protocol=units.QUOTE_PROTOCOL,
    )
    _, final = run_review(tmp_path, state, id_response, monkeypatch)
    assert set(final["reviewed_input_ids"]) == {chunk["id"] for chunk in originals}
    assert path.read_bytes() == before


def test_upgrade_audit_recovers_after_checkpoint_failure_without_resubmitting_the_old_child(tmp_path, monkeypatch):
    state, _, _ = seed(tmp_path, ["Refund requires the original receipt."])
    host = UpgradeHost()
    job, error, immutable = failed_legacy_current(tmp_path, state, host, monkeypatch)
    review._save(tmp_path / "state.json", state)
    original_save = host._staged_save

    async def fail_after_audit(*args, **kwargs):
        if kwargs.get("status") == "preparing":
            raise OSError("injected checkpoint failure after audit")
        return await original_save(*args, **kwargs)

    monkeypatch.setattr(host, "_staged_save", fail_after_audit)
    with pytest.raises(OSError, match="checkpoint failure"):
        asyncio.run(host._staged_evidence_review_upgrade(job, tmp_path, state, error))
    assert len(host.assignments) == 1
    audit_bytes = (tmp_path / "evidence-review/quote-upgrade.json").read_bytes()
    monkeypatch.setattr(host, "_staged_save", original_save)
    restored = review._read(tmp_path / "state.json")
    asyncio.run(host._staged_evidence_review_upgrade(job, tmp_path, restored, error))
    assert host.current["stage"] == units.QUOTE_STAGE and len(host.assignments) == 2
    assert (tmp_path / "evidence-review/quote-upgrade.json").read_bytes() == audit_bytes
    assert all(path.read_bytes() == content for path, content in immutable.items())


def test_legacy_800_character_quotes_keep_step_indexes_when_split_for_v2(tmp_path):
    state, original, _ = seed(tmp_path, ["a" * 800 + "b" * 800])
    descriptor = review._method_descriptor(tmp_path, 0, review._read(tmp_path / "proposals/00000000.json"))
    packet = {
        "input_ids": [original[0]["id"]],
        "evidence": original,
        "derived_controls": {"items": [descriptor["item"]], "allowed_source_refs": [original[0]["source_uri"]]},
    }
    value = response(packet, "insufficient_this_fragment")
    row = value["reviews"][0]
    row["supporting_quotes"] = [
        {"source_uri": original[0]["source_uri"], "text": text} for text in ("a" * 800, "b" * 800)
    ]
    row["partial_steps"] = [{"text": "Rule B depends on the second original quote.", "quote_indexes": [1]}]
    checked = review.validate_evidence_review(value, items=[descriptor["item"]], evidence=original)
    unit = review._support_unit(checked["reviews"][0], original, "old-unit", [])
    review._save(tmp_path / "evidence-review/synthesis/00000000/units/0/00000000.json", unit)
    evidence, controls, _, _ = review._synthesis_assignment(tmp_path, descriptor, 0, 0, 1, LANGUAGE, units.QUOTE_STAGE)
    assert [len(piece["content"]) for piece in evidence] == [768, 32, 768, 32]
    assert controls["support_units"][0]["partial_steps"][0]["quote_indexes"] == [2, 3]
    selected = controls["support_units"][0]["quote_ids"]
    assert all(evidence[index]["id"] == selected[index] for index in (2, 3))


def test_actual_aux_renderer_preserves_common_old_and_new_business_policy_once(tmp_path):
    policy = {
        "schema_version": "te-mining-policy.v1",
        "common": "Use the documented refund limit.",
        "stages": {
            review.STAGE: "Respect the documented receipt exception.",
            units.QUOTE_STAGE: "Keep symbolic runtime inputs.",
        },
    }
    body = (
        "---\nname: source-policy\ndescription: Custom source policy\n---\n"
        + "```te-mining-policy.v1\n"
        + review._json(policy)
        + "\n```\n"
    )
    (tmp_path / "compiler-base.md").write_text(body, encoding="utf-8")
    state, host = {}, Host()
    asyncio.run(host._ensure_quote_review_compiler(tmp_path, state))
    actual = host.compiler[1]
    assert all(actual.count(text) == 1 for text in [policy["common"], *policy["stages"].values()])
    assert "supporting_quote_ids" in actual and "{source_uri,text} objects" not in actual
    from team_miner.skill_miner.ov_staged_orchestration import _package

    _, published = _package(actual)
    assert state["evidence_review_quote_packet_budget"] <= 48000 - len(published) - 3072
