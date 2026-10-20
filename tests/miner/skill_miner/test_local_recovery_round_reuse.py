"""Local selection recovery preserves frozen assignments and reviewed round dependencies."""

from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from team_miner.skill_miner.compile_strategy import mining_policy_block
from team_miner.skill_miner.direct_workflow_planning import (
    LOCAL_SELECTION_RECOVERY_PROTOCOL,
    SELECTION_RECOVERY_PROTOCOL,
)
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import SOURCE_INPUT_CLARITY_PROTOCOL, _read, _save, _sha
from team_miner.skill_miner.round_reuse import PROTOCOL_FIELDS, try_reuse_candidate
from tests.miner.skill_miner.support.direct_workflow_planning import DirectFake
from tests.miner.skill_miner.support.extract_batches import begin
from tests.miner.skill_miner.support.ov_staged_orchestration import drive
from tests.miner.skill_miner.support.round_reuse import POLICY, next_inputs
from tests.miner.skill_miner.support.selection_window_recovery import RecoveryOwner


class LocalRecoveryFake(DirectFake):
    """Fail only the first original window; later independent windows are valid."""

    def __init__(self, *, threshold=6, refuse_all=False, source_failure=False):
        super().__init__()
        self.threshold, self.refuse_all = threshold, refuse_all
        self.failed_window_ids = None
        self.successful_assignments = []
        if source_failure:
            self.break_indexes[0] = ["deferred_text"]

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        request = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile":
            return result
        source = request["from"][0]
        if "/stages/extract_batch-" in source:
            packets = [json.loads(text) for uri, text in self.contents.items() if uri.startswith(source + "/")]
            for packet in packets:
                target = request["to"] + "/" + packet["output_path"]
                if target not in self.contents or not all("/original/" in row["source_uri"]
                                                        for row in packet["evidence"]):
                    continue
                value = json.loads(self.contents[target])
                # Preserve the intentional source schema defect until the real bounded repair.
                if any("reason" not in row for row in value.get("deferred", [])):
                    continue
                value["methods"] = [{"task": "退款复核", "boundary": "只按原始政策核实申请，缺值时询问用户。",
                                     "source_refs": [ref]}
                                    for ref in packet["derived_controls"]["allowed_source_refs"]]
                value["facts"], value["context_refs"] = [], []
                self.contents[target] = json.dumps(value, ensure_ascii=False)
            return result
        if "/stages/plan-" not in source:
            return result
        packet = json.loads(self.contents[source + "/packet.json"])
        assigned = [row["item_id"] for row in packet["derived_controls"]["items"]]
        if self.failed_window_ids is None:
            self.failed_window_ids = set(assigned)
        if self.refuse_all or len(assigned) > self.threshold and set(assigned) <= self.failed_window_ids:
            target = request["to"] + "/selection.json"
            value = json.loads(self.contents[target])
            value["skills"][0]["derived_from"].pop()
            self.contents[target] = json.dumps(value, ensure_ascii=False)
        else:
            self.successful_assignments.append(assigned)
        return result


class LocalRecoveryOwner(RecoveryOwner):
    def __init__(self, root, *, protocol=LOCAL_SELECTION_RECOVERY_PROTOCOL, clarity=True, **fake_options):
        super().__init__(root)
        self.recovery_protocol, self.clarity = protocol, clarity
        self.ov = self.fixture.ov = LocalRecoveryFake(**fake_options)

    def new(self, count=54):
        job, entries = super().new(count)
        for index, entry in enumerate(entries):
            text = f"原文文档{index}：依据客户退款申请复核既有政策；资料缺值时询问用户，不猜测规则。\n"
            Path(entry["local_path"]).write_text(text, encoding="utf-8")
            entry.update(sha256=_sha(text), content_bytes=len(text.encode()))
        job = self.repo.update_job(
            job["job_id"], staged_selection_recovery_protocol=self.recovery_protocol,
            staged_source_input_clarity_protocol=SOURCE_INPUT_CLARITY_PROTOCOL if self.clarity else "",
        )
        return job, entries


async def reviewed(owner, count=54):
    job, entries = owner.new(count)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert final["status"] == "waiting_human", final.get("error")
    return final


def dataset(owner, job):
    return _read(owner.repo.workspace(job) / "compiled_skill/refund-review/benchmark.json")


def frozen_files(root):
    return {str(path.relative_to(root)): path.read_bytes()
            for folder in ("chunks", "children", "compilers", "aux-compilers")
            for path in (root / folder).rglob("*") if path.is_file()}


def test_local_source_and_selection_repairs_restore_full_windows_and_reuse_reviewed_round(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path, source_failure=True)
        first = await reviewed(owner)
        root, state = await owner._staged_load(first)
        assert state["selection_recovery_protocol"] == LOCAL_SELECTION_RECOVERY_PROTOCOL
        assert state["source_input_clarity_protocol"] == SOURCE_INPUT_CLARITY_PROTOCOL
        assert state["proposal_count"] == 54 and state["validation_splits"] == 2
        assert not state.get("direct_recovery_limit") and not state.get("direct_recovery_end")
        assert [len(packet["derived_controls"]["items"]) for packet in owner.ov.selection_packets] == [
            24, 24, 12, 12, 6, 6, 6, 6, 24, 6,
        ]
        accepted = [identity for batch in owner.ov.successful_assignments for identity in batch]
        assert len(accepted) == len(set(accepted)) == 54
        assert len(list((root / "method-decisions").glob("*.json"))) == 54
        splits = [_read(path) for path in (root / "direct-planning/validation-splits").glob("*.json")]
        assert {row["recovery_end"] for row in splits} == {24}
        assert all(row["accepted_dispositions"] == 0 for row in splits)
        # Good source slots remain accepted while just the malformed slot is resubmitted.
        source_zero = [packet for batch in owner.ov.extract_batches for packet in batch
                       if packet["output_path"] == "index-00000000.json"]
        assert len(source_zero) == 2
        original_ids = [case["case_id"] for case in dataset(owner, first)["cases"]]
        assert len(original_ids) == len(set(original_ids)) == 12
        before = len(owner.ov.extract_batches), len(owner.ov.artifacts), len(owner.ov.batches)
        originals = frozen_files(root)

        second, entries = await next_inputs(owner, copy.deepcopy(first))
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        second_root, second_state = await owner._staged_load(second)
        assert second_state["reused_extract_count"] > 0 and second_state["reused_candidate_count"] == 1
        assert (len(owner.ov.artifacts), len(owner.ov.batches)) == before[1:]
        assert [case["case_id"] for case in dataset(owner, second)["cases"]] == original_ids
        assert all("/supplements/" in chunk["source_uri"]
                   for batch in owner.ov.extract_batches[before[0]:]
                   for packet in batch for chunk in packet["evidence"])
        manifest = _read(second_root / "reuse/manifest.json")
        assert manifest["previous_root"].endswith("/rounds/round-1")
        assert all((root / relative).read_bytes() == content for relative, content in originals.items())

    asyncio.run(check())


def test_restart_after_local_split_keeps_original_end_and_never_rewrites_rejected_inputs(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path)
        next_stage = owner._staged_next
        crashed = False

        async def crash_after_split(job, root, state):
            nonlocal crashed
            if state.get("validation_splits") == 1 and state.get("current") is None and not crashed:
                crashed = True
                raise RuntimeError("process exited after local split journal")
            return await next_stage(job, root, state)

        owner._staged_next = crash_after_split
        with pytest.raises(RuntimeError, match="local split journal"):
            await drive(owner, await begin(owner, 30))
        job = owner.repo.list_jobs()[0]
        root, state = await owner._staged_load(job)
        assert state["selection_cursor"] == 0 and state["current"] is None
        assert state["direct_recovery_limit"] == 12 and state["direct_recovery_end"] == 24
        frozen, source_count = frozen_files(root), len(owner.ov.extract_batches)
        owner._staged_next = next_stage
        final = await drive(owner, await owner._resume_staged_round(job))
        assert final["status"] == "waiting_human", final.get("error")
        _, state = await owner._staged_load(final)
        assert state["validation_splits"] == 2 and state["selection_cursor"] == 30
        assert not state.get("direct_recovery_limit") and not state.get("direct_recovery_end")
        assert len(owner.ov.extract_batches) == source_count
        assert all((root / relative).read_bytes() == content for relative, content in frozen.items())
        assert len(dataset(owner, final)["cases"]) == 12

    asyncio.run(check())


@pytest.mark.parametrize("change", ["clarity_protocol", "compiler_policy"])
def test_changed_input_clarity_profile_or_actual_compiler_cannot_reuse_reviewed_outputs(tmp_path, change):
    async def check():
        assert "source_input_clarity_protocol" in PROTOCOL_FIELDS
        owner = LocalRecoveryOwner(tmp_path, threshold=24)
        first = await reviewed(owner, 9)
        before = len(owner.ov.extract_batches), len(owner.ov.artifacts), len(owner.ov.batches)
        second, entries = await next_inputs(owner, first)
        body = SKILL_BODY
        if change == "clarity_protocol":
            second = owner.repo.update_job(second["job_id"], staged_source_input_clarity_protocol="")
        else:
            assert body.count(mining_policy_block()) == 1
            body = body.replace(mining_policy_block(),
                                mining_policy_block(common="核实当前原文中的每条条件；未知业务事实不允许补写。"))
        second = await drive(owner, await owner._prepare_staged_round(second, body, entries, POLICY, ""))
        assert second["status"] == "waiting_human", second.get("error")
        _, state = await owner._staged_load(second)
        assert not state.get("reused_extract_count") and not state.get("reused_candidate_count")
        assert len(owner.ov.artifacts) > before[1] and len(owner.ov.batches) > before[2]
        assert any("/original/" in chunk["source_uri"]
                   for batch in owner.ov.extract_batches[before[0]:]
                   for packet in batch for chunk in packet["evidence"])
        assert len(dataset(owner, second)["cases"]) == 12

    asyncio.run(check())


def test_frozen_v1_does_not_acquire_local_reset_or_input_clarity_from_late_job_flags(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path, protocol=SELECTION_RECOVERY_PROTOCOL, clarity=False)
        job = await begin(owner, 54)
        root, state = await owner._staged_load(job)
        assert state["selection_recovery_protocol"] == SELECTION_RECOVERY_PROTOCOL
        assert not state.get("source_input_clarity_protocol")
        frozen = frozen_files(root)
        job = owner.repo.update_job(
            job["job_id"], staged_selection_recovery_protocol=LOCAL_SELECTION_RECOVERY_PROTOCOL,
            staged_source_input_clarity_protocol=SOURCE_INPUT_CLARITY_PROTOCOL,
        )
        final = await drive(owner, await owner._resume_staged_round(job))
        assert final["status"] == "waiting_human", final.get("error")
        _, state = await owner._staged_load(final)
        assert state["selection_recovery_protocol"] == SELECTION_RECOVERY_PROTOCOL
        assert state["direct_recovery_limit"] == 6 and not state.get("direct_recovery_end")
        assert not state.get("source_input_clarity_protocol")
        assert [len(packet["derived_controls"]["items"]) for packet in owner.ov.selection_packets][8:] == [6] * 5
        assert all((root / relative).read_bytes() == content for relative, content in frozen.items())
        assert len(dataset(owner, final)["cases"]) == 12

    asyncio.run(check())


def test_stop_during_durable_local_reset_wins_without_a_new_native_submission(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path)
        save = owner._staged_save
        injected, posts_at_stop = False, 0

        async def stop_at_reset(job, root, state, **changes):
            nonlocal injected, posts_at_stop
            if (state.get("phase") == "selection" and state.get("selection_cursor") == 24
                    and state.get("validation_splits") and not state.get("direct_recovery_end")
                    and not state.get("direct_recovery_limit") and state.get("current") is None and not injected):
                injected = True
                posts_at_stop = len(owner.ov.calls)
                owner.repo.update_job(job["job_id"], status="stopped", finished_at="stop-finished",
                                      error="stop-error", ov_cancel_reason="user stop during local reset")
            return await save(job, root, state, **changes)

        owner._staged_save = stop_at_reset
        final = await drive(owner, await begin(owner, 54))
        assert injected and final["status"] == "stopped"
        assert final["finished_at"] == "stop-finished" and final["error"] == "stop-error"
        assert len(owner.ov.calls) == posts_at_stop
        _, state = await owner._staged_load(final)
        assert state["selection_cursor"] == 24 and state["current"] is None
        assert not state.get("direct_recovery_end") and not state.get("direct_recovery_limit")
        resumed = await owner._resume_staged_round(owner.repo.get_job(final["job_id"]))
        assert resumed["status"] == "stopped" and len(owner.ov.calls) == posts_at_stop
        assert not list(owner.repo.workspace(final).rglob("benchmark.json"))

    asyncio.run(check())


def test_invalid_single_method_has_only_three_attempts_without_any_accepted_disposition(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path, refuse_all=True)
        final = await drive(owner, await begin(owner, 1))
        root, state = await owner._staged_load(final)
        assert final["status"] == "failed" and not final["materialized"]
        assert len(owner.ov.selection_packets) == 3
        assert all(len(packet["derived_controls"]["items"]) == 1 for packet in owner.ov.selection_packets)
        assert state["selection_cursor"] == 0 and not state.get("validation_splits")
        assert not list((root / "method-decisions").glob("*.json"))
        assert not list(owner.repo.workspace(final).rglob("benchmark.json"))

    asyncio.run(check())


def test_frozen_host_lineage_damage_is_not_retried_or_treated_as_local_model_recovery(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path, threshold=24)
        job = await begin(owner, 3)
        while True:
            root, state = await owner._staged_load(job)
            if state["current"]["stage"] == "plan":
                break
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        path = root / "direct-planning/mapping/00000000.json"
        damaged = _read(path)
        damaged["record_sha256"] = "0" * 64
        _save(path, damaged)
        before = len(owner.ov.calls)
        final = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        assert final["status"] == "failed" and len(owner.ov.calls) == before
        _, state = await owner._staged_load(final)
        assert not state.get("validation_splits") and state["selection_cursor"] == 0
        assert not list((root / "method-decisions").glob("*.json"))

    asyncio.run(check())


def test_real_second_round_feedback_invalidates_artifacts_even_with_the_same_recovery_profile(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path, threshold=24)
        first = await reviewed(owner, 9)
        before = len(owner.ov.artifacts), len(owner.ov.batches)
        second, entries = await next_inputs(owner, first, feedback="请重新核实所有方法的适用边界。")
        revision = OvRoundOrchestrationMixin._staged_revision_instruction(second, 2)
        second = await drive(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, revision))
        assert second["status"] == "waiting_human", second.get("error")
        root, state = await owner._staged_load(second)
        assert state["selection_recovery_protocol"] == LOCAL_SELECTION_RECOVERY_PROTOCOL
        assert state["source_input_clarity_protocol"] == SOURCE_INPUT_CLARITY_PROTOCOL
        assert state["reused_extract_count"] > 0 and not state.get("reused_candidate_count")
        assert len(owner.ov.artifacts) > before[0] and len(owner.ov.batches) > before[1]
        assert _read(root / "reuse/manifest.json")["feedback"]["global"]
        assert len(dataset(owner, second)["cases"]) == 12

    asyncio.run(check())


async def historical_enabled_second_round(owner):
    """Build a real reviewed cache, then encode its pre-optional-field frozen header."""
    first = await reviewed(owner, 9)
    second, entries = await next_inputs(owner, first)
    second = await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, "")
    for _ in range(20):
        root, state = await owner._staged_load(second)
        if state["current"]["stage"] == "plan":
            break
        assert second["status"] not in {"failed", "stopped"}, second.get("error")
        second = await owner._advance_staged_round(second, owner.ov.tasks[second["ov_task_id"]])
    else:
        raise AssertionError("Historical reviewed source cache did not reach selection")
    path = root / "reuse/manifest.json"
    header = _read(path)
    assert header["enabled"] and header["extract_count"] > 0 and header["candidate_count"] == 1
    assert header["protocols"].pop("source_input_clarity_protocol") is None
    assert not state.get("source_input_clarity_protocol")
    state.pop("source_input_clarity_protocol", None)
    _save(path, header)
    # This is the fixture's historical commitment, not a bypass of the source,
    # checkpoint, compiler, Native request/result or feedback dependency checks.
    state["round_reuse_manifest_sha256"] = _sha(path.read_text(encoding="utf-8"))
    _save(root / "state.json", state)
    return first, second, root, state


def test_enabled_historical_second_round_resumes_without_migrating_its_frozen_manifest(tmp_path):
    async def check():
        owner = LocalRecoveryOwner(tmp_path, protocol=SELECTION_RECOVERY_PROTOCOL, clarity=False, threshold=24)
        first, second, root, state = await historical_enabled_second_round(owner)
        manifest = (root / "reuse/manifest.json").read_bytes()
        frozen = frozen_files(root)
        before = len(owner.ov.calls), len(owner.ov.artifacts), len(owner.ov.batches)
        previous = root.parent.parent.parent / "rounds/round-1"
        reviewed_dataset = _read(previous / "reviewed_output/compiled_skill/refund-review/benchmark.json")
        identities = [case["case_id"] for case in reviewed_dataset["cases"]]
        resumed = await owner._resume_staged_round(owner.repo.get_job(second["job_id"]))
        assert len(owner.ov.calls) == before[0]
        assert (root / "reuse/manifest.json").read_bytes() == manifest
        assert all((root / relative).read_bytes() == content for relative, content in frozen.items())
        final = await drive(owner, resumed)
        assert final["status"] == "waiting_human", final.get("error")
        _, after = await owner._staged_load(final)
        assert after["selection_recovery_protocol"] == SELECTION_RECOVERY_PROTOCOL
        assert not after.get("source_input_clarity_protocol")
        assert after["reused_extract_count"] == state["reused_extract_count"] > 0
        assert after["reused_candidate_count"] == 1
        assert (len(owner.ov.calls), len(owner.ov.artifacts), len(owner.ov.batches)) == before
        assert [case["case_id"] for case in dataset(owner, final)["cases"]] == identities
        assert len(identities) == 12 and len(set(identities)) == 12
        assert (root / "reuse/manifest.json").read_bytes() == manifest
        assert all((root / relative).read_bytes() == content for relative, content in frozen.items())

    asyncio.run(check())


@pytest.mark.parametrize("change", ["nonnull_state", "unknown_header_key"])
def test_historical_missing_optional_key_does_not_allow_changed_or_unknown_protocols(tmp_path, change):
    async def check():
        owner = LocalRecoveryOwner(tmp_path, protocol=SELECTION_RECOVERY_PROTOCOL, clarity=False, threshold=24)
        first, second, root, state = await historical_enabled_second_round(owner)
        if change == "nonnull_state":
            state["source_input_clarity_protocol"] = SOURCE_INPUT_CLARITY_PROTOCOL
        else:
            path = root / "reuse/manifest.json"
            header = _read(path)
            header["protocols"]["unknown_future_protocol"] = None
            _save(path, header)
            state["round_reuse_manifest_sha256"] = _sha(path.read_text(encoding="utf-8"))
        _save(root / "state.json", state)
        first_root, _ = await owner._staged_load(first)
        candidate = copy.deepcopy(_read(first_root / "plan.json")["skills"][0])
        candidate["source_refs"] = [ref.replace(first["source_snapshot"], second["source_snapshot"])
                                    for ref in candidate["source_refs"]]
        scope = _read(first_root / "scopes/refund-review.json")
        frozen, before = frozen_files(root), len(owner.ov.calls)
        manifest = (root / "reuse/manifest.json").read_bytes()
        with pytest.raises(ValueError, match="protocol changed"):
            try_reuse_candidate(second, root, state, candidate, scope, owner.repo)
        assert len(owner.ov.calls) == before and not state.get("reused_candidate_count")
        assert not list((root / "cases").rglob("*.json"))
        assert (root / "reuse/manifest.json").read_bytes() == manifest
        assert all((root / relative).read_bytes() == content for relative, content in frozen.items())

    asyncio.run(check())
