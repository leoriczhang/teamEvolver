"""Fresh partition review uses bounded public Compile calls, without a source-overlap graph."""
from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from team_miner import candidate_identity_orchestration as orchestration
from team_miner.candidate_identity import (
    IDENTITY_GROUP_SCHEMA_V2,
    IDENTITY_PARTITION_PROTOCOL,
    IdentityCatalog,
    render_identity_partition_compiler,
)
from team_miner.compile_strategy import mining_policy_block
from team_miner.ov_compile_mining import SKILL_BODY
from team_miner.ov_staged_orchestration import _package, _read, _save
from team_miner.source_scope import SOURCE_SCOPE_PROTOCOL_V3
from team_miner.tests.test_candidate_identity import fixture, row
from team_miner.tests.test_extract_batches import ExtractFake, ExtractOwner


def state():
    return {
        "execution_protocol": "te-mining-optimized.v1",
        "identity_partition_protocol": IDENTITY_PARTITION_PROTOCOL,
        "source_scope_protocol": SOURCE_SCOPE_PROTOCOL_V3,
        "language_policy": {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"},
        "identity_partition_packet_budget": 32000,
        "identity_pass": 0, "identity_cursor": 0,
    }


def output(context, *, merge=False):
    ids = context["identity_ids"]
    return {
        "schema_version": IDENTITY_GROUP_SCHEMA_V2, "candidate_ids": list(ids),
        "groups": ([{"candidate_ids": list(ids), "survivor_candidate_id": ids[-1], "reason": "完整相同流程"}]
                   if merge else [{"candidate_ids": [identity], "survivor_candidate_id": identity,
                                   "reason": "不同独立终态"} for identity in ids]),
    }


def run_windows(root, value, *, merge=False):
    windows = []
    while (window := orchestration._identity_window(root, value)) is not None:
        rows, context = window
        current = {"child": f"partition-{len(windows)}", "context": context}
        orchestration._accept_groups(root, current, output(context, merge=merge))
        value["identity_partition_cursor"] = context["after"]
        windows.append((rows, context))
    return windows


def test_29_same_source_identities_are_reviewed_once_as_complete_collection(tmp_path):
    rows = [row(index) for index in range(29)]
    fixture(tmp_path, rows)
    value = state()
    windows = run_windows(tmp_path, value)
    assert len(windows) == 1
    assert len(windows[0][0]) == 29
    assert len(_read(tmp_path / "plan.json")["skills"]) == 29
    assert value["identity_partition_level"] == 2
    assert len(list((tmp_path / "identity/coverage").glob("*.json"))) == 29
    assert not (tmp_path / "identity/partitions/level-1/ready.json").exists()


def test_120_same_source_identities_have_at_most_two_nonoverlapping_layers(tmp_path):
    rows = [row(index) for index in range(120)]
    fixture(tmp_path, rows)
    value = state()
    windows = run_windows(tmp_path, value)
    assert len(windows) == 8
    assigned = {}
    for selected, context in windows:
        assert len(selected) <= 32
        assigned.setdefault(context["identity_partition_level"], []).extend(context["identity_ids"])
    assert set(assigned) == {0, 1}
    for ids in assigned.values():
        assert len(ids) == 120 and len(set(ids)) == 120
    assert len(_read(tmp_path / "plan.json")["skills"]) == 120


def test_cross_batch_merge_preserves_every_original_scope_and_existing_metadata(tmp_path):
    rows = [row(index, task="同一完整退款复核流程") for index in range(120)]
    original = copy.deepcopy(rows[-1]["candidate"])
    fixture(tmp_path, rows)
    value = state()
    windows = run_windows(tmp_path, value, merge=True)
    assert len(windows) == 5
    plan = _read(tmp_path / "plan.json")
    assert len(plan["skills"]) == 1
    assert plan["skills"][0] == original
    scope = _read(tmp_path / "scopes" / (original["name"] + ".json"))
    assert scope["ordinals"] == list(range(120))
    assert scope["method_ordinals"] == list(range(120))
    assert len(_read(tmp_path / "identity/aliases.json")) == 119


@pytest.mark.parametrize("change", ["unknown", "missing", "duplicate", "nominee", "metadata"])
def test_partition_output_rejects_unknown_missing_duplicate_and_business_changes(tmp_path, change):
    fixture(tmp_path, [row(index) for index in range(29)])
    _, context = orchestration._identity_window(tmp_path, state())
    result = output(context)
    if change == "unknown":
        result["groups"][0]["candidate_ids"] = ["candidate-unknown"]
    elif change == "missing":
        result["groups"].pop()
    elif change == "duplicate":
        result["groups"].append(copy.deepcopy(result["groups"][0]))
    elif change == "nominee":
        result["groups"][0]["survivor_candidate_id"] = result["candidate_ids"][1]
    else:
        result["groups"][0]["task"] = "改写业务"
    with pytest.raises(ValueError):
        orchestration._accept_groups(tmp_path, {"child": "bad", "context": context}, result)
    assert not (tmp_path / "identity/journals/bad.json").exists()
    assert len(_read(tmp_path / "plan.json")["skills"]) == 29


@pytest.mark.parametrize("tamper", ["manifest", "version"])
def test_partition_freezes_assignment_and_version_before_model_submission(tmp_path, tamper):
    rows = [row(index) for index in range(29)]
    fixture(tmp_path, rows)
    _, context = orchestration._identity_window(tmp_path, state())
    if tamper == "manifest":
        p = tmp_path / context["identity_partition_file"]
        value = _read(p)
        value["controls"]["identities"][0]["task"] = "篡改"
        _save(p, value)
    else:
        catalog = IdentityCatalog(tmp_path / "identity/catalog.sqlite3")
        changed = copy.deepcopy(rows[0])
        changed["candidate"]["boundary"] = "边界已变化"
        catalog.put(changed)
        catalog.close()
    with pytest.raises(ValueError, match="changed|versions"):
        orchestration._accept_groups(tmp_path, {"child": "bad", "context": context}, output(context))
    assert not (tmp_path / "identity/journals/bad.json").exists()


def test_partition_late_write_replays_once_after_scope_version_changes(tmp_path, monkeypatch):
    rows = [row(index, task="同一完整流程") for index in range(29)]
    fixture(tmp_path, rows)
    value = state()
    _, context = orchestration._identity_window(tmp_path, value)
    current, result = {"child": "late", "context": context}, output(context, merge=True)
    original_io = orchestration._io
    original_save = original_io()[2]

    def broken_save(path, data):
        if path == tmp_path / "plan.json":
            raise OSError("late commit failure")
        return original_save(path, data)

    monkeypatch.setattr(orchestration, "_io", lambda: (*original_io()[:2], broken_save, *original_io()[3:]))
    with pytest.raises(OSError):
        orchestration._accept_groups(tmp_path, current, result)
    monkeypatch.setattr(orchestration, "_io", original_io)
    orchestration._accept_groups(tmp_path, current, result)
    orchestration._accept_groups(tmp_path, current, result)
    value["identity_partition_cursor"] = context["after"]
    assert orchestration._identity_window(tmp_path, value) is None
    assert len(_read(tmp_path / "plan.json")["skills"]) == 1
    assert len(_read(tmp_path / "identity/aliases.json")) == 28


def test_packet_budget_preserves_complete_maximal_business_fields(tmp_path):
    rows = [row(index) for index in range(12)]
    for item in rows:
        item["candidate"]["inputs"] = ["入" * 240] * 16
        item["candidate"]["outputs"] = ["出" * 240] * 16
    fixture(tmp_path, rows)
    value = state()
    value["identity_partition_packet_budget"] = 22000
    windows = run_windows(tmp_path, value)
    assert len(windows) == 12
    for assigned, context in windows:
        frozen = _read(tmp_path / context["identity_partition_file"])
        assert len(json.dumps(frozen["controls"], ensure_ascii=False, separators=(",", ":"))) < 22000
        for item in frozen["controls"]["identities"]:
            assert item["inputs"] == ["入" * 240] * 16
            assert item["outputs"] == ["出" * 240] * 16
        assert len(assigned) == 2


def test_renderer_preserves_explicit_common_and_identity_policy_once():
    common = "Custom policy: do not replace this literal identity_groups field."
    identity = "Keep the literal singleton procedure label."
    base = "---\nname: custom\ndescription: explicit policy\n---\n" + mining_policy_block(
        common, {"identity_group": identity}
    )
    source = render_identity_partition_compiler(base)
    contract = json.loads(source.rsplit("```json\n", 1)[1].split("\n```", 1)[0])
    instructions = contract["synthesize"]["instructions"]
    assert instructions.count(common) == instructions.count(identity) == 1
    assert contract["synthesize"]["execution"] == "direct"
    assert contract["synthesize"]["input_unit"] == "file"
    assert "p.map(sources, task=contract.synthesize)" in source
    assert "p.finalize(files, into=target)" in source
    assert "p.reduce" not in source
    assert IDENTITY_GROUP_SCHEMA_V2 in instructions
    assert "narrower Candidate into a supplied broader phase" in source
    assert "most complete existing" in source and "workflow as survivor" in source
    assert "EVERY other assigned identity" in source
    assert "generic claim such as" in source


def test_legacy_partition_renderer_keeps_its_frozen_rule_while_v2_is_explicit():
    from team_miner.candidate_identity import (
        LEGACY_IDENTITY_PARTITION_PROTOCOL,
        render_identity_partition_compiler,
    )

    base = "---\nname: custom\ndescription: explicit policy\n---\n" + mining_policy_block()
    legacy = render_identity_partition_compiler(base, protocol=LEGACY_IDENTITY_PARTITION_PROTOCOL)
    current = render_identity_partition_compiler(base, protocol=IDENTITY_PARTITION_PROTOCOL)
    assert "ONE bounded partition" in legacy
    assert "EVERY other assigned identity" not in legacy
    assert "EVERY other assigned identity" in current
    assert LEGACY_IDENTITY_PARTITION_PROTOCOL == "te-candidate-identity-partition.v1"
    assert IDENTITY_PARTITION_PROTOCOL == "te-candidate-identity-partition.v2"


class PartitionCompile(ExtractFake):
    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        if method == "POST" and path == "/api/v1/compile":
            body = kwargs["json"]
            packet_uri = body["from"][0] + "/packet.json"
            if packet_uri not in self.contents:
                return result
            packet = json.loads(self.contents[packet_uri])
            if packet["stage"] == "identity_group":
                value = output({"identity_ids": packet["derived_controls"]["required_candidate_ids"]})
                self.contents[body["to"] + "/" + packet["output_path"]] = json.dumps(value, ensure_ascii=False)
        return result


class PartitionOwner(ExtractOwner):
    def __init__(self, root):
        super().__init__(root)
        self.fixture.ov = self.ov = PartitionCompile()

    async def _staged_candidate_next(self, job, root, value):
        # Stop this integration at the owned identity boundary; writers have
        # independent transport coverage and no model call is faked as proof.
        assert value["identity_review_complete"]
        return job


@pytest.mark.asyncio
@pytest.mark.parametrize("count,max_calls", [(29, 1), (120, 8)])
async def test_actual_public_compile_transport_is_near_linear_for_same_source(tmp_path, count, max_calls):
    owner = PartitionOwner(tmp_path)
    job, entries = owner.new(1)
    job = owner.repo.update_job(
        job["job_id"], staged_workflow_protocol="te-direct-workflow-planning.v1",
        staged_source_scope_protocol=SOURCE_SCOPE_PROTOCOL_V3,
        staged_planning_alias_protocol="te-planning-aliases.v1",
        staged_compact_planning_protocol="te-compact-planning.v1",
        staged_identity_partition_protocol=IDENTITY_PARTITION_PROTOCOL,
    )
    await owner._prepare_staged_round(job, SKILL_BODY, entries, state()["language_policy"], "")
    job = owner.repo.get_job(job["job_id"])
    root = Path(job["staged_state_path"]).parent
    value = _read(root / "state.json")
    assert value["identity_partition_protocol"] == IDENTITY_PARTITION_PROTOCOL
    await owner._release_staged_compile_slot(job, value["current"]["task_id"])
    rows = [row(index) for index in range(count)]
    for item in rows:
        item["scope"] = {"ordinals": [0], "method_ordinals": [0]}
    fixture(root, rows)
    value.update(state(), phase="identity_group", current=None, candidate_names=[],
                 writer_resume={"phase": "skill_section", "candidate_names": [], "candidate_index": 0,
                                "cursor": 0, "section_count": 0, "case_index": 0})
    value.pop("identity_partition_packet_budget", None)
    _save(root / "state.json", value)
    job = await owner._staged_identity_next(job, root, value)
    for _ in range(max_calls + 1):
        value = _read(root / "state.json")
        if value.get("identity_review_complete"):
            break
        current = value["current"]
        assert current["stage"] == "identity_group"
        packet = _read(root / current["packet_file"])
        assert len(packet["derived_controls"]["identities"]) <= 32
        request = _read(root / current["request_file"])
        published = (root / current["compiler_file"]).read_text()
        assert request["from"] == [current["input_uri"]]
        assert len(published) + len(json.dumps(packet, ensure_ascii=False, separators=(",", ":"))) + len(
            request["instruction"]
        ) < 48000
        job = await owner._advance_staged_round(job, owner.ov.tasks[current["task_id"]])
    assert _read(root / "state.json")["identity_review_complete"]
    requests = [request for _, request in owner.ov.calls]
    packets = [json.loads(text) for request in requests for uri, text in owner.ov.contents.items()
               if uri.startswith(request["from"][0] + "/")]
    identity_packets = [packet for packet in packets if packet["stage"] == "identity_group"]
    assert len(identity_packets) == max_calls
    assert len(_read(root / "plan.json")["skills"]) == count
    summary = _read(root / "identity/summary.json")
    assert summary["partition_protocol"] == IDENTITY_PARTITION_PROTOCOL
    assert summary["max_identities_per_call"] == 32
    assert "source-overlap graph" in summary["retrieval"]
    assert _package(render_identity_partition_compiler(SKILL_BODY))[1] == (root / "children" / next(
        p.name for p in (root / "children").iterdir() if p.name.startswith("identity_group-")
    ) / "compiler.md").read_text()
