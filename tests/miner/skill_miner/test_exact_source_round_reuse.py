"""Reviewed adjacent rounds reuse exact source associations and complete twelve-Case bundles."""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.compile_strategy import render_stage_batch_compiler
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_staged_orchestration import _read, _save, _sha
from team_miner.skill_miner.round_reuse import (
    PROTOCOL_FIELDS,
    _profile,
    _sources,
    _writer_compilers,
    try_reuse_candidate,
    try_reuse_extract,
    try_reuse_source_unit_extract,
)
from team_miner.skill_miner.skill_body_patch import OUTPUT_SCHEMA
from team_miner.skill_miner.source_unit_extraction import next_source_unit_assignment
from team_miner.skill_miner.source_unit_scope import _digest
from team_miner.skill_miner.source_units import PROTOCOL, freeze_source_units, load_source_units
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.extract_batches import ExtractOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, candidate_and_scope, next_inputs, prepared_only


class PatchFake(DirectFake):
    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/artifact_batch-" not in request["from"][0]:
            return result
        for uri in sorted(self.contents):
            if not uri.startswith(request["from"][0] + "/"):
                continue
            packet = json.loads(self.contents[uri])
            controls = packet["derived_controls"].get("body_patch")
            if controls is None:
                continue
            atom = controls["next_atom_id"]
            value = {
                "schema_version": OUTPUT_SCHEMA, "assignment_sha256": controls["assignment_sha256"],
                "base_state_sha256": controls["base_state_sha256"], "input_ids": controls["input_ids"],
                "title": "完整退款复核方法", "add": [], "attach": [],
                "step_order": list(controls["step_order"]), "no_change_reason": "本批只有来源上下文。",
            }
            if controls["operative_ids"]:
                value.update(no_change_reason="", add=[{
                    "id": atom, "kind": "step", "text": "确认申请并按原始政策复核。",
                    "support_ids": [controls["operative_ids"][0]],
                }], step_order=[*controls["step_order"], atom])
            self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
        return result


class ExactReuseOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root, full=True)
        self.fixture.ov = self.ov = PatchFake()
        self.pause_next = False

    async def _staged_next(self, job, root, state):
        if self.pause_next:
            return await self._staged_save(job, root, state)
        return await super()._staged_next(job, root, state)

    def new(self, count=1):
        job, entries = super().new(count)
        job = self.repo.update_job(job["job_id"], max_rounds=3,
                                   staged_source_scope_protocol="te-source-scope.v3",
                                   staged_workflow_protocol="te-direct-workflow-planning.v1",
                                   staged_source_unit_protocol=PROTOCOL,
                                   staged_skill_body_protocol="te-skill-body-patch.v1")
        for entry in entries:
            text = "# 退款复核\n\n| 适用条件 | 操作 |\n| --- | --- |\n| 已收到申请 | 先确认再核实政策。 |\n"
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries


async def first_round(owner):
    job, entries = owner.new()
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], input_manifest=str(manifest))
    result = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert result["status"] == "waiting_human", result.get("error")
    return result


def rebound_candidate(prior_root, root, name, manifest):
    candidate, scope = candidate_and_scope(root, name)
    previous = load_source_units(prior_root, scope["source_units"]["unit_ids"])
    current = load_source_units(root, list(range(manifest["unit_count"])))
    def key(row):
        return row["source_uri"].split("/source/", 1)[1], row["start_char"], row["end_char"], row["sha256"]
    by_range = {key(row): row["id"] for row in current}
    mapping = {row["id"]: by_range[key(row)] for row in previous}
    precise = scope["source_units"]
    for field in ("unit_ids", "operative_ids", "context_ids"):
        precise[field] = [mapping[identity] for identity in precise[field]]
    for row in precise["method_groups"]:
        row["unit_ids"] = [mapping[identity] for identity in row["unit_ids"]]
    rows = load_source_units(root, precise["unit_ids"])
    precise["source_sha256"] = _digest([{field: row[field] for field in
                                       ("id", "source_uri", "start_char", "end_char", "sha256", "source_sha256")}
                                      for row in rows])
    precise["sha256"] = _digest({field: value for field, value in precise.items() if field != "sha256"})
    return candidate, scope


def test_patch_writer_sha_and_source_unit_protocol_are_frozen_dependencies():
    assert "source_unit_protocol" in PROTOCOL_FIELDS
    old = _writer_compilers(SKILL_BODY, {})
    patch = _writer_compilers(SKILL_BODY, {"skill_body_protocol": "te-skill-body-patch.v1"})
    assert patch["skill_section_batch"] == _sha(
        render_stage_batch_compiler(SKILL_BODY, "skill_section", body_patch=True)
    )
    assert patch["skill_section_batch"] != old["skill_section_batch"]
    assert {key: value for key, value in patch.items() if key != "skill_section_batch"} == {
        key: value for key, value in old.items() if key != "skill_section_batch"
    }


@pytest.mark.asyncio
async def test_real_first_round_exact_units_patch_and_twelve_cases_then_no_feedback_bundle_reuse(tmp_path):
    owner = ExactReuseOwner(tmp_path)
    first = await first_round(owner)
    prior_root, old = await owner._staged_load(first)
    assert old["source_unit_protocol"] == PROTOCOL and old["body_patch_complete"]
    before = len(owner.ov.artifacts), len(owner.ov.batches)
    job, entries = await next_inputs(owner, first)
    owner.pause_next = True
    job = await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, "")
    root, state = await owner._staged_load(job)
    manifest = _read(root / "source-units/manifest.json")
    header = _read(root / "reuse/manifest.json")
    assert header["extract_count"] and header["candidate_count"] == 1
    context = next_source_unit_assignment(root, cursor=0, count=manifest["unit_count"],
                                          unit_index=0, language_policy=POLICY)
    assert not try_reuse_extract(job, root, state, [], context)
    assert try_reuse_source_unit_extract(job, root, state, context)
    assert state["source_unit_cursor"] == old["source_unit_count"] < manifest["unit_count"]
    assert state["unit_count"] == 1
    rebound = _read(root / "units/0/00000000.json")
    assert rebound["source_unit_protocol"] == PROTOCOL
    assert rebound["assigned_unit_ids"] == context["assigned_unit_ids"][:old["source_unit_count"]]
    assert rebound["source_unit_bindings"] and all(
        identity in context["source_unit_ids"] for ids in rebound["source_unit_bindings"].values() for identity in ids
    )
    name = old["candidate_names"][0]
    candidate, scope = rebound_candidate(prior_root, root, name, manifest)
    assert try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
    cases = [_read(root / "cases" / name / f"{index:02d}.json") for index in range(12)]
    old_cases = _read(owner.repo._job_dir(job["job_id"]) /
                      f"rounds/round-1/reviewed_output/compiled_skill/{name}/benchmark.json")["cases"]
    assert [row["case_id"] for row in cases] == [row["case_id"] for row in old_cases]
    assert all(row["provenance"]["session_ids"] == candidate["source_refs"] for row in cases)
    assert before == (len(owner.ov.artifacts), len(owner.ov.batches))


@pytest.mark.asyncio
async def test_actual_second_round_routes_same_exact_units_and_reuses_all_twelve_cases(tmp_path):
    owner = ExactReuseOwner(tmp_path)
    first = await first_round(owner)
    before_artifacts, before_cases = len(owner.ov.artifacts), len(owner.ov.batches)
    job, entries = await next_inputs(owner, first)
    result = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert result["status"] == "waiting_human", result.get("error")
    root, state = await owner._staged_load(result)
    assert state["reused_extract_count"] == state["reused_candidate_count"] == 1
    assert state["source_unit_cursor"] == state["source_unit_count"]
    assert (len(owner.ov.artifacts), len(owner.ov.batches)) == (before_artifacts, before_cases)
    name = state["candidate_names"][0]
    document = _read(owner.repo.workspace(result) / f"compiled_skill/{name}/benchmark.json")
    assert len(document["cases"]) == len({row["case_id"] for row in document["cases"]}) == 12
    assert all(ref.startswith(result["source_snapshot"] + "/original/")
               for row in document["cases"] for ref in row["provenance"]["session_ids"])


@pytest.mark.asyncio
@pytest.mark.parametrize("change", ["feedback", "method_association", "exact_scope_missing", "body_protocol"])
async def test_exact_reviewed_bundle_safely_misses_on_unknown_feedback_or_changed_dependencies(tmp_path, change):
    owner = ExactReuseOwner(tmp_path)
    first = await first_round(owner)
    prior_root, old = await owner._staged_load(first)
    job, entries = await next_inputs(owner, first, feedback="请核实本次所有流程。" if change == "feedback" else "")
    if change == "body_protocol":
        job = owner.repo.update_job(job["job_id"], staged_skill_body_protocol="te-incremental-skill-body.v1")
    owner.pause_next = True
    job = await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, "")
    root, state = await owner._staged_load(job)
    manifest = _read(root / "source-units/manifest.json")
    candidate, scope = rebound_candidate(prior_root, root, old["candidate_names"][0], manifest)
    if change == "method_association":
        scope["source_units"]["method_groups"][0]["boundary"] = "改变了实际独立终态"
        scope["source_units"]["sha256"] = _digest({key: value for key, value in scope["source_units"].items()
                                                   if key != "sha256"})
    elif change == "exact_scope_missing":
        scope.pop("source_units")
    assert not try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
    assert not (root / "cases").exists()
    assert list((root / "reuse/misses").glob("*.json"))


@pytest.mark.asyncio
async def test_new_exact_protocol_never_loads_archived_chunk_extract_units(tmp_path):
    from tests.miner.skill_miner.support.round_reuse import ReuseOwner, reviewed_first

    owner = ReuseOwner(tmp_path)
    first = await reviewed_first(owner)
    job, entries = await next_inputs(owner, first)
    root, state = prepared_only(owner, job, entries)
    state.update(source_unit_protocol=PROTOCOL, source_unit_cursor=0)
    manifest = freeze_source_units(root, state)
    state["source_unit_count"] = manifest["unit_count"]
    from team_miner.skill_miner.round_reuse import prepare_round_reuse

    prepare_round_reuse(job, root, state, entries)
    header = _read(root / "reuse/manifest.json")
    assert header["extract_count"] == header["candidate_count"] == 0
    assert "not interchangeable" in header["extract_reuse_reason"]
    context = next_source_unit_assignment(root, cursor=0, count=manifest["unit_count"],
                                          unit_index=0, language_policy=POLICY)
    assert not try_reuse_source_unit_extract(job, root, state, context)
    assert not (root / "units/0").exists()


def make_scope(root, manifest, *, method_index=3, context_index=0):
    rows = load_source_units(root, [method_index, context_index])
    method, context = next(row for row in rows if row["unit_index"] == method_index), next(
        row for row in rows if row["unit_index"] == context_index
    )
    precise = {"protocol": PROTOCOL, "unit_ids": [row["id"] for row in rows],
               "operative_ids": [method["id"]], "context_ids": [context["id"]],
               "method_groups": [{"item_id": "method-1", "task": "完成核实", "boundary": "只按原始政策",
                                  "unit_ids": [method["id"]]}],
               "source_sha256": _digest([{key: row[key] for key in
                                          ("id", "source_uri", "start_char", "end_char", "sha256", "source_sha256")}
                                         for row in rows])}
    precise["sha256"] = _digest(precise)
    return {"source_units": precise, "ordinals": [0], "method_ordinals": [0]}


def profile_fixture(tmp_path):
    root = tmp_path / "round_inputs/round-1/staged"
    path = root.parent / "original.txt"
    path.parent.mkdir(parents=True)
    text = "# 政策\n\n| 条件 | 行动 |\n| --- | --- |\n| A | 先核实 |\n| B | 停止 |\n"
    path.write_text(text, encoding="utf-8")
    prefix = "viking://resources/runs/same-job/rounds/round-1/source"
    chunks = planning.source_chunks(text, prefix + "/original/doc.md", max_chars=12000)
    for index, chunk in enumerate(chunks):
        _save(root / "chunks" / f"{index:08d}.json", {**chunk, "ordinal": index})
    manifest = freeze_source_units(root, {"source_unit_protocol": PROTOCOL, "chunk_count": len(chunks)})
    sources = _sources([{"kind": "source", "relative_path": "original/doc.md", "local_path": str(path),
                         "sha256": _sha(text)}], root.parent)
    return root, prefix, sources, manifest


@pytest.mark.parametrize("change", ["row", "role", "association", "source"])
def test_exact_fingerprint_separates_rows_roles_method_associations_and_full_source(tmp_path, change):
    root, prefix, sources, manifest = profile_fixture(tmp_path)
    scope = make_scope(root, manifest)
    before = _profile(root, scope, prefix, sources)
    precise = scope["source_units"]
    if change == "row":
        scope = make_scope(root, manifest, method_index=4)
    elif change == "role":
        precise["operative_ids"], precise["context_ids"] = precise["context_ids"], precise["operative_ids"]
        precise["method_groups"][0]["unit_ids"] = precise["operative_ids"]
    elif change == "association":
        precise["method_groups"][0]["boundary"] = "不同独立终态"
    else:
        changed = copy.deepcopy(sources)
        changed["original/doc.md"]["sha256"] = "0" * 64
        with pytest.raises(ValueError, match="complete frozen source SHA"):
            _profile(root, scope, prefix, changed)
        return
    precise = scope["source_units"]
    precise["sha256"] = _digest({key: value for key, value in precise.items() if key != "sha256"})
    assert _profile(root, scope, prefix, sources) != before


def test_exact_scope_tampering_rejects_instead_of_falling_back_to_old_chunk_profile(tmp_path):
    root, prefix, sources, manifest = profile_fixture(tmp_path)
    scope = make_scope(root, manifest)
    scope["source_units"]["operative_ids"] = []
    with pytest.raises(ValueError, match="scope changed"):
        _profile(root, scope, prefix, sources)
