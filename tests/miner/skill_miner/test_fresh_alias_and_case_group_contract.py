"""Fresh machine aliases and Case groups retain the host's exact frozen authorities."""

from __future__ import annotations

import asyncio
import copy
import hashlib
import json
from pathlib import Path

import httpx
import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.candidate_identity import candidate_id
from team_miner.skill_miner.candidate_refinement import BUSINESS_FIELDS, latest_refinement
from team_miner.skill_miner.ov_case_batch_orchestration import _batch_ledger
from team_miner.skill_miner.ov_case_group_orchestration import (
    CASE_GROUP_COMPILER_STAGE,
    CASE_GROUP_PROTOCOL,
    _group_packet,
    render_case_group_compiler,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import (
    _chunks,
    _json,
    _method_groups,
    _package,
    _read,
    _save,
    _selection_packet,
)
from team_miner.skill_miner.planning_aliases import PROTOCOL, PlanningAliasManifestError
from tests.miner.skill_miner.support import admission
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.local_scope_pipeline import LocalScopeOwner
from tests.miner.skill_miner.support.ov_case_group_orchestration import GroupFake, assigned_indices
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs


class AliasFake(DirectFake):
    def __init__(self):
        super().__init__()
        self.bad_aliases = []
        self.lose_plan_ack = False

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/plan-" not in request["from"][0]:
            return result
        target = request["to"] + "/selection.json"
        value = json.loads(self.contents[target])
        if self.bad_aliases:
            mutation = self.bad_aliases.pop(0)
            origins = value["skills"][0]["derived_from"]
            if mutation == "unknown":
                origins[0] = "m999"
            elif mutation == "duplicate":
                origins.append(origins[0])
            elif mutation == "missing":
                origins.pop()
            elif mutation == "role":
                origins[0] = value["input_ids"][0]
            self.contents[target] = json.dumps(value, ensure_ascii=False)
        if self.lose_plan_ack:
            self.lose_plan_ack = False
            raise httpx.ReadTimeout("Plan accepted but its acknowledgement was lost")
        return result


class AliasOwner(LocalScopeOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = AliasFake()

    def new(self, count=8):
        job, entries = super().new(count)
        # The original fixture's long documents are unnecessary for metadata recovery.
        # Distinct complete local originals still travel through real extraction/scoping.
        for index, entry in enumerate(entries):
            text = (f"Refund procedure {index}: collect the request, verify the supplied policy "
                    "and explain the result.\n")
            path = Path(entry["local_path"])
            path.write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()),
                         relative_path=f"original/原始政策-{index}-" + "源" * 80 + ".md")
        return self.repo.update_job(job["job_id"], staged_planning_alias_protocol=PROTOCOL,
                                    staged_case_group_protocol=""), entries


async def first_selection(owner, count=8):
    job = await begin(owner, count)
    for _ in range(100):
        root, state = await owner._staged_load(job)
        if (state.get("current") or {}).get("stage") == "plan":
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("No bounded selection assignment was submitted")


def frozen_child(root, current):
    folder = root / "children" / current["child"]
    return {str(path.relative_to(root)): path.read_bytes() for path in folder.iterdir()
            if path.is_file() and path.name != "metadata.json"}


def assert_frozen(root, originals):
    assert all((root / path).read_bytes() == content for path, content in originals.items())


def dataset(owner, job):
    return _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")


def test_packet_local_aliases_restore_every_canonical_method_and_keep_original_sources(tmp_path):
    async def check():
        owner = AliasOwner(tmp_path)
        owner.ov.capacity_above = 1
        job, root, state = await first_selection(owner, 10)
        first = copy.deepcopy(state["current"])
        originals = frozen_child(root, first)
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        children = [_read(path) for path in (root / "children").glob("plan-*/metadata.json")]
        assignments = []
        for current in children:
            canonical, manifest = _selection_packet(root, state, current)
            actual = _read(root / current["packet_file"])
            assert manifest and current["planning_alias_protocol"] == PROTOCOL
            assert actual["derived_controls"]["items"][0]["item_id"] == "m001"
            assert actual["evidence"] == canonical["evidence"]
            assert [row["task"] for row in actual["derived_controls"]["items"]] == [
                row["task"] for row in canonical["derived_controls"]["items"]
            ]
            assert len(planning._json(actual)) <= 32000
            assignments.append(manifest["methods"]["m001"])
        assert len(set(assignments)) > 1  # m001 is deliberately reused only within its own packet.
        final_state = (await owner._staged_load(result))[1]
        decisions = list((root / "method-decisions").glob("*.json"))
        catalog_ids = {_read(path)["item"]["item_id"] for path in (root / "catalog/method").glob("*.json")}
        assert {path.stem for path in decisions} == catalog_ids
        assert len(decisions) == final_state["proposal_count"]
        assert _read(root / "scopes/refund-review.json")["method_ordinals"] == list(range(final_state["chunk_count"]))
        assert len(dataset(owner, result)["cases"]) == 12
        allowed = set(_read(root / "plan.json")["skills"][0]["source_refs"])
        assert all(set(case["provenance"]["session_ids"]) <= allowed for case in dataset(owner, result)["cases"])
        assert not any(ref.startswith(("m00", "c00", "u00")) for ref in allowed)
        assert_frozen(root, originals)

    asyncio.run(check())


@pytest.mark.parametrize("mutation", ["unknown", "missing", "duplicate", "role"])
def test_model_alias_errors_retry_without_accepting_partial_dispositions(tmp_path, mutation):
    async def check():
        owner = AliasOwner(tmp_path)
        owner.ov.bad_aliases = [mutation]
        job, root, state = await first_selection(owner)
        original = copy.deepcopy(state["current"])
        frozen = frozen_child(root, original)
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        assert len(owner.ov.selection_packets) == 2
        decisions = list((root / "method-decisions").glob("*.json"))
        assert len(decisions) == (await owner._staged_load(result))[1]["proposal_count"]
        raw = json.loads((root / "children" / original["child"] / "result.txt").read_text())
        assert raw["skills"][0]["derived_from"] != list(
            _read(root / original["planning_alias_file"])["methods"]
        ) or mutation == "role"
        assert_frozen(root, frozen)
        assert len(dataset(owner, result)["cases"]) == 12

    asyncio.run(check())


@pytest.mark.parametrize("tamper", ["map", "canonical", "packet"])
def test_frozen_alias_authority_damage_fails_before_any_new_native_submission(tmp_path, tamper):
    async def check():
        owner = AliasOwner(tmp_path)
        job, root, state = await first_selection(owner)
        current = state["current"]
        field = {"map": "planning_alias_file", "canonical": "canonical_packet_file", "packet": "packet_file"}[tamper]
        path = root / current[field]
        value = _read(path)
        if tamper == "map":
            value["methods"]["m001"] = "unassigned-canonical-method"
        else:
            value["derived_controls"]["items"][0]["boundary"] += " damaged frozen authority"
        _save(path, value)
        before = len(owner.ov.calls)
        with pytest.raises((PlanningAliasManifestError, ValueError), match="[Ff]rozen|SHA|alias|冻结"):
            await owner._resume_staged_round(job)
        assert len(owner.ov.calls) == before
        assert not list((root / "method-decisions").glob("*.json"))

    asyncio.run(check())


def test_alias_lost_ack_resume_and_user_stop_keep_the_same_frozen_request(tmp_path):
    async def check():
        owner = AliasOwner(tmp_path)
        owner.ov.lose_plan_ack = True
        job, root, state = await first_selection(owner)
        current = copy.deepcopy(state["current"])
        assert not job["ov_task_id"] and current["key"] in owner.ov.accepted
        originals = frozen_child(root, current)
        count = len(owner.ov.calls)
        stopped = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user stop")
        resumed = AliasOwner(tmp_path)
        resumed.ov = resumed.fixture.ov = owner.ov
        job = await resumed._resume_staged_round(stopped)
        assert job["status"] == "stopping" and job["ov_task_id"] == owner.ov.accepted[current["key"]][0]
        assert len(owner.ov.calls) == count
        assert_frozen(root, originals)
        job = await resumed._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "stopped" and len(owner.ov.calls) == count
        assert not list((root / "method-decisions").glob("*.json"))
        assert_frozen(root, originals)

    asyncio.run(check())


def test_frozen_selection_cannot_gain_aliases_from_a_late_job_setting(tmp_path):
    async def check():
        owner = AliasOwner(tmp_path)
        create = owner.new

        def legacy_new(count=8):
            job, entries = create(count)
            return owner.repo.update_job(job["job_id"], staged_planning_alias_protocol=""), entries

        owner.new = legacy_new
        job, root, state = await first_selection(owner)
        current = copy.deepcopy(state["current"])
        frozen = frozen_child(root, current)
        assert not state.get("planning_alias_protocol")
        assert _selection_packet(root, state, current)[1] is None
        job = owner.repo.update_job(job["job_id"], staged_planning_alias_protocol=PROTOCOL)
        result = await drive(owner, job)
        assert result["status"] == "waiting_human", result.get("error")
        assert not (await owner._staged_load(result))[1].get("planning_alias_protocol")
        assert_frozen(root, frozen)
        assert len(dataset(owner, result)["cases"]) == 12

    asyncio.run(check())


class GroupContractFake(AliasFake, GroupFake):
    def __init__(self):
        super().__init__()
        self.scoped_sources = False
        self.refine_second_round = False

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile":
            return result
        if self.scoped_sources and "/stages/extract_batch-" in request["from"][0]:
            prefix = request["from"][0] + "/"
            for uri, text in list(self.contents.items()):
                if not uri.startswith(prefix):
                    continue
                packet = json.loads(text)
                controls = packet["derived_controls"]
                reverse = {chunk_id: ref for ref, chunk_id in controls["reference_map"].items()}
                value = json.loads(self.contents[request["to"] + "/" + packet["output_path"]])
                value.update(methods=[], facts=[], deferred=[], context_refs=[], context_uncertain=False)
                for chunk in packet["evidence"]:
                    ref = reverse[chunk["id"]]
                    if "原始政策-0-" in chunk["source_uri"] or "原始政策-1-" in chunk["source_uri"]:
                        value["methods"].append({"task": "退款复核", "boundary": "仅按原始政策复核",
                                                 "source_refs": [ref]})
                    else:
                        value["facts"].append({"text": "Mandatory original policy constraint.", "source_refs": [ref]})
                        value["context_refs"].append(ref)
                self.contents[request["to"] + "/" + packet["output_path"]] = _json(value)
        if (self.refine_second_round and "/rounds/round-2/" in request["from"][0]
                and "/stages/plan-" in request["from"][0]):
            packet = json.loads(self.contents[request["from"][0] + "/packet.json"])
            known = next(row for row in packet["derived_controls"]["known_candidates"]
                         if row["name"] == "refund-review")
            assert known["candidate_id"].startswith("c") and known["refinement_allowed"]
            uri = request["to"] + "/selection.json"
            value = json.loads(self.contents[uri])
            row = {key: copy.deepcopy(known[key]) for key in BUSINESS_FIELDS}
            row.update(inputs=known["inputs"] + ["Request category"],
                       boundary="Classify the supplied request under its original policy before review.",
                       revise_candidate_id=known["candidate_id"],
                       base_metadata_sha256=known["base_metadata_sha256"],
                       reason="Retain the reviewed identity while clarifying its supported execution boundary.",
                       derived_from=value["skills"][0]["derived_from"])
            value["skills"] = [row]
            self.contents[uri] = _json(value)
        return result


class GroupContractOwner(AliasOwner):
    def __init__(self, root, *, scoped_sources=False):
        super().__init__(root)
        self.ov = self.fixture.ov = GroupContractFake()
        self.ov.scoped_sources = scoped_sources

    def new(self, count=8):
        job, entries = super().new(count)
        if self.ov.scoped_sources:
            assert count == 5
            for index, (entry, size) in enumerate(zip(entries, [11000, 11000, 4800, 4800, 4800], strict=True)):
                lead = "Original procedure.\n" if index < 2 else "Mandatory original policy constraint.\n"
                text = lead + "x" * (size - len(lead))
                Path(entry["local_path"]).write_text(text, encoding="utf-8")
                entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        job = self.repo.update_job(job["job_id"], staged_case_group_protocol=CASE_GROUP_PROTOCOL,
                                   staged_source_fragment_protocol="", staged_source_input_clarity_protocol="")
        return job, entries


async def first_group(owner, count=8):
    job = await begin(owner, count)
    for _ in range(200):
        root, state = await owner._staged_load(job)
        if (state.get("current") or {}).get("input_protocol") == CASE_GROUP_PROTOCOL:
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("No grouped Case assignment was submitted")


def assert_group_bounds(root):
    for metadata in (root / "children").glob("case_batch-*/metadata.json"):
        current = _read(metadata)
        if current.get("input_protocol") != CASE_GROUP_PROTOCOL:
            continue
        locators = _read(root / current["packet_file"])
        assert 1 <= len(locators) <= 4
        body = (root / current["compiler_file"]).read_text()
        request = _read(root / current["request_file"])
        assert set(request) == {"from", "to", "skill", "instruction"} and request["from"] == [current["input_uri"]]
        for locator in locators:
            packet = _read(root / locator["local_path"])
            assert 1 <= len(packet["derived_controls"]["cases"]) <= 4
            assert len(packet["evidence"]) <= 8 and len(_json(packet["evidence"])) <= 26000
            assert len(_json(packet)) <= 32000
            assert len(body) + len(_json(packet)) + len(request["instruction"]) <= 48000


def test_alias_and_group_partial_repair_keep_all_good_case_siblings_and_frozen_inputs(tmp_path):
    async def check():
        owner = GroupContractOwner(tmp_path)
        owner.ov.group_break_slots = {1: ["invalid"], 7: ["missing"]}
        job, root, state = await first_group(owner)
        frozen = frozen_child(root, state["current"])
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        siblings = {path.name: path.read_bytes() for path in (root / "cases/refund-review").glob("*.json")}
        assert len(siblings) == 10
        final = await drive(owner, job)
        assert final["status"] == "waiting_human", final.get("error")
        indices = assigned_indices(owner)
        assert indices.count(1) == indices.count(7) == 2
        assert all(indices.count(index) == 1 for index in range(12) if index not in {1, 7})
        assert all((root / "cases/refund-review" / name).read_bytes() == text for name, text in siblings.items())
        assert_frozen(root, frozen)
        cases = dataset(owner, final)["cases"]
        assert len(cases) == len({case["case_id"] for case in cases}) == 12
        assert all(set(case) == planning.CASE_KEYS for case in cases)
        assert_group_bounds(root)

    asyncio.run(check())


def test_distinct_original_case_scopes_are_isolated_before_model_execution(tmp_path):
    async def check():
        owner = GroupContractOwner(tmp_path, scoped_sources=True)
        next_cases = owner._staged_case_batch_next
        submitted = False

        async def submit_legitimate_mixed_assignments(job, root, state, candidate, scope, **kwargs):
            nonlocal submitted
            if submitted:
                return await next_cases(job, root, state, candidate, scope, **kwargs)
            submitted = True
            groups = _method_groups(root, scope)
            ids = [f"{candidate['name']}-case-{index + 1:02d}" for index in range(12)]
            ledger = _batch_ledger(root, candidate["name"], ids, 0)
            assignments = []
            from team_miner.skill_miner.ov_case_batch_orchestration import case_source_roles

            for index in (4, 10):
                group_index = index * len(groups) // 12
                chunks = _chunks(root, groups[group_index])
                context = {
                    "skill_name": candidate["name"], "case_id": ids[index], "case_index": index,
                    "case_group_index": group_index, "case_group_count": len(groups),
                    "input_ids": [chunk["id"] for chunk in chunks],
                    **case_source_roles(chunks, scope),
                    "source_refs": list(dict.fromkeys(chunk["source_uri"] for chunk in chunks)),
                    "format_attempts": 0, "group_attempt": 0,
                }
                assignments.append((ledger["slots"][index], context, chunks))
            await owner._ensure_aux_compiler(root, state, CASE_GROUP_COMPILER_STAGE,
                                             render_case_group_compiler((root / "compiler-base.md").read_text()))
            body = (root / "aux-compilers" / (CASE_GROUP_COMPILER_STAGE + ".md")).read_text()
            _, published = _package(body)
            with pytest.raises(ValueError, match="Different Case source scopes"):
                _group_packet(root, state, candidate, assignments, published)
            return await next_cases(job, root, state, candidate, scope, **kwargs)

        # A union that fits the transport budget still must not mix semantic assignments.
        owner._staged_case_batch_next = submit_legitimate_mixed_assignments
        final = await drive(owner, await begin(owner, 5))
        assert final["status"] == "waiting_human", final.get("error")
        root, _ = await owner._staged_load(final)
        scope = _read(root / "scopes/refund-review.json")
        assert scope["method_ordinals"] == [0, 1]
        assert scope["shared_context_ordinals"] == [2, 3, 4]
        packets = [packet for batch in owner.ov.group_batches for packet in batch]
        assert len(packets) > 1
        for packet in packets:
            assignments = packet["derived_controls"]["cases"]
            assert len({tuple(own["input_ids"]) for own in assignments}) == 1
            assert assignments[0]["input_ids"] == packet["input_ids"]
        assert len(dataset(owner, final)["cases"]) == 12
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert all(slot["format_attempts"] == 0 for slot in ledger["slots"])
        assert sorted(assigned_indices(owner)) == list(range(12))
        assert_group_bounds(root)

    asyncio.run(check())


def test_capacity_fallback_retains_all_twelve_cases_and_each_original_assignment(tmp_path):
    async def check():
        owner = GroupContractOwner(tmp_path)
        owner.ov.case_capacity_above = 1
        final = await drive(owner, await begin(owner, 10))
        assert final["status"] == "waiting_human", final.get("error")
        root, _ = await owner._staged_load(final)
        ledger = _read(root / "case-batches/refund-review/ledger.json")
        assert all(slot["status"] == "valid" and slot["format_attempts"] == 0 for slot in ledger["slots"])
        assert len(dataset(owner, final)["cases"]) == 12 and owner.ov.batches
        assert_group_bounds(root)
        for batch in owner.ov.batches:
            assert 1 <= len(batch) <= 8
            assert all(len(_json(packet)) <= 32000 for packet in batch)
        scope_uris = set(_read(root / "plan.json")["skills"][0]["source_refs"])
        assert all(set(case["provenance"]["session_ids"]) <= scope_uris for case in dataset(owner, final)["cases"])

    asyncio.run(check())


def test_group_unknown_ack_stop_recovers_exact_task_without_new_work(tmp_path):
    async def check():
        owner = GroupContractOwner(tmp_path)
        owner.ov.lose_group_ack = True
        job, root, state = await first_group(owner)
        current = copy.deepcopy(state["current"])
        frozen = frozen_child(root, current)
        assert not job["ov_task_id"]
        count = len(owner.ov.calls)
        stopped = owner.repo.update_job(job["job_id"], status="stopping", ov_cancel_reason="user stop")
        restarted = GroupContractOwner(tmp_path)
        restarted.ov = restarted.fixture.ov = owner.ov
        job = await restarted._resume_staged_round(stopped)
        assert job["status"] == "stopping" and job["ov_task_id"] == owner.ov.accepted[current["key"]][0]
        job = await restarted._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert job["status"] == "stopped" and len(owner.ov.calls) == count
        assert not list((root / "cases/refund-review").glob("*.json"))
        assert_frozen(root, frozen)

    asyncio.run(check())


@pytest.mark.parametrize("refine", [False, True])
def test_reviewed_group_round_preserves_custom_case_ids_through_alias_reuse_or_refinement(tmp_path, refine):
    async def check():
        owner = GroupContractOwner(tmp_path)
        job, entries = owner.new(3)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=3, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human", first.get("error")
        first_root, _ = await owner._staged_load(first)
        document = dataset(owner, first)
        custom_ids = [f"reviewed-case:{index:02d}" for index in range(12)]
        for case, case_id in zip(document["cases"], custom_ids, strict=True):
            case["case_id"] = case_id
        _save(owner.repo.workspace(first) / "compiled_skill/refund-review/benchmark.json", document)
        originals = {str(path.relative_to(first_root)): path.read_bytes()
                     for folder in (first_root / "children", first_root / "chunks", first_root / "compilers")
                     for path in folder.rglob("*") if path.is_file()}
        before = len(owner.ov.group_batches), len(owner.ov.batches), len(owner.ov.artifacts)
        owner.ov.refine_second_round = refine
        second, entries = await next_inputs(owner, first)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["planning_alias_protocol"] == PROTOCOL and state["case_group_protocol"] == CASE_GROUP_PROTOCOL
        assert [case["case_id"] for case in dataset(owner, second)["cases"]] == custom_ids
        if refine:
            assert "Request category" in _read(root / "plan.json")["skills"][0]["inputs"]
            assert len(owner.ov.group_batches) > before[0]
            assert not state.get("reused_candidate_count")
            identity = candidate_id(second["job_id"], "refund-review")
            accepted = latest_refinement(root, identity)
            assert "Request category" in accepted["after"]["inputs"]
        else:
            # Human editing the completed R1 Test Dataset is not an already validated
            # cache entry. R2 verifies/regenerates those exact custom identities first.
            assert not state.get("reused_candidate_count")
            assert len(owner.ov.group_batches) > before[0]
            verified_counts = len(owner.ov.group_batches), len(owner.ov.batches), len(owner.ov.artifacts)
            third, entries = await next_inputs(owner, second)
            third = await drive(owner, await owner._prepare_staged_round(third, SKILL_BODY, entries, POLICY, ""))
            assert third["status"] == "waiting_human", third.get("error")
            root, state = await owner._staged_load(third)
            assert state["reused_candidate_count"] == 1
            assert (len(owner.ov.group_batches), len(owner.ov.batches), len(owner.ov.artifacts)) == verified_counts
            assert [case["case_id"] for case in dataset(owner, third)["cases"]] == custom_ids
        # prepare_next_round archives R1 without rewriting its private staged files.
        archive = owner.repo._job_dir(first["job_id"]) / "rounds/round-1/input/staged"
        assert_frozen(archive, originals)
        assert_group_bounds(root)

        if refine:
            current = _read(root / "children" / accepted["accepted_child"] / "metadata.json")
            manifest_path = root / current["planning_alias_file"]
            manifest = _read(manifest_path)
            original_map = manifest_path.read_bytes()
            manifest["candidates"]["c001"] = "candidate-" + "f" * 24
            _save(manifest_path, manifest)
            with pytest.raises(ValueError, match="SHA|alias|[Ff]rozen"):
                latest_refinement(root, identity)
            manifest_path.write_bytes(original_map)
            canonical_path = root / current["canonical_packet_file"]
            canonical = _read(canonical_path)
            canonical["derived_controls"]["known_candidates"][0]["base_metadata_sha256"] = "0" * 64
            _save(canonical_path, canonical)
            with pytest.raises(ValueError, match="SHA|alias|[Ff]rozen"):
                latest_refinement(root, identity)

    asyncio.run(check())


@pytest.mark.parametrize("changed_marker", ["staged_planning_alias_protocol", "staged_case_group_protocol"])
def test_reviewed_profile_change_regenerates_cases_instead_of_reusing_incompatible_transport(tmp_path, changed_marker):
    async def check():
        owner = GroupContractOwner(tmp_path)
        job, entries = owner.new(3)
        manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert first["status"] == "waiting_human", first.get("error")
        first_ids = [case["case_id"] for case in dataset(owner, first)["cases"]]
        before = len(owner.ov.group_batches), len(owner.ov.batches)
        second, entries = await next_inputs(owner, first)
        second = owner.repo.update_job(second["job_id"], **{changed_marker: ""})
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert not state.get("reused_candidate_count")
        assert (len(owner.ov.group_batches), len(owner.ov.batches)) != before
        assert [case["case_id"] for case in dataset(owner, second)["cases"]] == first_ids
        assert not _read(root / "reuse/manifest.json")["candidate_count"]

    asyncio.run(check())


def test_real_group_hosts_share_two_native_slots_and_do_not_submit_a_third_pending_job(tmp_path):
    async def check():
        first = GroupContractOwner(tmp_path)
        first_job, first_root, first_state = await first_group(first, 3)
        first.ov.tasks[first_job["ov_task_id"]]["status"] = "running"
        second = GroupContractOwner(tmp_path)
        second.ov = second.fixture.ov = first.ov
        second_job, _, _ = await first_group(second, 3)
        first.ov.tasks[second_job["ov_task_id"]]["status"] = "running"
        assert len(admission.reservations(first.repo.jobs_root)) == 2
        third = GroupContractOwner(tmp_path)
        third.ov = third.fixture.ov = first.ov
        calls = len(first.ov.calls)
        third_job = await begin(third, 3)
        assert not third_job["ov_task_id"] and len(first.ov.calls) == calls
        assert len(admission.reservations(first.repo.jobs_root)) == 2
        frozen = frozen_child(first_root, first_state["current"])
        first_job = first.repo.update_job(first_job["job_id"], status="stopping", ov_cancel_reason="user stop")
        first.ov.tasks[first_job["ov_task_id"]]["status"] = "completed"
        first_job = await first._advance_staged_round(first_job, first.ov.tasks[first_job["ov_task_id"]])
        assert first_job["status"] == "stopped"
        third_job = await third._resume_staged_round(third_job)
        assert third_job["ov_task_id"] and len(first.ov.calls) == calls + 1
        assert len(admission.reservations(first.repo.jobs_root)) == 2
        assert_frozen(first_root, frozen)

    asyncio.run(check())
