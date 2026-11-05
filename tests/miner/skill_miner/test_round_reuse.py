from __future__ import annotations

import asyncio
import copy
import json
from pathlib import Path

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
from team_miner.skill_miner.ov_round_orchestration import OvRoundOrchestrationMixin
from team_miner.skill_miner.ov_staged_orchestration import _alias, _freeze_chunks, _raw_group, _read, _save, _sha
from team_miner.skill_miner.round_reuse import (
    OPTIMIZED_EXECUTION_PROTOCOL,
    _file_sha,
    _profile,
    _sources,
    prepare_round_reuse,
    try_reuse_candidate,
    try_reuse_extract,
)
from tests.miner.skill_miner.support.artifact_batches import ArtifactFake, ArtifactOwner
from tests.miner.skill_miner.support.ov_staged_orchestration import drive

POLICY = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}


class ScopedFake(ArtifactFake):
    """Two unrelated methods, each explicitly supported by one assigned source file."""

    def __init__(self):
        super().__init__()
        self.candidate_count = 2

    async def request(self, account, method, path, **kwargs):
        result = await super().request(account, method, path, **kwargs)
        body = kwargs.get("json", {})
        if method != "POST" or path != "/api/v1/compile" or "/stages/extract-" not in body["from"][0]:
            return result
        packet = json.loads(self.contents[body["from"][0] + "/packet.json"])
        aliases = packet["derived_controls"]["reference_map"]
        eligible = [alias for alias, chunk_id in aliases.items()
                    if any(chunk["id"] == chunk_id and "/original/" in chunk["source_uri"]
                           for chunk in packet["evidence"])]
        output = json.loads(self.contents[body["to"] + "/index.json"])
        output["methods"] = [
            {**row, "source_refs": [eligible[index % len(eligible)]]}
            for index, row in enumerate(output["methods"])
        ] if eligible else []
        if not eligible:
            output["facts"] = [{
                "text": "该文档仅记录本轮复核，没有额外领域规则。", "source_refs": list(aliases),
            }]
        self.contents[body["to"] + "/index.json"] = json.dumps(output, ensure_ascii=False)
        return result


class ReuseOwner(ArtifactOwner):
    def __init__(self, root):
        super().__init__(root)
        self.ov = self.fixture.ov = ScopedFake()


async def reviewed_first(owner, *, question=False):
    job, entries = owner.new(2)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=3, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    if question:
        pending = copy.deepcopy(first["pending_checkpoint"])
        pending["questions"] = [{
            "qid": "source-question", "question": "请确认该方法的边界", "reason": "已知来源的复核问题",
            "required": False, "source_refs": [first["source_snapshot"] + "/original/doc-0.md"],
        }]
        first = owner.repo.update_job(first["job_id"], pending_checkpoint=pending)
    return first


async def next_inputs(owner, first, *, feedback="", answer=False):
    answers = {"source-question": "请核实明确边界，不推测额外事实。"} if answer else {}
    job = owner.repo.prepare_next_round(first["job_id"], first["pending_checkpoint"]["id"], answers, [], {},
                                       feedback=feedback)
    entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, job, staged=True)
    manifest = owner.repo._job_dir(job["job_id"]) / f"round_inputs/round-{job['current_round']}/manifest.json"
    job = owner.repo.update_job(job["job_id"], input_manifest=str(manifest))
    owner.ov.directories.add(job["source_snapshot"].rsplit("/", 1)[0])
    return job, entries


def prepared_only(owner, job, entries, *, body=SKILL_BODY, policy=POLICY):
    from team_miner.skill_miner.case_context import CASE_NARRATIVE_PROTOCOL

    root = owner._staged_root(job)
    count = _freeze_chunks(root, entries, job["source_snapshot"])
    (root / "compiler-base.md").write_text(body, encoding="utf-8")
    (root / "revision.txt").write_text("Frozen review context", encoding="utf-8")
    compilers = {}
    efficient = job.get("staged_efficiency_protocol") == "te-mining-efficient.v2"
    for stage in ("extract", "merge_index", "plan", "skill_section", "evaluation", "case"):
        value = planning.stage_compiler_body(body, stage)
        if efficient:
            from team_miner.skill_miner.compile_strategy import render_efficient_stage_compiler

            value = render_efficient_stage_compiler(body, stage)
        (root / "compilers").mkdir(exist_ok=True)
        (root / "compilers" / f"{stage}.md").write_text(value, encoding="utf-8")
        compilers[stage] = _sha(value)
    state = {
        "execution_protocol": OPTIMIZED_EXECUTION_PROTOCOL, "round": job["current_round"],
        "compilers": compilers, "base_sha256": _sha(body), "language_policy": copy.deepcopy(policy),
        "chunk_count": count, "cursor": 0, "unit_count": 0,
        "case_narrative_protocol": CASE_NARRATIVE_PROTOCOL,
    }
    if job.get("staged_skill_body_protocol"):
        state["skill_body_protocol"] = job["staged_skill_body_protocol"]
    if efficient:
        from team_miner.skill_miner.ov_extract_batch_orchestration import EXTRACT_BATCH_PROTOCOL
        from team_miner.skill_miner.workflow_planning import WORKFLOW_PLANNING_PROTOCOL

        state.update(efficiency_protocol="te-mining-efficient.v2", extract_batch_protocol=EXTRACT_BATCH_PROTOCOL,
                     workflow_planning_protocol=WORKFLOW_PLANNING_PROTOCOL)
    prepare_round_reuse(job, root, state, entries)
    return root, state


def candidate_and_scope(root, name="refund-review-0"):
    previous = root.parent.parent.parent / "rounds/round-1"
    plan = _read(previous / "reviewed_output/semantic_reports/capability-plan.json")
    candidate = copy.deepcopy(next(row for row in plan["skills"] if row["name"] == name))
    old_prefix = root.parent.parent.parent.name + "/rounds/round-1/"
    new_prefix = root.parent.parent.parent.name + "/rounds/round-2/"
    candidate["source_refs"] = [ref.replace(old_prefix, new_prefix) for ref in candidate["source_refs"]]
    scope = _read(previous / "input/staged/scopes" / f"{name}.json")
    return candidate, scope


def test_full_optimized_two_rounds_reuse_unchanged_extraction_and_all_candidate_artifacts(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        before_artifacts, before_batches = len(owner.ov.artifacts), len(owner.ov.batches)
        job, entries = await next_inputs(owner, first)
        result = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert result["status"] == "waiting_human", result.get("error")
        root, state = await owner._staged_load(result)
        assert state["reused_extract_count"] == 1
        assert state["reused_candidate_count"] == 2
        assert len(owner.ov.artifacts) == before_artifacts and len(owner.ov.batches) == before_batches
        assert state["complete"] and result["artifact_quality"]["level"] == "ready"
        assert all(row["can_submit"] for row in result["candidate_quality"].values())
        for name in state["candidate_names"]:
            document = _read(owner.repo.workspace(result) / f"compiled_skill/{name}/benchmark.json")
            assert len(document["cases"]) == 12
            assert len({case["case_id"] for case in document["cases"]}) == 12
            assert all(ref.startswith(job["source_snapshot"] + "/original/")
                       for case in document["cases"] for ref in case["provenance"]["session_ids"])
        # Round three consumes only the immediate reviewed Round two, including audited reused units.
        third, third_entries = await next_inputs(owner, result)
        final = await drive(owner, await owner._prepare_staged_round(third, SKILL_BODY, third_entries, POLICY, ""))
        assert final["status"] == "waiting_human", final.get("error")
        third_root, third_state = await owner._staged_load(final)
        assert third_state["reused_candidate_count"] == 2 and third_state["reused_extract_count"] >= 1
        assert _read(third_root / "reuse/manifest.json")["previous_root"].endswith("/rounds/round-2")
        assert len(owner.ov.artifacts) == before_artifacts and len(owner.ov.batches) == before_batches

    asyncio.run(check())


def test_unscoped_feedback_invalidates_candidates_but_is_not_treated_as_extraction_evidence(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        before_artifacts, before_batches = len(owner.ov.artifacts), len(owner.ov.batches)
        job, entries = await next_inputs(owner, first, feedback="请重新核实所有方法的适用边界。")
        revision = OvRoundOrchestrationMixin._staged_revision_instruction(job, 2)
        final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, revision))
        assert final["status"] == "waiting_human", final.get("error")
        root, state = await owner._staged_load(final)
        assert state["reused_extract_count"] == 1 and not state.get("reused_candidate_count")
        assert len(owner.ov.artifacts) > before_artifacts and len(owner.ov.batches) > before_batches
        assert _read(root / "reuse/manifest.json")["feedback"]["global"]
        current_round_requests = [request for _, request in owner.ov.calls
                                  if "/rounds/round-2/" in request["from"][0]]
        assert any("请重新核实" in request["instruction"] for request in current_round_requests)

    asyncio.run(check())


def test_source_scoped_review_recomputes_only_overlapping_candidate(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner, question=True)
        before_artifacts, before_batches = len(owner.ov.artifacts), len(owner.ov.batches)
        job, entries = await next_inputs(owner, first, answer=True)
        final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY,
                                                                   OvRoundOrchestrationMixin._staged_revision_instruction(
                                                                       job, 2)))
        assert final["status"] == "waiting_human", final.get("error")
        root, state = await owner._staged_load(final)
        assert state["reused_candidate_count"] == 1
        assert state["reused_candidate_names"] == ["refund-review-1"]
        new_artifacts = [packet for batch in owner.ov.artifacts[before_artifacts:] for packet in batch]
        new_cases = [packet for batch in owner.ov.batches[before_batches:] for packet in batch]
        assert all(packet["derived_controls"]["skill_name"] == "refund-review-0"
                   for packet in new_artifacts + new_cases)
        impact = _read(root / "reuse/manifest.json")["feedback"]
        assert not impact["global"] and impact["paths"] == ["original/doc-0.md"]

    asyncio.run(check())


@pytest.mark.parametrize("change", ["canonical", "scope", "language", "policy"])
def test_changed_candidate_dependencies_miss_without_copying_any_cases(tmp_path, change):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        job, entries = await next_inputs(owner, first)
        policy = {**POLICY, "language": "en"} if change == "language" else POLICY
        body = SKILL_BODY.replace("Never invent", "Absolutely never invent", 1) if change == "policy" else SKILL_BODY
        # Changing one actual compiler business rule, rather than an unrelated wrapper, invalidates reuse.
        if change == "policy":
            from team_miner.skill_miner.compile_strategy import mining_policy_block

            body = ("---\nname: custom-policy\ndescription: Test policy\n---\n\n"
                    + mining_policy_block("New business policy"))
        root, state = prepared_only(owner, job, entries, body=body, policy=policy)
        candidate, scope = candidate_and_scope(root)
        if change == "canonical":
            candidate["boundary"] += " Changed applicable boundary."
        elif change == "scope":
            scope["ordinals"] = [0, 1]
        assert not try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        assert not list((root / "cases").glob("*/*.json"))

    asyncio.run(check())


@pytest.mark.parametrize("tamper", ["unit", "result", "artifact", "source", "manifest", "feedback"])
def test_reuse_rejects_changed_frozen_authority_without_accepting_any_output(tmp_path, tamper):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        job, entries = await next_inputs(owner, first)
        root, state = prepared_only(owner, job, entries)
        previous = root.parent.parent.parent / "rounds/round-1"
        if tamper == "unit":
            path = previous / "input/staged/units/0/00000000.json"
        elif tamper == "result":
            path = next((previous / "input/staged/children").glob("extract-*/result.txt"))
        elif tamper == "artifact":
            path = previous / "reviewed_output/compiled_skill/refund-review-0/SKILL.md"
        elif tamper == "source":
            path = Path(entries[0]["local_path"])
        elif tamper == "manifest":
            path = root / "reuse/manifest.json"
        else:
            job = {**job, "last_checkpoint_submission": {**job["last_checkpoint_submission"], "feedback": "Changed"}}
            path = None
        if path:
            with path.open("a", encoding="utf-8") as stream:
                stream.write("\nchanged frozen bytes\n")
        with pytest.raises(ValueError):
            if tamper in {"unit", "result"}:
                chunks, after = _raw_group(root, list(range(state["chunk_count"])), 0)
                context = {
                    "after": after, "ordinals": [chunk["ordinal"] for chunk in chunks],
                    "input_ids": [chunk["id"] for chunk in chunks],
                    "source_refs": [_alias(chunk["id"]) for chunk in chunks],
                }
                try_reuse_extract(job, root, state, chunks, context)
            else:
                candidate, scope = candidate_and_scope(root)
                try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        assert not list((root / "cases").glob("*/*.json"))

    asyncio.run(check())


def test_candidate_application_and_count_are_idempotent_before_scheduler_ledger_save(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        job, entries = await next_inputs(owner, first)
        root, state = prepared_only(owner, job, entries)
        candidate, scope = candidate_and_scope(root)
        assert try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        copied = {path.name: path.read_bytes() for path in (root / "cases/refund-review-0").glob("*.json")}
        assert try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        assert state["reused_candidate_count"] == 1 and state["reused_candidate_names"] == [candidate["name"]]
        assert {path.name: path.read_bytes() for path in (root / "cases/refund-review-0").glob("*.json")} == copied
        assert len(list((root / "reuse/applied/candidates").glob("*.json"))) == 1

    asyncio.run(check())


def test_unreviewed_archive_is_not_a_reuse_source_and_job_round_binding_is_strict(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        job, entries = await next_inputs(owner, first)
        checkpoint = owner.repo._job_dir(job["job_id"]) / "rounds/round-1/checkpoint_submission.json"
        checkpoint.unlink()
        root, state = prepared_only(owner, job, entries)
        assert not _read(root / "reuse/manifest.json")["enabled"]
        candidate, scope = candidate_and_scope(root)
        assert not try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        # Restoring a reviewed checkpoint then copying its cache to another job is never permitted.
        _save(checkpoint, first["last_checkpoint_submission"] if first.get("last_checkpoint_submission") else {
            **job["last_checkpoint_submission"], "action": "continue",
        })
        prepare_round_reuse(job, root, state, entries)
        with pytest.raises(ValueError, match="cross jobs"):
            try_reuse_candidate({**job, "job_id": "another-job"}, root, state, candidate, scope, owner.repo)

    asyncio.run(check())


def test_more_than_one_thousand_reuse_hits_do_not_recurse_or_build_all_ordinal_lists(tmp_path, monkeypatch):
    from team_miner.skill_miner import ov_artifact_batch_orchestration, ov_staged_part0, round_reuse

    class LoopOwner(ReuseOwner):
        async def _staged_save(self, job, root, state, **changes):
            return job

        async def _staged_catalog_ready(self, job, root, state):
            return {**job, "loop_complete": True}

        async def _staged_assemble(self, job, root, state):
            return {**job, "loop_complete": True}

    async def check():
        owner = LoopOwner(tmp_path)
        job, _ = owner.new(1)
        root = owner._staged_root(job)
        counts = {"raw": 0, "reuse": 0}

        def raw_group(folder, ordinals, cursor):
            assert isinstance(ordinals, range)
            counts["raw"] += 1
            return [{"id": f"chunk-{cursor}", "ordinal": cursor}], cursor + 1

        def reused(job, folder, state, chunks, context):
            assert len(chunks) == 1
            counts["reuse"] += 1
            state.update(cursor=context["after"], unit_count=state["unit_count"] + 1)
            return True

        monkeypatch.setattr(ov_staged_part0, "_raw_group", raw_group)
        monkeypatch.setattr(round_reuse, "try_reuse_extract", reused)
        state = {"phase": "extract", "cursor": 0, "chunk_count": 1001, "unit_count": 0,
                 "round_reuse_protocol": "test", "planning_version": 2}
        result = await owner._staged_next(job, root, state)
        assert result["loop_complete"] and state["cursor"] == state["unit_count"] == 1001
        assert counts == {"raw": 1001, "reuse": 1001} and not owner.ov.calls
        _save(root / "plan.json", {"skills": [], "deferred": []})
        monkeypatch.setattr(ov_artifact_batch_orchestration, "_ledger", lambda root, name: {"reused": True})
        state = {"phase": "case", "execution_protocol": OPTIMIZED_EXECUTION_PROTOCOL,
                 "artifact_batches_complete": True, "candidate_index": 0,
                 "candidate_names": [f"known-{index}" for index in range(1001)]}
        result = await owner._staged_candidate_next(job, root, state)
        assert result["loop_complete"] and state["candidate_index"] == 1001 and not owner.ov.calls

    asyncio.run(check())


def test_custom_twelve_case_ids_and_large_skill_are_preserved_with_bounded_streaming(tmp_path, monkeypatch):
    from team_miner.skill_miner.dataset_view import dataset_markdown

    identities = [f"stable-reviewed:{index:02d}" for index in range(12)]

    class LargeOwner(ReuseOwner):
        async def _complete_ov_skill_round(self, job):
            if job["current_round"] == 1:
                folder = self.repo.workspace(job) / "compiled_skill/refund-review-0"
                dataset_path = folder / "benchmark.json"
                document = _read(dataset_path)
                for case, identity in zip(document["cases"], identities, strict=True):
                    case["case_id"] = identity
                _save(dataset_path, document)
                (folder / "BENCHMARK.md").write_text(dataset_markdown(document), encoding="utf-8")
                with (folder / "SKILL.md").open("a", encoding="utf-8") as stream:
                    chunk = "根据原始材料核实条件，不补充未知事实。\n" * 2000
                    for _ in range(300):
                        stream.write(chunk)
                assert (folder / "SKILL.md").stat().st_size > 20 * 1024 * 1024
            return self.repo.finish_materialized_round(job["job_id"])

    async def check():
        owner = LargeOwner(tmp_path)
        first = await reviewed_first(owner)
        previous_hash = _file_sha(owner.repo.workspace(first) / "compiled_skill/refund-review-0/SKILL.md")
        original_read, original_write = Path.read_text, owner.repo.write_output_stream
        widths = []

        def bounded_read(path, *args, **kwargs):
            if path.name == "SKILL.md":
                raise AssertionError("Reusable large Skill must never be read wholly into memory")
            return original_read(path, *args, **kwargs)

        def bounded_write(job_id, relative, chunks):
            def checked():
                for chunk in chunks:
                    if relative == "compiled_skill/refund-review-0/SKILL.md":
                        widths.append(len(chunk))
                        assert len(chunk) <= 64 * 1024
                    yield chunk
            return original_write(job_id, relative, checked())

        monkeypatch.setattr(Path, "read_text", bounded_read)
        monkeypatch.setattr(owner.repo, "write_output_stream", bounded_write)
        before_artifacts, before_cases = len(owner.ov.artifacts), len(owner.ov.batches)
        job, entries = await next_inputs(owner, first)
        final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert final["status"] == "waiting_human", final.get("error")
        folder = owner.repo.workspace(final) / "compiled_skill/refund-review-0"
        assert _file_sha(folder / "SKILL.md") == previous_hash and widths
        assert [case["case_id"] for case in _read(folder / "benchmark.json")["cases"]] == identities
        assert len(owner.ov.artifacts) == before_artifacts and len(owner.ov.batches) == before_cases
        assert final["candidate_quality"]["refund-review-0"]["can_submit"]

    asyncio.run(check())


def test_unknown_review_scope_conservatively_invalidates_every_candidate(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner, question=True)
        pending = copy.deepcopy(first["pending_checkpoint"])
        pending["questions"][0]["source_refs"] = [first["source_snapshot"]]
        first = owner.repo.update_job(first["job_id"], pending_checkpoint=pending)
        job, entries = await next_inputs(owner, first, answer=True)
        root, state = prepared_only(owner, job, entries)
        assert _read(root / "reuse/manifest.json")["feedback"]["global"]
        for name in ("refund-review-0", "refund-review-1"):
            candidate, scope = candidate_and_scope(root, name)
            assert not try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        assert not list((root / "cases").glob("*/*.json"))

    asyncio.run(check())


def test_human_edited_reviewed_artifact_is_not_mistaken_for_an_unchanged_validated_result(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        with (owner.repo.workspace(first) / "compiled_skill/refund-review-0/SKILL.md").open("a") as stream:
            stream.write("\nHuman edited content requires a new source-grounded generation.\n")
        job, entries = await next_inputs(owner, first)
        root, state = prepared_only(owner, job, entries)
        candidate, scope = candidate_and_scope(root)
        assert not try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        candidate, scope = candidate_and_scope(root, "refund-review-1")
        assert try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        assert state["reused_candidate_names"] == ["refund-review-1"]

    asyncio.run(check())


def test_scope_protocol_and_context_roles_invalidate_reuse_even_with_identical_raw_ordinals(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        job, entries = await next_inputs(owner, first)
        root, _ = prepared_only(owner, job, entries)
        sources = _sources(entries, root.parent)
        legacy = {"ordinals": [0, 1], "method_ordinals": [0]}
        precise = {**legacy, "scope_protocol": "te-candidate-source-scope.v1", "context_ordinals": [1]}
        old = _profile(root, legacy, job["source_snapshot"], sources)
        current = _profile(root, precise, job["source_snapshot"], sources)
        assert "scope_protocol" not in old and current["scope_protocol"] == precise["scope_protocol"]
        assert old["ordinals"] == current["ordinals"] and old["method_ordinals"] == current["method_ordinals"]
        assert old != current  # Existing frozen legacy caches never acquire new precision semantics implicitly.
        changed = _profile(root, {**precise, "context_ordinals": [0]}, job["source_snapshot"], sources)
        assert changed["ordinals"] == current["ordinals"]
        assert changed["context_ordinals"] != current["context_ordinals"]
        version = _profile(root, {**precise, "scope_protocol": "te-candidate-source-scope.v2"},
                           job["source_snapshot"], sources)
        assert version != current and version["ordinals"] == current["ordinals"]

    asyncio.run(check())


def test_context_evidence_cannot_be_recorded_outside_the_actual_candidate_input_scope(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        job, entries = await next_inputs(owner, first)
        root, _ = prepared_only(owner, job, entries)
        sources = _sources(entries, root.parent)
        scope = {"ordinals": [0], "method_ordinals": [0], "scope_protocol": "te-candidate-source-scope.v1",
                 "context_ordinals": [1]}
        with pytest.raises(ValueError, match="context Evidence"):
            _profile(root, scope, job["source_snapshot"], sources)
        with pytest.raises(ValueError, match="nonempty source scope protocol"):
            _profile(root, {**scope, "scope_protocol": ""}, job["source_snapshot"], sources)

    asyncio.run(check())


async def reviewed_batch_inputs(tmp_path):
    from tests.miner.skill_miner.support.extract_batches import ExtractFake, ExtractOwner

    class ReuseExtractFake(ExtractFake):
        async def request(self, account, method, path, **kwargs):
            result = await super().request(account, method, path, **kwargs)
            body = kwargs.get("json", {})
            if (method == "POST" and path == "/api/v1/compile"
                    and "/stages/extract_batch-" in body["from"][0]):
                prefix = body["from"][0] + "/"
                for uri in sorted(self.contents):
                    if not uri.startswith(prefix):
                        continue
                    packet = json.loads(self.contents[uri])
                    if all("/supplements/" in chunk["source_uri"] for chunk in packet["evidence"]):
                        refs = packet["derived_controls"]["allowed_source_refs"]
                        self.contents[body["to"] + "/" + packet["output_path"]] = json.dumps({
                            "schema_version": planning.INDEX_SCHEMA_V2, "input_ids": packet["input_ids"],
                            "methods": [], "facts": [{"text": "本轮复核记录未增加领域规则。", "source_refs": refs}],
                            "deferred": [], "context_refs": [], "context_uncertain": False,
                        }, ensure_ascii=False)
            return result

    owner = ExtractOwner(tmp_path, full=True)
    owner.fixture.ov = owner.ov = ReuseExtractFake()
    job, entries = owner.new(4)
    manifest = owner.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
    _save(manifest, entries)
    job = owner.repo.update_job(job["job_id"], max_rounds=2, input_manifest=str(manifest))
    first = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
    assert first["status"] == "waiting_human", first.get("error")
    return owner, first, await next_inputs(owner, first)


def test_real_file_map_v2_indexes_reuse_context_ranges_with_full_final_dataset(tmp_path):
    async def check():
        owner, _, (job, entries) = await reviewed_batch_inputs(tmp_path)
        before = len(owner.ov.extract_batches)
        before_artifacts, before_cases = len(owner.ov.artifacts), len(owner.ov.batches)
        final = await drive(owner, await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, ""))
        assert final["status"] == "waiting_human", final.get("error")
        root, state = await owner._staged_load(final)
        assert state["reused_extract_count"] == 2
        assert len(owner.ov.extract_batches) == before + 1  # Only the new review record needs extraction.
        for path in sorted((root / "units/0").glob("*.json"))[:2]:
            unit = _read(path)
            assert unit["value"]["schema_version"] == planning.INDEX_SCHEMA_V2
            assert unit["value"]["context_refs"]
            assert unit["context_ordinals"]
            assert all(ref in unit["bindings"] for ref in unit["value"]["context_refs"])
            assert set(unit["value"]["input_ids"]) <= {
                _read(root / "chunks" / f"{index:08d}.json")["id"] for index in range(state["chunk_count"])
            }
        assert state["reused_candidate_count"] == 1
        assert len(owner.ov.artifacts) == before_artifacts and len(owner.ov.batches) == before_cases
        document = _read(owner.repo.workspace(final) / "compiled_skill/refund-review/benchmark.json")
        assert len(document["cases"]) == 12 and final["candidate_quality"]["refund-review"]["can_submit"]

    asyncio.run(check())


@pytest.mark.parametrize("tamper", ["native_origin", "v2_context", "native_result"])
def test_native_file_map_cache_rejects_changed_accepted_origin_or_routing(tmp_path, tamper):
    async def check():
        owner, _, (job, entries) = await reviewed_batch_inputs(tmp_path)
        root, state = prepared_only(owner, job, entries)
        previous = root.parent.parent.parent / "rounds/round-1/input/staged"
        origin_path = next(path for path in (previous / "extract-batches/accepted").glob("*.json")
                           if _read(path)["unit_index"] == 0)
        origin = _read(origin_path)
        if tamper == "native_origin":
            origin["context"]["source_refs"] = ["unassigned-source"]
            _save(origin_path, origin)
        elif tamper == "v2_context":
            unit_path = previous / origin["unit_path"]
            unit = _read(unit_path)
            unit["value"]["context_refs"] = ["unassigned-context"]
            _save(unit_path, unit)
        else:
            with (previous / origin["result_file"]).open("a") as stream:
                stream.write("\nchanged native result\n")
        chunks, after = _raw_group(root, range(state["chunk_count"]), 0)
        context = {"after": after, "ordinals": [chunk["ordinal"] for chunk in chunks],
                   "input_ids": [chunk["id"] for chunk in chunks],
                   "source_refs": [_alias(chunk["id"]) for chunk in chunks]}
        with pytest.raises(ValueError):
            try_reuse_extract(job, root, state, chunks, context)
        assert state["unit_count"] == state["cursor"] == 0

    asyncio.run(check())


def test_same_raw_chunks_but_new_workflow_membership_invalidates_the_old_artifact(tmp_path):
    async def check():
        owner, _, (job, entries) = await reviewed_batch_inputs(tmp_path)
        root, state = prepared_only(owner, job, entries)
        candidate, scope = candidate_and_scope(root, "refund-review")
        assert scope["method_lineage_verified"] and len(scope["method_lineage_sha256"]) == 64
        assert _read(root / "reuse/manifest.json")["candidate_count"] == 1
        # Two methods can share one exact raw chunk. A newly merged branch changes lineage,
        # even when canonical name/business fields and every source ordinal stay the same.
        changed = {**scope, "method_lineage_sha256": "f" * 64}
        assert changed["ordinals"] == scope["ordinals"] and changed["method_ordinals"] == scope["method_ordinals"]
        assert not try_reuse_candidate(job, root, state, candidate, changed, owner.repo)
        assert not list((root / "cases").glob("*/*.json"))

    asyncio.run(check())


def test_unverified_precise_workflow_lineage_is_a_safe_miss(tmp_path):
    async def check():
        owner, _, (job, entries) = await reviewed_batch_inputs(tmp_path)
        root, state = prepared_only(owner, job, entries)
        candidate, scope = candidate_and_scope(root, "refund-review")
        changed = {**scope, "method_lineage_sha256": "", "method_lineage_verified": False}
        assert not try_reuse_candidate(job, root, state, candidate, changed, owner.repo)
        assert not list((root / "cases").glob("*/*.json"))
        miss = next((root / "reuse/misses").glob("refund-review-*.json"))
        assert "not completely verified" in _read(miss)["reason"]

    asyncio.run(check())


def test_workflow_protocol_upgrade_invalidates_candidates_and_frozen_protocol_mutation_is_rejected(tmp_path):
    async def check():
        owner = ReuseOwner(tmp_path)
        first = await reviewed_first(owner)
        job, entries = await next_inputs(owner, first)
        root, state = prepared_only(owner, job, entries)
        # A fresh round with new workflow semantics cannot reuse prior artifacts merely
        # because the writer renderer and generated canonical strings happen to agree.
        state["workflow_planning_protocol"] = "te-workflow-planning.v-next"
        prepare_round_reuse(job, root, state, entries)
        candidate, scope = candidate_and_scope(root)
        assert _read(root / "reuse/manifest.json")["candidate_count"] == 0
        assert not try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        state["workflow_planning_protocol"] = "unexpected-mutated-protocol"
        with pytest.raises(ValueError, match="protocol changed"):
            try_reuse_candidate(job, root, state, candidate, scope, owner.repo)
        assert not list((root / "cases").glob("*/*.json"))

    asyncio.run(check())
