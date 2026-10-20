# ruff: noqa: F401
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
