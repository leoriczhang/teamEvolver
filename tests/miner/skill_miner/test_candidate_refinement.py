"""Explicit later-round revisions correct business fields without replacing routing identity."""

from __future__ import annotations

import asyncio
import copy
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import candidate_identity_orchestration_helpers as identities
from team_miner.skill_miner import candidate_refinement as refinement
from team_miner.skill_miner.candidate_identity import candidate_id
from team_miner.skill_miner.ov_staged_orchestration import _json, _read, _save, _sha
from tests.miner.skill_miner.support.candidate_identity import candidate


def assignment(tmp_path):
    old = candidate("request-review", task="核验请求并返回处理决定")
    identity = candidate_id("job", old["name"])
    authority = {**copy.deepcopy(old), "candidate_id": identity, "refinement_allowed": True,
                 "refinement_item_ids": ["method-1"], "base_metadata_sha256": refinement.metadata_sha256(old)}
    proposed = {key: copy.deepcopy(old[key]) for key in refinement.BUSINESS_FIELDS}
    proposed.update(inputs=["请求", "请求类型"], boundary="按照原文分别处理普通和特殊请求",
                    revise_candidate_id=identity, base_metadata_sha256=authority["base_metadata_sha256"],
                    reason="修正上一轮遗漏的分支输入", derived_from=["method-1"])
    item = {"item_id": "method-1", "kind": "method", "task": old["task"],
            "boundary": proposed["boundary"], "source_refs": ["alias-1"]}
    record = {"item": item, "ordinals": [0], "leaf_items": ["leaf-1"],
              "bindings": {"alias-1": [0]}, "unit_id": "unit-1"}
    value = {"schema_version": planning.SELECTION_SCHEMA_V3, "input_ids": ["unit-1"],
             "skills": [proposed], "knowledge": [], "deferred": [], "resolved": []}
    packet = {"derived_controls": {"known_candidates": [authority], "items": [item], "input_ids": ["unit-1"],
                                  "selection_schema": planning.SELECTION_SCHEMA_V3}}
    request = {"from": ["viking://frozen/packet"], "instruction": "据原文复核相同工作流"}
    child = "plan-" + "a" * 24
    _save(tmp_path / "children" / child / "packet.json", packet)
    _save(tmp_path / "children" / child / "request.json", request)
    _save(tmp_path / "children" / child / "result.txt", value)
    _save(tmp_path / "chunk-metadata/00000000.json", {"source_uri": "viking://current/original/input.md"})
    current = {"child": child, "packet_file": f"children/{child}/packet.json",
               "request_file": f"children/{child}/request.json", "packet_sha256": _sha(_json(packet)),
               "request_sha256": _sha(_json(request))}
    return {"job_id": "job", "current_round": 2}, current, value, [record], [authority]


def checked(value, records, known):
    return planning.validate_selection(value, items=[row["item"] for row in records], input_ids=["unit-1"],
                                       known_candidates={row["candidate_id"]: row for row in known},
                                       refinement_enabled=True)


def test_explicit_revision_preserves_name_and_corrects_business_fields(tmp_path):
    job, current, value, records, known = assignment(tmp_path)
    result = checked(value, records, known)
    assert result["skills"][0]["name"] == "request-review"
    assert result["skills"][0]["inputs"] == ["请求", "请求类型"]
    journal = refinement.prepare_refinements(tmp_path, job, current, value, result, records, known)
    refinement.commit_refinements(tmp_path, journal)
    loaded = refinement.latest_refinement(tmp_path, known[0]["candidate_id"])
    assert loaded["after"]["boundary"] == value["skills"][0]["boundary"]
    assert loaded["before"]["inputs"] == ["请求"]
    assert loaded["leaf_items"] == ["leaf-1"]
    # Real request files have a final newline while the controller hash is canonical JSON.
    assert journal["request_sha256"] != current["request_sha256"]
    original = copy.deepcopy(journal)
    assert refinement.prepare_refinements(tmp_path, job, current, value, result, records, known) == original
    refinement.commit_refinements(tmp_path, original)
    assert refinement.latest_refinement(tmp_path, known[0]["candidate_id"]) == loaded


@pytest.mark.parametrize("failure", ["unknown", "hash", "scope", "rename", "missing", "unsupported"])
def test_refinement_rejects_unassigned_workflow_hash_scope_and_implicit_renaming(tmp_path, failure):
    _, _, value, records, known = assignment(tmp_path)
    row = value["skills"][0]
    if failure == "unknown":
        row["revise_candidate_id"] = "candidate-" + "b" * 24
    elif failure == "hash":
        row["base_metadata_sha256"] = "0" * 64
    elif failure == "scope":
        records[0]["item"]["item_id"] = "another-workflow"
        row["derived_from"] = ["another-workflow"]
    elif failure == "rename":
        row["name"] = "unrelated-workflow"
    elif failure == "missing":
        del row["trigger"]
    else:
        known[0]["refinement_allowed"] = False
    with pytest.raises(ValueError):
        checked(value, records, known)


def test_r1_v2_refinement_stays_forbidden_and_v3_reuse_keeps_old_business_fields(tmp_path):
    _, _, value, records, known = assignment(tmp_path)
    value["schema_version"] = planning.SELECTION_SCHEMA_V2
    with pytest.raises(ValueError, match="later-round protocol"):
        planning.validate_selection(value, items=[records[0]["item"]], input_ids=["unit-1"],
                                    known_candidates={known[0]["candidate_id"]: known[0]})
    value["schema_version"] = planning.SELECTION_SCHEMA_V3
    value["skills"] = [{"reuse_candidate_id": known[0]["candidate_id"],
                        "reason": "元数据无需修订", "derived_from": ["method-1"]}]
    result = checked(value, records, known)
    assert result["skills"][0]["inputs"] == ["请求"]
    assert result["skills"][0]["boundary"] == known[0]["boundary"]


@pytest.mark.parametrize("file", ["packet.json", "request.json", "result.txt", "journal", "symlink"])
def test_refinement_restart_rejects_frozen_request_response_and_audit_tampering(tmp_path, file):
    job, current, value, records, known = assignment(tmp_path)
    journal = refinement.prepare_refinements(
        tmp_path, job, current, value, checked(value, records, known), records, known,
    )
    refinement.commit_refinements(tmp_path, journal)
    path = tmp_path / "children" / current["child"] / file
    if file in {"journal", "symlink"}:
        path = tmp_path / "refinements/journals" / (current["child"] + ".json")
    if file == "symlink":
        saved = path.read_text()
        path.unlink()
        external = tmp_path.parent / (tmp_path.name + "-external.json")
        external.write_text(saved)
        path.symlink_to(external)
    else:
        path.write_text(path.read_text() + " ")
    with pytest.raises(ValueError):
        refinement.latest_refinement(tmp_path, known[0]["candidate_id"])


def test_late_projection_failure_replays_same_frozen_child_after_restart(tmp_path, monkeypatch):
    job, current, value, records, known = assignment(tmp_path)
    journal = refinement.prepare_refinements(
        tmp_path, job, current, value, checked(value, records, known), records, known,
    )
    from team_miner.skill_miner import ov_staged_orchestration as runner
    original = runner._save
    def fail_latest(path, content):
        if path.parent.name == "latest":
            raise OSError("projection failed")
        original(path, content)
    monkeypatch.setattr(runner, "_save", fail_latest)
    with pytest.raises(OSError):
        refinement.commit_refinements(tmp_path, journal)
    monkeypatch.setattr(runner, "_save", original)
    replay = refinement.prepare_refinements(
        tmp_path, job, current, value, checked(value, records, known), records, known,
    )
    refinement.commit_refinements(tmp_path, replay)
    assert refinement.latest_refinement(tmp_path, known[0]["candidate_id"])["after"]["inputs"] == ["请求", "请求类型"]


def test_another_child_cannot_reapply_stale_base_even_with_identical_new_metadata(tmp_path):
    job, current, value, records, known = assignment(tmp_path)
    result = checked(value, records, known)
    refinement.commit_refinements(
        tmp_path, refinement.prepare_refinements(tmp_path, job, current, value, result, records, known),
    )
    current["child"] = "plan-" + "b" * 24
    with pytest.raises(ValueError, match="already accepted business version"):
        refinement.prepare_refinements(tmp_path, job, current, value, result, records, known)


def test_forged_business_audit_cannot_override_immutable_model_response(tmp_path):
    job, current, value, records, known = assignment(tmp_path)
    journal = refinement.prepare_refinements(
        tmp_path, job, current, value, checked(value, records, known), records, known,
    )
    refinement.commit_refinements(tmp_path, journal)
    path = tmp_path / "refinements/journals" / (current["child"] + ".json")
    journal["changes"][0]["after"]["boundary"] = "伪造另一业务规则"
    journal["changes"][0]["metadata_sha256"] = refinement.metadata_sha256(journal["changes"][0]["after"])
    _save(path, journal)
    _save(tmp_path / "refinements/latest" / (known[0]["candidate_id"] + ".json"), {
        "child": current["child"], "index": 0, "journal_sha256": _sha(path.read_text()),
    })
    with pytest.raises(ValueError, match="original response"):
        refinement.latest_refinement(tmp_path, known[0]["candidate_id"])


def test_same_file_other_method_and_changed_original_hash_are_not_revision_scope(tmp_path):
    from team_miner.skill_miner.ov_compile_mining import RUNS_ROOT
    job = {"job_id": "job", "current_round": 2, "source_snapshot": f"{RUNS_ROOT}/job/rounds/round-2/source"}
    root = tmp_path / "round_inputs/round-2/staged"
    old = tmp_path / "round_inputs/round-1/staged"
    previous_uri = f"{RUNS_ROOT}/job/rounds/round-1/source"
    relative = "original/input.md"
    _save(tmp_path / "rounds/round-1/input.json", {"source_snapshot": previous_uri})
    _save(old / "scopes/request-review.json", {"ordinals": [0, 1], "method_ordinals": [0]})
    _save(old / "chunk-metadata/00000000.json", {
        "source_uri": previous_uri + "/" + relative, "start_char": 0, "end_char": 50, "source_sha256": "original-hash",
    })
    current_uri = job["source_snapshot"] + "/" + relative
    _save(root / "source-files" / (_sha(current_uri) + ".json"), {"uri": current_uri, "ordinals": [3, 4, 5]})
    for ordinal, start in [(3, 0), (4, 50), (5, 100)]:
        _save(root / "chunk-metadata" / f"{ordinal:08d}.json", {
            "source_uri": current_uri, "start_char": start, "end_char": start + 50, "source_sha256": "original-hash",
        })
    assert refinement.mapped_method_ordinals(job, root, "request-review", [relative]) == [3]
    changed = _read(root / "chunk-metadata/00000003.json")
    changed["source_sha256"] = "different-original-hash"
    _save(root / "chunk-metadata/00000003.json", changed)
    assert refinement.mapped_method_ordinals(job, root, "request-review", [relative]) == []


def typed_assignment(tmp_path):
    job, current, value, records, known = assignment(tmp_path)
    current["context"] = {"typed_planning": "selection", "record_indexes": [0], "input_ids": ["unit-1"], "after": 1}
    _save(tmp_path / "proposals/00000000.json", records[0])
    uri = _read(tmp_path / "chunk-metadata/00000000.json")["source_uri"]
    _save(tmp_path / "source-files" / (_sha(uri) + ".json"), {"uri": uri, "ordinals": [0]})
    state = {"candidate_refinement_protocol": refinement.REFINEMENT_PROTOCOL, "selection_count": 0}
    return job, current, value, records, known, state


def test_real_typed_accept_replays_after_journal_commit_then_plan_write_failure(tmp_path, monkeypatch):
    from team_miner.skill_miner import ov_staged_part2 as runner
    from tests.miner.skill_miner.support.ov_staged_orchestration import Owner
    job, current, value, _, _, state = typed_assignment(tmp_path)
    owner = Owner(tmp_path / "owner")
    original = runner._save
    def fail_plan(path, content):
        if path == tmp_path / "plan.json":
            raise OSError("late plan write")
        original(path, content)
    monkeypatch.setattr(runner, "_save", fail_plan)
    with pytest.raises(OSError, match="late plan write"):
        asyncio.run(owner._staged_accept_typed(job, tmp_path, state, current, value))
    assert state["selection_count"] == 0
    monkeypatch.setattr(runner, "_save", original)
    asyncio.run(owner._staged_accept_typed(job, tmp_path, state, current, value))
    assert _read(tmp_path / "plan.json")["skills"][0]["inputs"] == ["请求", "请求类型"]
    assert state["selection_count"] == 1


def test_valid_revision_and_another_implicit_name_conflict_cannot_commit_partial_audit(tmp_path):
    from tests.miner.skill_miner.support.ov_staged_orchestration import Owner
    job, current, value, records, known, state = typed_assignment(tmp_path)
    conflicting = candidate("other-workflow", task="原来独立方法")
    _save(tmp_path / "plan.json", {"schema_version": planning.PLAN_SCHEMA, "skills": [conflicting], "deferred": []})
    item = {"item_id": "method-2", "kind": "method", "task": "另一个不相关工作流",
            "boundary": "不同范围", "source_refs": ["alias-2"]}
    record = {"item": item, "ordinals": [1], "leaf_items": ["leaf-2"],
              "bindings": {"alias-2": [1]}, "unit_id": "unit-1"}
    records.append(record)
    _save(tmp_path / "proposals/00000001.json", record)
    current["context"]["record_indexes"] = [0, 1]
    current["context"]["after"] = 2
    output = {key: copy.deepcopy(conflicting[key]) for key in ("name", *refinement.BUSINESS_FIELDS)}
    output.update(task="改成另一个不相关工作流", derived_from=["method-2"])
    value["skills"].append(output)
    _save(tmp_path / "children" / current["child"] / "result.txt", value)
    packet = _read(tmp_path / current["packet_file"])
    packet["derived_controls"]["items"].append(item)
    _save(tmp_path / current["packet_file"], packet)
    current["packet_sha256"] = _sha(_json(packet))
    with pytest.raises(ValueError, match="已用于不同方法"):
        asyncio.run(Owner(tmp_path / "owner")._staged_accept_typed(job, tmp_path, state, current, value))
    assert not (tmp_path / "refinements/journals").exists()
    assert _read(tmp_path / "plan.json")["skills"] == [conflicting]


def test_large_readonly_notes_cannot_crowd_out_source_scoped_revision_authority(tmp_path, monkeypatch):
    from team_miner.skill_miner.candidate_identity import IdentityCatalog
    from team_miner.skill_miner.compile_strategy import render_refinement_plan_compiler
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
    from team_miner.skill_miner.ov_staged_orchestration import _package
    job, _, _, records, known = assignment(tmp_path)
    old = candidate("request-review", task=records[0]["item"]["task"])
    old["inputs"] = [str(index) + "入" * 230 for index in range(16)]
    old["outputs"] = [str(index) + "出" * 230 for index in range(16)]
    catalog = IdentityCatalog(tmp_path / "selection-identities/catalog.sqlite3")
    catalog.put({"candidate_id": known[0]["candidate_id"], "candidate": old, "prior": True, "selected": False,
                 "ordinal": 0, "scope": {"refinement_ordinals": [0]}})
    catalog.close()
    monkeypatch.setattr(identities, "_seed_catalog", lambda *args, **kwargs: None)
    controls = {"items": [records[0]["item"]], "input_ids": ["unit-1"],
                "related_facts": [{"text": "只读摘要" * 6000}], "provisional_gaps": []}
    identities._selection_known(job, tmp_path, records, controls, refinement_enabled=True)
    assert controls["known_candidates"][0]["refinement_allowed"]
    assert controls["context_omitted"] and not controls["related_facts"]
    packet = planning.build_stage_packet("plan", "selection.json", controls=controls,
                                         language_policy={"language": "zh-CN"})
    _, published = _package(render_refinement_plan_compiler(SKILL_BODY))
    assert len(_json(packet)) <= 32000
    assert len(_json(packet)) + len(published) + 3072 <= 48000


@pytest.mark.parametrize("both_refined", [False, True])
def test_group_cannot_discard_another_identity_accepted_business_version(tmp_path, monkeypatch, both_refined):
    from tests.miner.skill_miner.support.candidate_identity import fixture, group_current, row
    rows = [row(0, prior=True), row(1, prior=True)]
    rows[1]["candidate"]["inputs"].append("已复核的分支参数")
    fixture(tmp_path, rows)
    current, value = group_current(rows)
    refined = {rows[1]["candidate_id"]}
    if both_refined:
        refined.add(rows[0]["candidate_id"])
    monkeypatch.setattr(identities, "latest_refinement", lambda root, identity: {"accepted": True}
                        if identity in refined else None)
    identities._accept_groups(tmp_path, current, value)
    assert _read(tmp_path / "plan.json")["skills"] == [row["candidate"] for row in rows]
    journal = _read(tmp_path / "identity/journals" / (current["child"] + ".json"))
    assert journal["requested_groups"] == value["groups"]
    assert len(journal["groups"]) == 2 and len(journal["host_splits"]) == 1
    assert _read(tmp_path / "identity/aliases.json") == {}
    audit = json.loads(next(identities._split_audit(tmp_path)))
    assert audit["requested_groups"] == value["groups"] and len(audit["effective_groups"]) == 2


def test_refined_survivor_still_merges_a_new_duplicate_without_reverting_business_fields(tmp_path, monkeypatch):
    from tests.miner.skill_miner.support.candidate_identity import fixture, group_current, row
    rows = [row(0, prior=True), row(1)]
    rows[0]["candidate"]["inputs"].append("已复核的分支参数")
    fixture(tmp_path, rows)
    current, value = group_current(rows)
    monkeypatch.setattr(identities, "latest_refinement", lambda root, identity: {"accepted": True}
                        if identity == rows[0]["candidate_id"] else None)
    identities._accept_groups(tmp_path, current, value)
    assert _read(tmp_path / "plan.json")["skills"][0]["inputs"] == rows[0]["candidate"]["inputs"]
    aliases = _read(tmp_path / "identity/aliases.json")
    assert aliases[rows[1]["candidate"]["name"]]["name"] == rows[0]["candidate"]["name"]


def test_equivalent_refined_member_can_merge_without_a_false_version_conflict(tmp_path, monkeypatch):
    from tests.miner.skill_miner.support.candidate_identity import fixture, group_current, row
    rows = [row(0, prior=True), row(1, prior=True)]
    rows[1]["candidate"].update({key: copy.deepcopy(rows[0]["candidate"][key]) for key in refinement.BUSINESS_FIELDS})
    fixture(tmp_path, rows)
    current, value = group_current(rows)
    monkeypatch.setattr(identities, "latest_refinement", lambda root, identity: {"accepted": True}
                        if identity == rows[1]["candidate_id"] else None)
    identities._accept_groups(tmp_path, current, value)
    assert len(_read(tmp_path / "plan.json")["skills"]) == 1
    journal = _read(tmp_path / "identity/journals" / (current["child"] + ".json"))
    assert journal["host_splits"] == [] and journal["groups"] == value["groups"]


def test_two_round_refinement_survives_catalog_restart_and_preserves_custom_case_ids(tmp_path):
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
    from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
    from tests.miner.skill_miner.support.ov_staged_orchestration import Owner, drive
    async def run():
        owner = Owner(tmp_path)
        job, entries = owner.new()
        job_dir = owner.repo._job_dir(job["job_id"])
        manifest = job_dir / "round_inputs/round-1/manifest.json"
        _save(manifest, entries)
        job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
        language = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, language, ""))
        dataset = owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json"
        document = _read(dataset)
        custom_ids = [f"stable:{index}" for index in range(12)]
        for case, identity in zip(document["cases"], custom_ids, strict=True):
            case["case_id"] = identity
        _save(dataset, document)
        job = owner.repo.prepare_next_round(job["job_id"], job["pending_checkpoint"]["id"], {}, [], {})
        entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, job, staged=True)
        job = owner.repo.update_job(job["job_id"], input_manifest=str(job_dir / "round_inputs/round-2/manifest.json"))
        owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
        request = owner.ov.request
        async def refine_request(account, method, path, **kwargs):
            result = await request(account, method, path, **kwargs)
            if method == "POST" and path == "/api/v1/compile":
                req = kwargs["json"]
                packet = json.loads(owner.ov.contents[req["from"][0] + "/packet.json"])
                if packet["stage"] == "plan":
                    controls = packet["derived_controls"]
                    assert controls["selection_schema"] == planning.SELECTION_SCHEMA_V3
                    old = next(row for row in controls["known_candidates"] if row["name"] == "refund-review")
                    assert old["refinement_allowed"]
                    output_uri = req["to"] + "/" + packet["output_path"]
                    output = json.loads(owner.ov.contents[output_uri])
                    proposed = {key: copy.deepcopy(old[key]) for key in refinement.BUSINESS_FIELDS}
                    proposed.update(inputs=old["inputs"] + ["请求类型"], boundary="按原始条件区分请求类型并复核",
                                    revise_candidate_id=old["candidate_id"],
                                    base_metadata_sha256=old["base_metadata_sha256"],
                                    reason="复核原文后修正遗漏输入和处理范围",
                                    derived_from=output["skills"][0]["derived_from"])
                    output["skills"] = [proposed]
                    owner.ov.contents[output_uri] = json.dumps(output, ensure_ascii=False)
            return result
        owner.ov.request = refine_request
        job = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, language, ""))
        assert job["status"] == "waiting_human", job.get("error")
        root, _ = await owner._staged_load(job)
        updated = _read(root / "plan.json")["skills"][0]
        assert updated["name"] == "refund-review" and "请求类型" in updated["inputs"]
        assert [case["case_id"] for case in _read(dataset)["cases"]] == custom_ids
        # Fresh catalogs must adopt the accepted business version, never restore the old prior metadata.
        identities._seed_catalog(job, root, "restarted-identities", include_reviews=False)
        from team_miner.skill_miner.candidate_identity import IdentityCatalog
        catalog = IdentityCatalog(root / "restarted-identities/catalog.sqlite3")
        assert catalog.get(candidate_id(job["job_id"], "refund-review"))["candidate"]["inputs"] == updated["inputs"]
        catalog.close()
    asyncio.run(run())
