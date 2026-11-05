"""Compact planning uses the real frozen host authorities and complete Case contract."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.candidate_identity import IDENTITY_PARTITION_PROTOCOL, candidate_id
from team_miner.skill_miner.candidate_refinement import BUSINESS_FIELDS, latest_refinement
from team_miner.skill_miner.compile_strategy import render_compact_aliased_workflow_compiler
from team_miner.skill_miner.direct_workflow_planning import CAPACITY_SCHEMA
from team_miner.skill_miner.evidence_quote_aliases import PROTOCOL as QUOTE_ALIAS_PROTOCOL
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _selection_packet, _sha
from team_miner.skill_miner.workflow_planning import COMPACT_PLANNING_PROTOCOL
from tests.miner.skill_miner.support.fresh_alias_and_case_group_contract import (
    AliasFake,
    AliasOwner,
    assert_frozen,
    dataset,
    first_selection,
    frozen_child,
)
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class CompactFake(AliasFake):
    """Synthetic model output, while every assignment/receipt/ledger stays production code."""

    def __init__(self):
        super().__init__()
        self.long_capacity = False
        self.refine_second_round = False
        self.refined_once = False
        self.duplicate_reuse_outputs = []
        self.reuse_one_row_per_method = False

    async def request(self, account, method, path, **kwargs):
        accepted = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile":
            return accepted
        if "/stages/extract_batch-" in request["from"][0]:
            prefix = request["from"][0] + "/"
            for uri, text in list(self.contents.items()):
                if not uri.startswith(prefix):
                    continue
                packet = json.loads(text)
                if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                    continue
                controls = packet["derived_controls"]
                aliases = {chunk_id: ref for ref, chunk_id in controls["reference_map"].items()}
                target = request["to"] + "/" + packet["output_path"]
                output = json.loads(self.contents[target])
                output["methods"] = [{"task": "退款复核", "boundary": "仅按原始政策复核",
                                      "source_refs": [aliases[chunk["id"]]]}
                                     for chunk in packet["evidence"]]
                self.contents[target] = _json(output)
        if "/stages/plan-" not in request["from"][0]:
            return accepted
        packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
        controls = packet["derived_controls"]
        target = request["to"] + "/selection.json"
        value = json.loads(self.contents[target])
        if value["schema_version"] == CAPACITY_SCHEMA:
            if self.long_capacity:
                value["reason"] = "Capacity diagnostic. " + "x" * (1151 - len("Capacity diagnostic. "))
            self.contents[target] = _json(value)
            return accepted
        row = value["skills"][0]
        if (self.refine_second_round and "/rounds/round-2/" in request["from"][0]
                and not self.refined_once):
            known = next(candidate for candidate in controls["known_candidates"]
                         if candidate["name"] == "refund-review")
            assert known["refinement_allowed"]
            row = {key: copy.deepcopy(known[key]) for key in BUSINESS_FIELDS}
            row.update(inputs=known["inputs"] + ["Request category"],
                       boundary="Classify the supplied request under its original policy before review.",
                       revise_candidate_id=known["candidate_id"],
                       base_metadata_sha256=known["base_metadata_sha256"],
                       reason="Clarify this same reviewed workflow using its assigned original methods.",
                       derived_from=value["skills"][0]["derived_from"])
            value["skills"] = [row]
            self.refined_once = True
        elif "reuse_candidate_id" in row and len(row["derived_from"]) > 1:
            midpoint = len(row["derived_from"]) // 2
            value["skills"] = [
                {**row, "reason": "First independent assigned subset supports this exact known identity.",
                 "derived_from": row["derived_from"][:midpoint]},
                {**row, "reason": "Second independent assigned subset supports this exact known identity.",
                 "derived_from": row["derived_from"][midpoint:]},
            ]
            if self.reuse_one_row_per_method:
                value["skills"] = [{**row, "reason": f"Exact assigned authority, independent subset {index}",
                                    "derived_from": [identity]}
                                   for index, identity in enumerate(row["derived_from"])]
            self.duplicate_reuse_outputs.append(copy.deepcopy(value))
        self.contents[target] = _json(value)
        return accepted


class CompactOwner(AliasOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = CompactFake()

    def new(self, count=8):
        job, entries = super().new(count)
        return self.repo.update_job(job["job_id"],
                                    staged_compact_planning_protocol=COMPACT_PLANNING_PROTOCOL,
                                    staged_quote_alias_protocol=QUOTE_ALIAS_PROTOCOL,
                                    staged_identity_partition_protocol=IDENTITY_PARTITION_PROTOCOL), entries


@pytest.mark.parametrize("count,expected_rows", [(32, 2), (48, 24)])
def test_duplicate_authorized_reuse_reaches_exact_method_ledger_and_keeps_frozen_sidecars(tmp_path, count,
                                                                                       expected_rows):
    async def check():
        owner = CompactOwner(tmp_path)
        owner.ov.reuse_one_row_per_method = expected_rows == 24
        job, root, state = await first_selection(owner, count)
        current = copy.deepcopy(state["current"])
        frozen = frozen_child(root, current)
        assert state["compact_planning_protocol"] == COMPACT_PLANNING_PROTOCOL
        assert state["quote_alias_protocol"] == QUOTE_ALIAS_PROTOCOL
        assert state["identity_partition_protocol"] == IDENTITY_PARTITION_PROTOCOL
        assert (root / "compilers/plan.md").read_text() == render_compact_aliased_workflow_compiler(SKILL_BODY)
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        _, final = await owner._staged_load(result)
        assert final["proposal_count"] == count and owner.ov.duplicate_reuse_outputs
        catalog = {_read(path)["item"]["item_id"]: _read(path)
                   for path in (root / "catalog/method").glob("*.json")}
        decisions = {path.stem: _read(path) for path in (root / "method-decisions").glob("*.json")}
        assert set(decisions) == set(catalog)
        assert all(row["target"] == "candidate" and row["name"] == "refund-review"
                   for row in decisions.values())
        expected = sorted({ordinal for row in catalog.values() for ordinal in row["ordinals"]})
        scope = _read(root / "scopes/refund-review.json")
        assert scope["method_ordinals"] == expected
        assert set(expected) <= set(scope["full_ordinals"])
        merged = []
        for path in (root / "children").glob("plan-*/metadata.json"):
            metadata = _read(path)
            canonical, aliases = _selection_packet(root, final, metadata)
            assert aliases and canonical["evidence"] == _read(root / metadata["packet_file"])["evidence"]
            audit = _read(root / metadata["reuse_coalescing_file"])
            assert _sha(_json(audit)) == metadata["reuse_coalescing_sha256"]
            if audit["merged_rows"]:
                raw = _read(root / "children" / metadata["child"] / "result.txt")
                assert len(raw["skills"]) == expected_rows
                merged.extend(audit["merged_rows"])
                assert len({row["reuse_candidate_id"] for row in raw["skills"]}) == 1
        assert len(merged) == 1 and len(merged[0]["original_rows"]) == expected_rows
        assert merged[0]["original_rows"][0]["reason"] != merged[0]["original_rows"][1]["reason"]
        assert len(dataset(owner, result)["cases"]) == 12
        assert result["candidate_quality"]["refund-review"]["can_submit"]
        assert_frozen(root, frozen)

    asyncio.run(check())


def test_fresh_diagnostic_1151_characters_is_one_capacity_split_without_model_repair(tmp_path):
    async def check():
        owner = CompactOwner(tmp_path)
        owner.ov.capacity_above = 4
        owner.ov.long_capacity = True
        job, root, state = await first_selection(owner, 8)
        current = copy.deepcopy(state["current"])
        frozen = frozen_child(root, current)
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        _, final = await owner._staged_load(result)
        assert final["capacity_splits"] == 1 and not final.get("validation_splits")
        assert len(owner.ov.selection_packets) == 3
        raw = _read(root / "children" / current["child"] / "result.txt")
        assert raw["schema_version"] == CAPACITY_SCHEMA and len(raw["reason"]) == 1151
        assert _read(root / "children" / current["child"] / "metadata.json")["attempt"] == 0
        assert len(list((root / "method-decisions").glob("*.json"))) == final["proposal_count"] == 8
        assert len(dataset(owner, result)["cases"]) == 12
        assert_frozen(root, frozen)

    asyncio.run(check())


def test_legacy_frozen_alias_protocol_keeps_1000_character_capacity_bound(tmp_path):
    async def check():
        owner = CompactOwner(tmp_path)
        create = owner.new

        def legacy(count=8):
            job, entries = create(count)
            return owner.repo.update_job(job["job_id"], staged_compact_planning_protocol="",
                                        staged_selection_recovery_protocol=""), entries

        owner.new = legacy
        owner.ov.force_capacity = owner.ov.long_capacity = True
        job, root, state = await first_selection(owner, 8)
        assert not state.get("compact_planning_protocol")
        original = frozen_child(root, state["current"])
        result = await drive(owner, job)
        assert result["status"] == "failed" and "diagnostic bound" in result["error"]
        _, final = await owner._staged_load(result)
        assert len(owner.ov.selection_packets) == 3 and not final.get("capacity_splits")
        assert not list((root / "method-decisions").glob("*.json"))
        assert not result["materialized"]
        assert_frozen(root, original)

    asyncio.run(check())


def test_compact_refinement_reaccepts_committed_journal_preserves_custom_twelve_cases_and_audit(tmp_path,
                                                                                             monkeypatch):
    async def check():
        import team_miner.skill_miner.ov_staged_part2 as staged

        owner = CompactOwner(tmp_path)
        job, entries = owner.new(3)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human", first.get("error")
        document = dataset(owner, first)
        custom_ids = [f"reviewed-custom:{index:02d}" for index in range(12)]
        for case, identity in zip(document["cases"], custom_ids, strict=True):
            case["case_id"] = identity
        _save(owner.repo.workspace(first) / "compiled_skill/refund-review/benchmark.json", document)
        owner.ov.refine_second_round = True
        second, entries = await next_inputs(owner, first)
        second = await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, "")
        commit = staged.commit_refinements
        crashed = False

        def commit_and_crash(root, journal):
            nonlocal crashed
            commit(root, journal)
            if journal["changes"] and not crashed:
                crashed = True
                raise RuntimeError("process exited after committed refinement")

        monkeypatch.setattr(staged, "commit_refinements", commit_and_crash)
        with pytest.raises(RuntimeError, match="committed refinement"):
            await drive(owner, second)
        second = owner.repo.get_job(second["job_id"])
        root, state = await owner._staged_load(second)
        current = _read(root / "children" / state["current"]["child"] / "metadata.json")
        frozen = frozen_child(root, current)
        native_count = len(owner.ov.calls)
        assert current["reuse_coalescing_file"]
        assert _read(root / current["canonical_packet_file"])["derived_controls"]["selection_schema"] == (
            planning.SELECTION_SCHEMA_V3
        )
        monkeypatch.setattr(staged, "commit_refinements", commit)
        resumed = CompactOwner(tmp_path)
        resumed.ov = resumed.fixture.ov = owner.ov
        second = await drive(resumed, await resumed._resume_staged_round(second))
        assert second["status"] == "waiting_human", second.get("error")
        assert len(resumed.ov.calls) > native_count  # New writers, while the accepted Plan is not resubmitted.
        assert sum(body["to"] == _read(root / current["request_file"])["to"]
                   for _, body in resumed.ov.calls) == 1
        assert [case["case_id"] for case in dataset(resumed, second)["cases"]] == custom_ids
        accepted = latest_refinement(root, candidate_id(second["job_id"], "refund-review"))
        assert "Request category" in accepted["after"]["inputs"]
        assert accepted["accepted_child"] == current["child"]
        assert len(list((root / "refinements/journals").glob("*.json"))) == 1
        assert_frozen(root, frozen)
        audit_path = root / current["reuse_coalescing_file"]
        audit = _read(audit_path)
        audit["all_exact_dispositions_preserved"] = False
        _save(audit_path, audit)
        with pytest.raises(ValueError, match="projection audit changed"):
            latest_refinement(root, candidate_id(second["job_id"], "refund-review"))

    asyncio.run(check())
