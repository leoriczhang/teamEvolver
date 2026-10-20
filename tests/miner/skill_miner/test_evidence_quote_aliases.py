"""Short quote identities retain strict assignments and exact original-source proofs."""

from __future__ import annotations

import asyncio
import copy
import json

import httpx
import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import deferred_evidence_review as review
from team_miner.skill_miner import evidence_quote_aliases as aliases
from team_miner.skill_miner import evidence_quote_units as units
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import STAGED_PROTOCOL
from tests.miner.skill_miner.support.deferred_evidence_review import seed
from tests.miner.skill_miner.support.evidence_quote_units import id_response
from tests.miner.skill_miner.support.ov_staged_orchestration import FakeCompile, Owner


def quote_packet(count=18, *, source="viking://resources/refund/中文😀.md", method="item-refund-method-opaque"):
    content = ("Refund “approved” ≤ 50. The literal m001/q001 strings are business text.\n" * 300)[:768 * count]
    original = planning.source_chunks(content, source, max_chars=768 * count)[0]
    original["ordinal"] = 7
    original["source_sha256"] = original["sha256"]
    evidence = [units.source_quote_unit(original, index) for index in range(units.quote_unit_count(original))]
    packet = planning.build_stage_packet(
        units.QUOTE_STAGE, "evidence-review.json", evidence_chunks=evidence,
        controls={"items": [{"item_id": method, "task": "Review q001 and m001 examples faithfully.",
                             "boundary": "Use original refund rules."}],
                  "allowed_source_refs": [source], "quotation_protocol": units.QUOTE_PROTOCOL},
    )
    assert len(evidence) == count
    return packet, original


@pytest.mark.parametrize("count", [12, 18])
def test_alias_roundtrip_resolves_actual_quote_text_offsets_and_hashes(count):
    canonical, original = quote_packet(count)
    before = copy.deepcopy(canonical)
    packet, manifest = aliases.build_quote_alias_packet(canonical)
    aliases.validate_quote_alias_manifest(manifest, packet=packet, canonical_packet=canonical)
    assert canonical == before
    assert packet["input_ids"] == [f"q{index:03d}" for index in range(1, count + 1)]
    assert packet["derived_controls"]["items"][0]["item_id"] == "m001"
    assert packet["derived_controls"]["items"][0]["task"] == canonical["derived_controls"]["items"][0]["task"]
    for short, actual in zip(packet["evidence"], canonical["evidence"]):
        assert {k: v for k, v in short.items() if k != "id"} == {k: v for k, v in actual.items() if k != "id"}
    value = id_response(packet, selected=[packet["evidence"][-1]["id"]])
    raw_before = copy.deepcopy(value)
    decoded = aliases.decode_quote_alias_output(value, manifest)
    checked, audit = review.validate_quote_id_review(
        decoded, items=canonical["derived_controls"]["items"], evidence=canonical["evidence"]
    )
    assert value == raw_before
    assert decoded["input_ids"] == canonical["input_ids"]
    assert checked["reviews"][0]["item_id"] == "item-refund-method-opaque"
    assert checked["reviews"][0]["source_refs"] == [original["source_uri"]]
    assert checked["reviews"][0]["supporting_quotes"][0]["text"] == original["content"][(count - 1) * 768:]
    assert audit[0]["start_char"] == (count - 1) * 768
    assert audit[0]["end_char"] == len(original["content"])
    assert audit[0]["sha256"] == canonical["evidence"][-1]["sha256"]
    assert audit[0]["original_chunk_id"] == original["id"]


def test_combine_quote_references_are_mapped_but_lineage_and_business_text_are_unchanged():
    canonical, _ = quote_packet(12)
    controls = canonical["derived_controls"]
    controls["input_ids"] = canonical["input_ids"][:]
    controls["support_units"] = [{"unit_id": "support-immutable-opaque", "quote_ids": canonical["input_ids"][:2],
                                  "partial_steps": [{"text": "q001/m001 are literal examples", "quote_indexes": [1]}]}]
    packet, manifest = aliases.build_quote_alias_packet(canonical)
    assert packet["derived_controls"]["input_ids"] == packet["input_ids"]
    support = packet["derived_controls"]["support_units"][0]
    assert support["quote_ids"] == ["q001", "q002"]
    assert support["unit_id"] == controls["support_units"][0]["unit_id"]
    assert support["partial_steps"] == controls["support_units"][0]["partial_steps"]
    aliases.validate_quote_alias_manifest(manifest, packet=packet, canonical_packet=canonical)


@pytest.mark.parametrize("violation", ["header-missing", "header-duplicate", "header-method", "quote-method",
                                       "unknown", "opaque", "method-quote", "method-missing", "method-duplicate",
                                       "quote-duplicate", "extra-field", "raw-quotes"])
def test_model_metadata_cannot_guess_or_cross_identity_roles(violation):
    canonical, _ = quote_packet(18)
    packet, manifest = aliases.build_quote_alias_packet(canonical)
    value = id_response(packet)
    row = value["reviews"][0]
    if violation == "header-missing":
        value["input_ids"].pop()
    elif violation == "header-duplicate":
        value["input_ids"].append("q001")
    elif violation == "header-method":
        value["input_ids"][0] = "m001"
    elif violation == "quote-method":
        row["supporting_quote_ids"] = ["m001"]
    elif violation == "unknown":
        row["supporting_quote_ids"] = ["q999"]
    elif violation == "opaque":
        row["supporting_quote_ids"] = [canonical["input_ids"][0]]
    elif violation == "method-quote":
        row["item_id"] = "q001"
    elif violation == "method-missing":
        value["reviews"] = []
    elif violation == "method-duplicate":
        value["reviews"].append(copy.deepcopy(row))
    elif violation == "quote-duplicate":
        row["supporting_quote_ids"] = ["q001", "q001"]
    elif violation == "extra-field":
        row["invented_role"] = "q001"
    else:
        row["supporting_quotes"] = [{"text": "model reconstruction", "source_uri": "viking://foreign"}]
    with pytest.raises(aliases.QuoteAliasOutputError):
        aliases.decode_quote_alias_output(value, manifest)


@pytest.mark.parametrize("violation", ["map", "packet", "canonical", "duplicate-map", "wrong-role-map"])
def test_frozen_maps_and_each_packet_are_bound_to_their_original_assignment(violation):
    canonical, _ = quote_packet(12)
    packet, manifest = aliases.build_quote_alias_packet(canonical)
    if violation == "map":
        manifest["quotes"]["q001"] = "src-different"
    elif violation == "packet":
        packet["evidence"][0]["content"] += "Invented rule"
    elif violation == "canonical":
        canonical["evidence"][0]["start_char"] += 1
    elif violation == "duplicate-map":
        manifest["quotes"]["q002"] = manifest["quotes"]["q001"]
        manifest["manifest_sha256"] = aliases._digest({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    else:
        manifest["methods"]["m001"] = manifest["quotes"]["q001"]
        manifest["manifest_sha256"] = aliases._digest({k: v for k, v in manifest.items() if k != "manifest_sha256"})
    with pytest.raises(aliases.QuoteAliasManifestError):
        aliases.validate_quote_alias_manifest(manifest, packet=packet, canonical_packet=canonical)


def test_same_short_names_in_another_packet_do_not_authorize_its_original_source_or_method():
    first, _ = quote_packet(12)
    second, _ = quote_packet(12, source="viking://resources/other.md", method="item-other-opaque")
    packet_a, map_a = aliases.build_quote_alias_packet(first)
    packet_b, map_b = aliases.build_quote_alias_packet(second)
    assert packet_a["input_ids"] == packet_b["input_ids"]
    assert map_a["quotes"]["q001"] != map_b["quotes"]["q001"]
    with pytest.raises(aliases.QuoteAliasManifestError):
        aliases.validate_quote_alias_manifest(map_a, packet=packet_b, canonical_packet=second)
    raw = id_response(packet_a)
    decoded = aliases.decode_quote_alias_output(raw, map_b)
    with pytest.raises(ValueError, match="references"):
        review.validate_quote_id_review(decoded, items=second["derived_controls"]["items"], evidence=second["evidence"])


def test_unselected_current_units_remain_original_evidence_not_a_proven_business_gap():
    # A generic four-step procedure spans supplied units; representative quote
    # selection does not establish semantic completeness or absence of other units.
    originals = ["x" * 768, "Step 1: receive refund request. Step 2: verify receipt." + "x" * 709,
                 "Step 3: compute eligible refund. Step 4: confirm customer decision." + "x" * 701]
    content = "".join(originals)
    source = planning.source_chunks(content, "viking://resources/refund-procedure.md")[0]
    source["ordinal"] = 0
    evidence = [units.source_quote_unit(source, index) for index in range(units.quote_unit_count(source))]
    canonical = planning.build_stage_packet(units.QUOTE_STAGE, "evidence-review.json", evidence_chunks=evidence,
                                           controls={"items": [{"item_id": "item-refund-method"}],
                                                     "allowed_source_refs": [source["source_uri"]]})
    packet, manifest = aliases.build_quote_alias_packet(canonical)
    decoded = aliases.decode_quote_alias_output(id_response(packet, selected=["q003"]), manifest)
    checked, audit = review.validate_quote_id_review(
        decoded, items=canonical["derived_controls"]["items"], evidence=evidence
    )
    assert "Step 1" in packet["evidence"][1]["content"]
    assert "Step 2" in packet["evidence"][1]["content"]
    assert audit[0]["source_quote_id"] == evidence[2]["id"]
    assert len(checked["reviews"][0]["supporting_quotes"]) == 1
    assert "Not selecting a unit" in review.QUOTE_ALIAS_INSTRUCTIONS
    # No assertion that this structural check proves the model's business judgment.


class QuoteNative(FakeCompile):
    """Inert unchanged public Compile endpoint; only the model response is synthetic."""

    async def request(self, account, method, path, **kwargs):
        if path == "/api/v1/compile" and method == "POST":
            body = kwargs["json"]
            packet = json.loads(self.contents[body["from"][0] + "/packet.json"])
            if packet["stage"] == units.QUOTE_STAGE:
                assert set(body) == {"from", "to", "skill", "instruction"}
                assert all(uri in self.directories for uri in body["from"])
                key = kwargs["extra_headers"]["Idempotency-Key"]
                self.calls.append((key, copy.deepcopy(body)))
                if key not in self.accepted:
                    task_id = "cmp_" + str(len(self.accepted))
                    self.accepted[key] = (task_id, copy.deepcopy(body))
                    self.tasks[task_id] = {"task_id": task_id, "status": "completed"}
                    self.contents[body["to"] + "/evidence-review.json"] = review._json(id_response(packet))
                else:
                    task_id, frozen = self.accepted[key]
                    assert frozen == body
                if self.lose_ack:
                    self.lose_ack = False
                    raise httpx.ReadTimeout("Inert lost acknowledgement after exact-key acceptance")
                return {"task_id": task_id}
        return await super().request(account, method, path, **kwargs)


class QuoteOwner(Owner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = QuoteNative()


async def start_quote_owner(owner, *, aliased=True, lose_ack=False):
    job, _ = owner.new(12)
    job = owner.repo.update_job(job["job_id"], status="running")
    root = owner._staged_root(job)
    state, _, _ = seed(root, [f"Refund procedure {index}: verify receipt, calculate and confirm. "
                             + "x" * 650 for index in range(12)])
    state.pop("evidence_review_protocol")
    if aliased:
        state["quote_alias_protocol"] = aliases.PROTOCOL
    (root / "compiler-base.md").write_text(SKILL_BODY, encoding="utf-8")
    (root / "revision.txt").write_text("", encoding="utf-8")
    state.update(base_sha256=review._sha(SKILL_BODY), round=1, protocol=STAGED_PROTOCOL)
    owner.ov.lose_ack = lose_ack
    job = await owner._staged_evidence_review_next(job, root, state)
    return job, root, state


def frozen_child_bytes(root, current):
    return {key: (root / current[key]).read_bytes() for key in (
        "request_file", "compiler_file", "packet_file", "canonical_packet_file", "quote_alias_file"
    ) if key in current}


def test_real_host_accept_after_restart_restores_canonical_scope_and_keeps_original_result(tmp_path):
    async def check():
        owner = QuoteOwner(tmp_path)
        job, root, state = await start_quote_owner(owner)
        current = copy.deepcopy(state["current"])
        frozen = frozen_child_bytes(root, current)
        actual = review._read(root / current["packet_file"])
        canonical, manifest = owner._staged_canonical_packet_for_current(root, state, current)
        assert actual["input_ids"][0] == "q001" and canonical["input_ids"][0].startswith("src-")
        assert manifest["methods"]["m001"] == current["context"]["review_item_id"]
        raw = id_response(actual, selected=[actual["evidence"][-1]["id"]])
        result_file = root / "children" / current["child"] / "result.txt"
        result_file.write_text(review._json(raw), encoding="utf-8")
        before = result_file.read_bytes()
        second = QuoteOwner(tmp_path)
        await second._staged_evidence_review_accept(job, root, copy.deepcopy(state), current, raw)
        fragment = review._read(root / "evidence-review/quote-fragments/00000000/00000000.json")
        assert fragment["input_ids"] == canonical["input_ids"]
        assert fragment["review"]["item_id"] == current["context"]["review_item_id"]
        audit = fragment["source_quote_audit"][0]
        assert audit["source_quote_id"] == canonical["evidence"][-1]["id"]
        assert audit["start_char"] == canonical["evidence"][-1]["start_char"]
        assert audit["sha256"] == canonical["evidence"][-1]["sha256"]
        assert result_file.read_bytes() == before
        assert frozen_child_bytes(root, current) == frozen
        assert not second.ov.calls

    asyncio.run(check())


@pytest.mark.parametrize("damage", ["quote_alias_file", "canonical_packet_file", "packet_file"])
def test_real_host_resume_rejects_frozen_sidecar_damage_before_post_or_acceptance(tmp_path, damage):
    async def check():
        owner = QuoteOwner(tmp_path)
        job, root, state = await start_quote_owner(owner, lose_ack=True)
        assert not job.get("ov_task_id") and len(owner.ov.accepted) == 1
        current = state["current"]
        file = root / current[damage]
        file.write_text(file.read_text() + " ", encoding="utf-8")
        if damage == "quote_alias_file":
            changed = review._read(file)
            changed["quotes"]["q001"] = "src-unauthorized"
            review._save(file, changed)
        second = QuoteOwner(tmp_path)
        second.ov = owner.ov
        from team_miner.skill_miner.direct_workflow_planning import FrozenPlanningIntegrityError

        with pytest.raises(FrozenPlanningIntegrityError):
            second._staged_canonical_packet_for_current(root, state, current)
        with pytest.raises(FrozenPlanningIntegrityError):
            await second._resume_staged_round(job)
        assert len(second.ov.calls) == 1
        assert not list((root / "evidence-review/quote-fragments").rglob("*.json"))

    asyncio.run(check())


def test_real_host_only_restored_machine_id_expansion_can_exceed_the_unchanged_model_output_bound(tmp_path):
    async def check():
        owner = QuoteOwner(tmp_path)
        job, root, state = await start_quote_owner(owner)
        current = state["current"]
        packet = review._read(root / current["packet_file"])
        canonical, manifest = owner._staged_canonical_packet_for_current(root, state, current)
        raw = id_response(packet, selected=packet["input_ids"][:4])
        row = raw["reviews"][0]
        row.update(reason="r" * 900, partial_steps=[{"text": "p" * 400, "quote_indexes": [i % 4]}
                                                  for i in range(8)])
        row["candidate"] = {"name": "refund-review", "task": "t" * 400, "trigger": "t" * 400,
                            "inputs": ["i" * 228 + f"{i:02d}" for i in range(10)],
                            "outputs": ["o" * 228 + f"{i:02d}" for i in range(11)],
                            "boundary": "b" * 400, "rationale": "r" * 400}
        assert len(review._json(raw)) <= 12000
        decoded = aliases.decode_quote_alias_output(raw, manifest)
        assert len(review._json(decoded)) > 12000
        with pytest.raises(ValueError, match="raw output"):
            review.validate_quote_id_review(decoded, items=canonical["derived_controls"]["items"],
                                           evidence=canonical["evidence"])
        await owner._staged_evidence_review_accept(job, root, state, current, raw)
        assert (root / "evidence-review/quote-fragments/00000000/00000000.json").is_file()
        too_large = copy.deepcopy(raw)
        too_large["reviews"][0]["reason"] = "r" * 13000
        with pytest.raises(aliases.QuoteAliasOutputError, match="12000"):
            await owner._staged_evidence_review_accept(job, root, state, current, too_large)

    asyncio.run(check())


def test_fresh_quote_compiler_preserves_custom_common_and_both_review_policies_verbatim(tmp_path):
    from team_miner.skill_miner.compile_strategy import TE_STAGED_COMPILE, mining_policy_block
    from tests.miner.skill_miner.support.deferred_evidence_review import Host

    common = "  General refund policy: preserve ≤ and currency.\n q001 stays business text.  "
    old_policy, quote_policy = "\nOriginal custom Evidence-review policy.\n", "\nCustom quote-stage policy.\n"
    body = ("---\nname: explicit-refund-compiler\ndescription: Frozen policy\n---\n" + TE_STAGED_COMPILE
            + mining_policy_block(common, {review.STAGE: old_policy, units.QUOTE_STAGE: quote_policy}))
    (tmp_path / "compiler-base.md").write_text(body, encoding="utf-8")
    host, state = Host(), {"quote_alias_protocol": aliases.PROTOCOL}
    asyncio.run(host._ensure_quote_review_compiler(tmp_path, state))
    contract = json.loads(host.compiler[1].split("```json\n", 1)[1].split("\n```", 1)[0])
    instruction = contract["synthesize"]["instructions"]
    assert instruction.count(common) == instruction.count(old_policy) == instruction.count(quote_policy) == 1
    assert "Only packet.evidence[].id q001" in instruction
    assert "Not selecting a unit" in instruction
    assert (tmp_path / "compiler-base.md").read_text() == body
    old_compiler = host.compiler
    state["quote_alias_protocol"] = "late-new-unknown-marker"
    asyncio.run(host._ensure_quote_review_compiler(tmp_path, state))
    assert host.compiler == old_compiler  # Frozen bodies never migrate on later metadata.


@pytest.mark.parametrize("stop", [False, True])
def test_real_host_lost_ack_recovers_exact_quote_request_and_stop_never_submits_again(tmp_path, stop):
    async def check():
        owner = QuoteOwner(tmp_path)
        job, root, state = await start_quote_owner(owner, lose_ack=True)
        assert not job.get("ov_task_id") and len(owner.ov.accepted) == 1
        current = copy.deepcopy(state["current"])
        frozen = frozen_child_bytes(root, current)
        if stop:
            job = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user")
        second = QuoteOwner(tmp_path)
        second.ov = owner.ov
        job = await second._resume_staged_round(job)
        assert job["ov_task_id"] == owner.ov.accepted[current["key"]][0]
        assert job["status"] == ("stopping" if stop else "queued")
        assert len(second.ov.calls) == 1
        assert frozen_child_bytes(root, current) == frozen
        if stop:
            job = await second._advance_staged_round(job, second.ov.tasks[job["ov_task_id"]])
            assert job["status"] == "stopped" and len(second.ov.calls) == 1

    asyncio.run(check())


def test_legacy_frozen_quote_current_and_compiler_do_not_migrate_from_late_job_marker(tmp_path):
    async def check():
        owner = QuoteOwner(tmp_path)
        job, root, state = await start_quote_owner(owner, aliased=False, lose_ack=True)
        current = copy.deepcopy(state["current"])
        frozen = frozen_child_bytes(root, current)
        compiler = (root / "aux-compilers" / (units.QUOTE_STAGE + ".md")).read_bytes()
        assert "quote_alias_protocol" not in state and "quote_alias_protocol" not in current
        job = owner.repo.update_job(job["job_id"], staged_quote_alias_protocol=aliases.PROTOCOL)
        second = QuoteOwner(tmp_path)
        second.ov = owner.ov
        resumed = await second._resume_staged_round(job)
        assert resumed["ov_task_id"] and len(second.ov.calls) == 1
        assert frozen_child_bytes(root, current) == frozen
        reloaded = review._read(root / "state.json")
        assert "quote_alias_protocol" not in reloaded
        assert (root / "aux-compilers" / (units.QUOTE_STAGE + ".md")).read_bytes() == compiler

    asyncio.run(check())
