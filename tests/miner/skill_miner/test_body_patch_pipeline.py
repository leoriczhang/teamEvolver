"""Real body/Case host orchestration with inert Native output and frozen exact-unit fixtures.

The fixture supplies source-unit associations at the body boundary while the new
extractor has its own tests. It does not infer business correctness from fake output.
"""

from __future__ import annotations

import copy
import hashlib
import json
from pathlib import Path

import pytest

from team_miner.skill_miner import skill_body_patch as patch_contract
from team_miner.skill_miner.ov_artifact_batch_orchestration import _ledger
from team_miner.skill_miner.ov_body_patch_orchestration import body_state
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from team_miner.skill_miner.source_unit_scope import PROTOCOL as UNIT_PROTOCOL
from team_miner.skill_miner.source_unit_scope import _digest, route_exact_source_candidates
from team_miner.skill_miner.source_units import freeze_source_units, load_source_unit
from tests.miner.skill_miner.support.artifact_batches import ArtifactFake, ArtifactOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive, start

RAW = """| 阶段 | 原文 |
| --- | --- |
| 交流 | 首次说明约20分钟；另加40分钟之前必须征得客户同意。 |
| 前置 | 开始交流之前先表明身份并征得对方同意。 |
| 生命周期 | 退休后的费用不会立刻下降，而是缓慢下降。 |
| 可选 | 过去工作的高光经历可作为示例，不是必讲项。 |
| 结束 | 最后确认下次交流安排。 |
| 未决 | 两处账户称谓未映射，保持待核实。 |
"""


class PatchFake(ArtifactFake):
    def __init__(self):
        super().__init__()
        self.candidate_count = 1
        self.body_packets = []
        self.capacity_names = set()
        self.capacity_single = False
        self.bad_names = set()
        self.context_atoms = False

    def output(self, packet):
        controls = packet["derived_controls"]["body_patch"]
        name = packet["derived_controls"]["skill_name"]
        base = {"schema_version": patch_contract.OUTPUT_SCHEMA,
                "assignment_sha256": controls["assignment_sha256"],
                "base_state_sha256": controls["base_state_sha256"], "input_ids": controls["input_ids"]}
        if name in self.capacity_names:
            self.capacity_names.remove(name)
            return {**base, "schema_version": patch_contract.CAPACITY_SCHEMA,
                    "reason": "本批完整步骤超过输出容量，请缩小本次原文范围。"}
        if self.capacity_single:
            return {**base, "schema_version": patch_contract.CAPACITY_SCHEMA,
                    "reason": "单个单元仍无法容纳完整步骤。"}
        result = {**base, "title": controls["title"] or "完整客户沟通方法", "add": [], "attach": [],
                  "step_order": list(controls["step_order"]), "no_change_reason": "本批无新增适用步骤。"}
        evidence = {row["id"]: row["content"] for row in packet["evidence"]}
        operative = controls["operative_ids"]
        old = controls["atom_directory"]
        counter = int(controls["next_atom_id"][1:])

        def atom(kind, text, source, *, parent=None):
            nonlocal counter
            value = {"id": f"a{counter:04d}", "kind": kind, "text": text, "support_ids": [source]}
            counter += 1
            if parent is not None:
                value["parent_id"] = parent
            return value

        primary = next((row["id"] for row in old if row["kind"] == "step" and "说明交流安排" in row["text"]), None)
        # All add IDs precede attach IDs, as required by the real output contract.
        attachments = []
        if self.context_atoms and not operative:
            for kind, text in (("limit", "当前内容只能提供交流边界，不能授权新步骤。"),
                               ("conflict", "上下文的账户定义尚未明确，保持待核实。")):
                result["add"].append(atom(kind, text, controls["context_ids"][0]))
        if not primary and operative:
            first = operative[0]
            step = atom("step", "说明交流安排并确认客户的时间。", first)
            primary = step["id"]
            result["add"].append(step)
            result["step_order"].append(primary)
            attachments.append(("condition", "首次说明约20分钟；另加40分钟必须再次征得客户同意。", first))
        for identity in operative:
            content = evidence[identity]
            if "| 前置 |" in content:
                step = atom("step", "先表明身份并征得客户同意。", identity)
                result["add"].append(step)
                result["step_order"].insert(0, step["id"])
            if "| 结束 |" in content:
                step = atom("step", "确认下次交流安排。", identity)
                result["add"].append(step)
                result["step_order"].append(step["id"])
            for marker, kind, text in (
                ("| 生命周期 |", "condition", "退休后费用不会立刻下降，而是缓慢下降。"),
                ("| 可选 |", "example", "工作高光经历为可选示例，不是必讲步骤。"),
                ("| 未决 |", "conflict", "账户称谓未映射，保持待核实。"),
            ):
                if marker in content and primary:
                    attachments.append((kind, text, identity))
        for kind, text, identity in attachments:
            result["attach"].append(atom(kind, text, identity, parent=primary))
        if result["add"] or result["attach"]:
            result["no_change_reason"] = ""
        if name in self.bad_names:
            self.bad_names.remove(name)
            result["input_ids"] = ["unknown-unit"]
        return result

    async def request(self, account, method, path, **kwargs):
        request = kwargs.get("json", {})
        before = len(self.artifacts)
        result = await super().request(account, method, path, **kwargs)
        if len(self.artifacts) > before:
            for packet in self.artifacts[-1]:
                if packet["stage"] == "skill_section" and "body_patch" in packet["derived_controls"]:
                    self.body_packets.append(copy.deepcopy(packet))
                    self.contents[request["to"] + "/" + packet["output_path"]] = json.dumps(
                        self.output(packet), ensure_ascii=False,
                    )
        return result


class PatchOwner(ArtifactOwner):
    def __init__(self, root, *, body_limit=2, candidates=1, sources=None, routed=False):
        super().__init__(root)
        self.ov = self.fixture.ov = PatchFake()
        self.ov.candidate_count = candidates
        self.body_limit = body_limit
        self.sources = sources or [RAW]
        self.routed = routed

    def new(self, count=1):
        job, entries = super().new(len(self.sources))
        for entry, raw in zip(entries, self.sources, strict=True):
            # The original fixture paths are retained; only this new tmp source is constructed.
            path = Path(entry["local_path"])
            path.write_text(raw)
            entry.update(sha256=hashlib.sha256(raw.encode()).hexdigest(), content_bytes=len(raw.encode()))
        return self.repo.update_job(job["job_id"], staged_skill_body_protocol=patch_contract.PROTOCOL), entries

    async def _staged_artifact_batch_next(self, job, root, state):
        if state.get("skill_body_protocol") == patch_contract.PROTOCOL and not state.get("fixture_unit_scope_ready"):
            # Explicit unit/source association fixture: real freeze/read/hash validators,
            # but no claim that this replaces the production semantic extractor.
            units = freeze_source_units(root, {**state, "source_unit_protocol": UNIT_PROTOCOL})
            rows = [load_source_unit(root, i) for i in range(units["unit_count"])]
            ids = [row["id"] for row in rows]
            operative = [row["id"] for row in rows if row["kind"] == "table_row"]
            context = [row["id"] for row in rows if row["kind"] != "table_row"]
            candidates = _read(root / "plan.json")["skills"]
            for candidate in candidates:
                path = root / "scopes" / (candidate["name"] + ".json")
                scope = _read(path)
                precise = {"protocol": UNIT_PROTOCOL, "unit_ids": ids, "operative_ids": operative,
                           "context_ids": context, "method_groups": [{"item_id": "fixture-whole-method",
                               "task": candidate["task"], "boundary": candidate["boundary"],
                               "unit_ids": operative, "context_ids": context}],
                           "source_sha256": _digest([{key: row[key] for key in (
                               "id", "source_uri", "start_char", "end_char", "sha256", "source_sha256",
                           )} for row in rows])}
                precise["sha256"] = _digest(precise)
                scope["source_units"] = precise
                _save(path, scope)
                ledger = _ledger(root, candidate["name"])
                ledger["body_max_units"] = self.body_limit
                _save(root / "artifact-batches/candidates" / (candidate["name"] + ".json"), ledger)
            if self.routed:
                # Only the accepted semantic labels below are supplied by the fixture.
                # The production host must perform its own filtering and exact routing.
                bindings = {f"u{i}": [row["id"]] for i, row in enumerate(rows)}
                refs = list(bindings)
                _save(root / "catalog/manifest.json", {"method": len(candidates)})
                for index, candidate in enumerate(candidates):
                    item = {"item_id": f"fixture-{index}", "task": candidate["task"],
                            "boundary": candidate["boundary"], "source_refs": refs}
                    _save(root / f"catalog/method/{index:08d}.json", {"item": item,
                                                                          "source_unit_bindings": bindings})
                    _save(root / f"method-decisions/{item['item_id']}.json",
                          {"target": "candidate", "name": candidate["name"],
                           "reason": "独立集成测试冻结的完整方法关联。"})
                _save(root / "units/0/00000000.json", {"assigned_unit_ids": ids,
                    "source_unit_bindings": bindings, "value": {
                        "methods": [{"source_refs": refs}], "facts": [], "deferred": [],
                        "context_refs": [ref for ref, values in bindings.items() if values[0] in context],
                        "context_uncertain": False,
                    }})
                route_exact_source_candidates(root, {**state, "unit_count": 1,
                    "source_unit_count": len(rows)}, rows[0]["source_uri"].rsplit("/", 1)[0])
            state["fixture_unit_scope_ready"] = True
            _save(root / "state.json", state)
        return await super()._staged_artifact_batch_next(job, root, state)


async def first_body(owner):
    job = await start(owner, 1)
    for _ in range(100):
        root, state = await owner._staged_load(job)
        if state.get("current", {}).get("stage") == "artifact_batch":
            return job, root, state
        assert job["status"] not in {"failed", "stopped"}, job.get("error")
        job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    raise AssertionError("Body boundary was not reached")


@pytest.mark.asyncio
async def test_multibatch_body_preserves_conditions_reorders_and_completes_twelve_cases(tmp_path):
    owner = PatchOwner(tmp_path)
    final = await drive(owner, await start(owner, 1))
    assert final["status"] == "waiting_human", final.get("error")
    root, state = await owner._staged_load(final)
    candidate = _read(root / "plan.json")["skills"][0]
    ledger = _ledger(root, candidate["name"])
    accepted = body_state(root, candidate, ledger["section_count"])
    assert state["complete"] and state["body_patch_complete"]
    assert state["skill_body_protocol"] == patch_contract.PROTOCOL
    assert len(owner.ov.body_packets) == 4 and ledger["section_count"] == 4
    scope = _read(root / "scopes" / (candidate["name"] + ".json"))["source_units"]
    assert [identity for receipt in accepted["receipts"] for identity in receipt["input_ids"]] == scope["unit_ids"]
    workspace = owner.repo.workspace(final) / "compiled_skill" / candidate["name"]
    skill = (workspace / "SKILL.md").read_text()
    assert "# 完整客户沟通方法" in skill
    assert "另加40分钟必须再次征得客户同意" in skill
    assert "退休后费用不会立刻下降，而是缓慢下降" in skill
    assert "可选示例，不是必讲步骤" in skill and "账户称谓未映射，保持待核实" in skill
    assert skill.index("1. 先表明身份") < skill.index("2. 说明交流安排")
    assert skill.count("说明交流安排并确认客户的时间") == 1
    assert (workspace / "EVALUATION.md").is_file()
    cases = _read(workspace / "benchmark.json")["cases"]
    assert len(cases) == len({row["case_id"] for row in cases}) == 12
    assert all(row["derived_controls"]["body_patch"]["title"] == "完整客户沟通方法"
               for row in owner.ov.body_packets[1:])


@pytest.mark.asyncio
@pytest.mark.parametrize("failure", ["capacity", "invalid"])
async def test_only_failed_file_splits_or_repairs_without_rerunning_successful_sibling(tmp_path, failure):
    owner = PatchOwner(tmp_path, body_limit=32, candidates=2)
    names = owner.ov.capacity_names if failure == "capacity" else owner.ov.bad_names
    names.add("refund-review-1")
    job, root, state = await first_body(owner)
    assert len(owner.ov.body_packets) == 2
    job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    assert _ledger(root, "refund-review-0")["skill_ready"]
    assert _ledger(root, "refund-review-1")["cursor"] == 0
    if failure == "capacity":
        assert _ledger(root, "refund-review-1")["body_max_units"] == 3
        assert len(owner.ov.body_packets[-1]["input_ids"]) == 3
    final = await drive(owner, job)
    assert final["status"] == "waiting_human", final.get("error")
    submitted_names = [row["derived_controls"]["skill_name"] for row in owner.ov.body_packets]
    assert submitted_names.count("refund-review-0") == 1
    assert submitted_names.count("refund-review-1") == (4 if failure == "capacity" else 2)
    for name in ("refund-review-0", "refund-review-1"):
        assert len(_read(owner.repo.workspace(final) / f"compiled_skill/{name}/benchmark.json")["cases"]) == 12


@pytest.mark.asyncio
async def test_projection_written_before_ledger_crash_replays_without_duplicate_atoms_or_compile(tmp_path, monkeypatch):
    owner = PatchOwner(tmp_path)
    job, root, state = await first_body(owner)
    task = owner.ov.tasks[job["ov_task_id"]]
    original_save = _save
    crashed = False

    def crash(path, value):
        nonlocal crashed
        if "/body-patches/" in str(path) and "/states/" in str(path) and not crashed:
            original_save(path, value)
            crashed = True
            raise OSError("Simulated crash after durable next-state projection")
        return original_save(path, value)

    monkeypatch.setattr("team_miner.skill_miner.ov_staged_orchestration._save", crash)
    retriable = await owner._advance_staged_round(job, task)
    assert crashed and "Simulated crash" in retriable["submission_error"]
    assert len(owner.ov.body_packets) == 1 and _ledger(root, "refund-review")["cursor"] == 0
    monkeypatch.setattr("team_miner.skill_miner.ov_staged_orchestration._save", original_save)
    recovered = await owner._advance_staged_round(retriable, task)
    assert len(owner.ov.body_packets) == 2
    first = _read(root / "body-patches/refund-review/states/00000001.json")
    assert len(first["atoms"]) == 2 and len(first["receipts"]) == 1
    final = await drive(owner, recovered)
    assert final["status"] == "waiting_human", final.get("error")
    assert len(owner.ov.body_packets) == 4
    final_state = _read(root / "body-patches/refund-review/states/00000004.json")
    assert len(final_state["atoms"]) == len({row["id"] for row in final_state["atoms"]}) == 7


@pytest.mark.asyncio
@pytest.mark.parametrize("missing", ["state", "manifest"])
async def test_frozen_missing_patch_state_fails_without_runtime_upgrade_or_new_submission(tmp_path, missing):
    owner = PatchOwner(tmp_path)
    job, root, state = await first_body(owner)
    job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    if missing == "state":
        prior = root / "body-patches/refund-review/states/00000001.json"
        message = "状态文件缺失"
    else:
        current = (await owner._staged_load(job))[1]["current"]
        prior = root / _read(root / current["packet_file"])[0]["context"]["body_manifest_file"]
        message = "任务清单缺失"
    prior.unlink()
    before = len(owner.ov.calls)
    with pytest.raises(ValueError, match=message):
        await owner._resume_staged_round(job)
    failed = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    assert failed["status"] == "failed" and message in failed["error"]
    assert len(owner.ov.calls) == before
    assert not prior.exists()


@pytest.mark.asyncio
async def test_single_unit_capacity_fails_without_discarding_source_or_fabricating_body(tmp_path):
    owner = PatchOwner(tmp_path, body_limit=1)
    owner.ov.capacity_single = True
    job, root, state = await first_body(owner)
    final = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    assert final["status"] == "failed" and "单个完整原文单元" in final["error"]
    assert _ledger(root, "refund-review")["cursor"] == 0
    assert not list((root / "body-patches/refund-review/states").glob("*.json"))
    assert not list(owner.repo.workspace(final).glob("compiled_skill/*/SKILL.md"))


@pytest.mark.asyncio
async def test_context_first_retains_limit_and_conflict_without_authorizing_a_step(tmp_path):
    owner = PatchOwner(tmp_path, body_limit=1)
    owner.ov.context_atoms = True
    job, root, state = await first_body(owner)
    assert not owner.ov.body_packets[0]["derived_controls"]["body_patch"]["operative_ids"]
    job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    first = _read(root / "body-patches/refund-review/states/00000001.json")
    assert {row["kind"] for row in first["atoms"]} == {"limit", "conflict"}
    assert first["step_order"] == []
    final = await drive(owner, job)
    assert final["status"] == "waiting_human", final.get("error")
    skill = (owner.repo.workspace(final) / "compiled_skill/refund-review/SKILL.md").read_text()
    assert "当前内容只能提供交流边界" in skill and "上下文的账户定义尚未明确" in skill
    assert "1. 先表明身份" in skill


@pytest.mark.asyncio
async def test_production_scope_excludes_empty_and_separator_but_keeps_complete_raw_archive(tmp_path):
    owner = PatchOwner(tmp_path, sources=["\n\n" + RAW + "\n\n", ""], routed=True)
    final = await drive(owner, await start(owner, 1))
    assert final["status"] == "waiting_human", final.get("error")
    root, state = await owner._staged_load(final)
    manifest = _read(root / "source-units/manifest.json")
    archived = [load_source_unit(root, i) for i in range(manifest["unit_count"])]
    excluded = {row["id"] for row in archived if row["kind"] in {"empty", "separator"}}
    assert {row["kind"] for row in archived} >= {"empty", "separator", "table_header", "table_row"}
    routed = _read(root / "scopes/refund-review.json")["source_units"]
    assert not set(routed["unit_ids"]) & excluded
    assert _read(root / "source-routing/units.json")["source_unit_count"] == len(archived)
    assert set(_read(root / "units/0/00000000.json")["assigned_unit_ids"]) == {row["id"] for row in archived}
    for packet in owner.ov.body_packets:
        assert all(row["content"].strip() for row in packet["evidence"])
        assert not set(packet["input_ids"]) & excluded
    assert {row["id"] for packet in owner.ov.body_packets for row in packet["evidence"]} == set(routed["unit_ids"])


@pytest.mark.asyncio
async def test_job_flag_cannot_upgrade_an_existing_frozen_legacy_body_round(tmp_path):
    class LegacyOwner(ArtifactOwner):
        def new(self, count=1):
            job, entries = super().new(count)
            return self.repo.update_job(job["job_id"], staged_skill_body_protocol="",
                                        staged_source_unit_protocol=""), entries

    owner = LegacyOwner(tmp_path)
    owner.ov.candidate_count = 1
    job, root, state = await first_body(owner)
    protocol = state.get("skill_body_protocol")
    frozen_compiler = (root / "compilers/skill_section.md").read_bytes()
    job = owner.repo.update_job(job["job_id"], staged_skill_body_protocol=patch_contract.PROTOCOL,
                                staged_source_unit_protocol=UNIT_PROTOCOL)
    final = await drive(owner, await owner._resume_staged_round(job))
    assert final["status"] == "waiting_human", final.get("error")
    assert (await owner._staged_load(final))[1].get("skill_body_protocol") == protocol
    assert (root / "compilers/skill_section.md").read_bytes() == frozen_compiler
    assert not (root / "body-patches").exists()
    assert all("body_patch" not in packet["derived_controls"]
               for batch in owner.ov.artifacts for packet in batch)
