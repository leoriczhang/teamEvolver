"""Fresh uncertainty-unit routing preserves frozen originals and reviewed-round contracts."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import pytest

from team_miner import bounded_compile_plan as planning
from team_miner.candidate_refinement import mapped_previous_source_scope, merge_candidate_scopes
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _json, _read, _save
from team_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3, UNCERTAINTY_SCOPE_PROTOCOL
from team_miner.tests.test_compact_host_contract import CompactFake, CompactOwner
from team_miner.tests.test_extract_batches import begin
from team_miner.tests.test_fresh_alias_and_case_group_contract import dataset
from team_miner.tests.test_ov_staged_orchestration import drive
from team_miner.tests.test_round_reuse import POLICY, next_inputs


class UnitContextFake(CompactFake):
    """Only the first long-document window has uncertain linkage; later text is ordinary notes."""

    async def request(self, account, method, path, **kwargs):
        accepted = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/extract_batch-" not in request["from"][0]:
            return accepted
        prefix = request["from"][0] + "/"
        for uri, text in list(self.contents.items()):
            if not uri.startswith(prefix):
                continue
            packet = json.loads(text)
            if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                continue
            controls = packet["derived_controls"]
            refs = {identity: ref for ref, identity in controls["reference_map"].items()}
            target = request["to"] + "/" + packet["output_path"]
            value = json.loads(self.contents[target])
            value.update(methods=[], facts=[], deferred=[], context_refs=[], context_uncertain=False)
            for chunk in packet["evidence"]:
                ref = refs[chunk["id"]]
                is_long = "原始政策-0-" in chunk["source_uri"]
                if not is_long or chunk["ordinal"] == 0:
                    value["methods"].append({"task": "退款复核", "boundary": "仅按原始政策复核",
                                             "source_refs": [ref]})
                else:
                    value["facts"].append({"text": "Original supplied example note.", "source_refs": [ref]})
                if chunk["ordinal"] == 0:
                    value.update(context_uncertain=True, context_refs=[ref])
            self.contents[target] = _json(value)
        return accepted


class UnitContextOwner(CompactOwner):
    def __init__(self, root, *, enabled=True):
        super().__init__(root)
        self.ov = self.fixture.ov = UnitContextFake()
        self.enabled = enabled

    def new(self, count=2):
        assert count == 2
        job, entries = super().new(count)
        text = ("Original table heading. Review a supplied request under original conditions. "
                "The mandatory condition has unresolved applicability within this first window.\n"
                + "Ordinary example note, retained verbatim for source audit.\n" * 450)
        Path(entries[0]["local_path"]).write_text(text, encoding="utf-8")
        entries[0].update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return self.repo.update_job(job["job_id"], staged_uncertainty_scope_protocol=(
            UNCERTAINTY_SCOPE_PROTOCOL if self.enabled else ""
        )), entries


def assert_raw_source_complete(root, entries):
    chunks = [_read(path) for path in sorted((root / "chunks").glob("*.json"))]
    for entry in entries:
        original = Path(entry["local_path"]).read_text(encoding="utf-8")
        name = Path(entry["relative_path"]).name
        selected = [chunk for chunk in chunks if Path(chunk["source_uri"]).name == name]
        assert selected and selected[0]["start_char"] == 0
        assert all(left["end_char"] == right["start_char"] for left, right in zip(selected, selected[1:]))
        assert selected[-1]["end_char"] == len(original)
        assert "".join(chunk["content"] for chunk in selected) == original
        assert all(hashlib.sha256(chunk["content"].encode()).hexdigest() == chunk["sha256"] for chunk in selected)
        assert hashlib.sha256(original.encode()).hexdigest() == entry["sha256"]


async def first_reviewed(owner):
    job, entries = owner.new()
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    return first, entries


def test_actual_prepare_freezes_unit_profile_and_only_routes_unknown_unit_without_losing_raw(tmp_path):
    async def check():
        owner = UnitContextOwner(tmp_path)
        first, entries = await first_reviewed(owner)
        root, state = await owner._staged_load(first)
        routing = _read(root / "source-routing/context.json")
        scope = _read(root / "scopes/refund-review.json")
        assert state["uncertainty_scope_protocol"] == routing["uncertainty_scope_protocol"] == (
            scope["uncertainty_scope_protocol"]
        ) == UNCERTAINTY_SCOPE_PROTOCOL
        assert state["source_scope_protocol"] == scope["scope_protocol"] == SOURCE_SCOPE_PROTOCOL_V3
        unknown = next(unit for unit in routing["unit_contexts"] if unit["context_uncertain"])
        assert unknown["method_ordinals"] and len(unknown["ordinals"]) == 3
        assert set(unknown["ordinals"]) <= set(scope["shared_context_ordinals"])
        # The first assigned window ends inside a longer file. Its continuation
        # must be globally shared, even for a method belonging to the other file.
        source = _read(root / "chunk-metadata" / f"{unknown['ordinals'][-1]:08d}.json")["source_uri"]
        same_file = [index for index in range(state["chunk_count"])
                     if _read(root / "chunk-metadata" / f"{index:08d}.json")["source_uri"] == source]
        after = same_file[same_file.index(unknown["ordinals"][-1]) + 1]
        assert set(routing["uncertain_context_ordinals"]) == set(unknown["ordinals"]) | {after}
        assert after in scope["shared_context_ordinals"]
        assert len(scope["ordinals"]) < len(scope["full_ordinals"])
        assert set(scope["method_ordinals"]) <= set(scope["ordinals"]) <= set(scope["full_ordinals"])
        assert len(scope["full_ordinals"]) == state["chunk_count"]
        assert_raw_source_complete(root, entries)
        assert len(dataset(owner, first)["cases"]) == 12
        assert first["candidate_quality"]["refund-review"]["can_submit"]

    asyncio.run(check())


def test_late_job_profile_cannot_migrate_a_frozen_v3_round(tmp_path):
    async def check():
        owner = UnitContextOwner(tmp_path, enabled=False)
        job = await begin(owner, 2)
        root, state = await owner._staged_load(job)
        assert not state.get("uncertainty_scope_protocol")
        frozen = {str(path.relative_to(root)): path.read_bytes()
                  for folder in (root / "compilers", root / "chunks") for path in folder.glob("*")}
        job = owner.repo.update_job(job["job_id"], staged_uncertainty_scope_protocol=UNCERTAINTY_SCOPE_PROTOCOL)
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        _, state = await owner._staged_load(result)
        scope = _read(root / "scopes/refund-review.json")
        assert not state.get("uncertainty_scope_protocol") and not scope.get("uncertainty_scope_protocol")
        assert scope["ordinals"] == scope["full_ordinals"] == list(range(state["chunk_count"]))
        assert all((root / relative).read_bytes() == content for relative, content in frozen.items())
        assert len(dataset(owner, result)["cases"]) == 12

    asyncio.run(check())


def test_reviewed_round_mapper_identity_union_and_cache_keep_exact_new_profile_and_twelve_cases(tmp_path):
    async def check():
        owner = UnitContextOwner(tmp_path)
        first, _ = await first_reviewed(owner)
        first_ids = [case["case_id"] for case in dataset(owner, first)["cases"]]
        before = len(owner.ov.artifacts), len(owner.ov.batches)
        second, entries = await next_inputs(owner, first)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["uncertainty_scope_protocol"] == UNCERTAINTY_SCOPE_PROTOCOL
        assert state["reused_candidate_count"] == 1 and state["reused_extract_count"] > 0
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) == before
        assert [case["case_id"] for case in dataset(owner, second)["cases"]] == first_ids
        scope = _read(root / "scopes/refund-review.json")
        mapped = mapped_previous_source_scope(second, root, "refund-review", [entry["relative_path"]
                                             for entry in entries if entry["kind"] == "source"])
        assert mapped["uncertainty_scope_protocol"] == UNCERTAINTY_SCOPE_PROTOCOL
        assert set(mapped["shared_context_ordinals"]) <= set(scope["shared_context_ordinals"])
        raw_scope = {"ordinals": [state["chunk_count"] - 1], "method_ordinals": [state["chunk_count"] - 1]}
        combined = merge_candidate_scopes(mapped, raw_scope)
        assert combined["uncertainty_scope_protocol"] == UNCERTAINTY_SCOPE_PROTOCOL
        assert set(mapped["ordinals"]) | set(raw_scope["ordinals"]) <= set(combined["ordinals"])
        mismatched = copy.deepcopy(mapped)
        mismatched.pop("uncertainty_scope_protocol")
        with pytest.raises(ValueError, match="uncertainty routing profiles"):
            merge_candidate_scopes(mapped, mismatched)
        # A complete custom identity set is validated without editing any archived artifact.
        cases = copy.deepcopy(dataset(owner, second)["cases"])
        custom_ids = [f"opaque-reviewed:{index:02d}" for index in range(12)]
        for case, identity in zip(cases, custom_ids, strict=True):
            case["case_id"] = identity
        document = planning.assemble_dataset(cases, skill_name="refund-review", task="退款复核",
                                             identity="custom-identity-contract", expected_case_ids=custom_ids)
        assert [case["case_id"] for case in document["cases"]] == custom_ids
        assert len(document["cases"]) == 12
        assert_raw_source_complete(root, entries)

    asyncio.run(check())


def test_profile_change_is_a_cache_miss_without_relabeling_old_approved_scope(tmp_path):
    async def check():
        owner = UnitContextOwner(tmp_path, enabled=False)
        first, _ = await first_reviewed(owner)
        first_root, _ = await owner._staged_load(first)
        original_scope = _read(first_root / "scopes/refund-review.json")
        assert not original_scope.get("uncertainty_scope_protocol")
        before = len(owner.ov.artifacts), len(owner.ov.batches)
        second, entries = await next_inputs(owner, first)
        second = owner.repo.update_job(second["job_id"], staged_uncertainty_scope_protocol=UNCERTAINTY_SCOPE_PROTOCOL)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["uncertainty_scope_protocol"] == UNCERTAINTY_SCOPE_PROTOCOL
        assert not state.get("reused_candidate_count") and _read(root / "reuse/manifest.json")["candidate_count"] == 0
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) != before
        assert _read(first_root / "scopes/refund-review.json") == original_scope
        assert len(dataset(owner, second)["cases"]) == 12

    asyncio.run(check())
