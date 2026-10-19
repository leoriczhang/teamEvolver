"""An empty supplement form advances real rounds without inventing human approval or Evidence."""

import asyncio
import json

import pytest

from team_miner.bridge import SkillMinerBridgeMixin
from team_miner.ov_compile_mining import OvSkillMiningRepository
from team_miner.tests.test_ov_compile_round_repository import complete_round, create_job, populate, question


@pytest.fixture
def repository(tmp_path):
    return OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")


def fresh_job(repository, *, rounds=2):
    job = create_job(repository, rounds=rounds)
    return repository.update_job(job["job_id"], empty_review_policy="te-empty-review-skip.v1")


def test_two_empty_rounds_advance_and_finish_without_fake_review(repository):
    first = complete_round(repository, fresh_job(repository), questions=[])
    assert first["status"] == "running" and first["pending_checkpoint"] is None
    assert first["skipped_checkpoints"][0]["human_approved"] is False
    assert repository.finish_materialized_round(first["job_id"]) == first
    reopened = OvSkillMiningRepository(repository.state_root, repository.project_root)
    second = reopened.prepare_automatic_next_round(first["job_id"])
    assert second["current_round"] == 2 and second["status"] == "preparing"
    assert second["checkpoint_history"] == second["supplements"] == []
    assert not second["last_checkpoint_submission"]
    archive = reopened._job_dir(second["job_id"]) / "rounds/round-1"
    assert (archive / "output/compiled_skill/policy-review/SKILL.md").is_file()
    assert not (archive / "checkpoint_submission.json").exists()
    reopened.update_job(second["job_id"], status="running", started_at="2026-10-10T01:00:00Z")
    final = complete_round(reopened, second, questions=[])
    assert final["status"] == "succeeded" and final["current_round"] == 2
    assert [row["round"] for row in final["skipped_checkpoints"]] == [1, 2]
    assert len(final["rounds_history"]) == 2
    assert final["checkpoint_history"] == final["supplements"] == []
    assert final["artifact_quality"]["can_submit"]


@pytest.mark.parametrize("required", [True, False])
def test_any_real_question_still_waits_including_optional(repository, required):
    job = fresh_job(repository)
    item = {**question(), "required": required}
    final = complete_round(repository, job, questions=[item])
    assert final["status"] == "waiting_human" and final["pending_checkpoint"]["questions"]
    assert not final["skipped_checkpoints"]
    with pytest.raises(ValueError, match="不能自动"):
        repository.prepare_automatic_next_round(job["job_id"])


def test_new_question_after_empty_receipt_blocks_automatic_continuation(repository):
    first = complete_round(repository, fresh_job(repository), questions=[])
    populate(repository, first, questions=[question()])
    with pytest.raises(ValueError, match="有补证问题"):
        repository.prepare_automatic_next_round(first["job_id"])
    assert repository.get_job(first["job_id"])["current_round"] == 1


def test_missing_or_invalid_review_cannot_be_treated_as_zero_questions(repository):
    job = fresh_job(repository, rounds=1)
    populate(repository, job, review=False)
    with pytest.raises(ValueError, match="review"):
        repository.finalize_materialized(job["job_id"])
    path = repository.workspace(job) / "semantic_reports/review.json"
    path.write_text(json.dumps({"schema_version": "skill-miner.review.v1", "questions": None}))
    with pytest.raises(ValueError, match="questions"):
        repository.finish_materialized_round(repository.update_job(job["job_id"], materialized=True)["job_id"])


def test_skip_preserves_unknown_history_without_asserting_resolution(repository):
    job = fresh_job(repository, rounds=1)
    repository.update_job(job["job_id"], checkpoint_history=[{"round": 0, "unresolved": ["unknown-fact"]}],
                          unresolved_questions=["unknown-fact"])
    result = complete_round(repository, job, questions=[])
    assert result["status"] == "succeeded" and result["unresolved_questions"] == ["unknown-fact"]
    assert result["checkpoint_history"][0]["unresolved"] == ["unknown-fact"]
    assert result["skipped_checkpoints"][0]["new_evidence"] is False


def test_bridge_schedules_next_round_only_after_valid_empty_review(repository):
    class Owner:
        def __init__(self):
            self.scheduled = []

        def _ov_skill_mining_repository(self):
            return repository

        def _schedule_ov_skill_round_submission(self, job):
            self.scheduled.append(job)

    job = fresh_job(repository)
    populate(repository, job, questions=[])
    repository.finalize_materialized(job["job_id"])
    owner = Owner()
    next_job = asyncio.run(SkillMinerBridgeMixin._complete_ov_skill_round(owner, job))
    assert next_job["current_round"] == 2 and len(owner.scheduled) == 1
    populate(repository, next_job, questions=[question()])
    repository.finalize_materialized(next_job["job_id"])
    waiting = asyncio.run(SkillMinerBridgeMixin._complete_ov_skill_round(owner, next_job))
    assert waiting["status"] == "waiting_human" and len(owner.scheduled) == 1


@pytest.mark.asyncio
async def test_body_and_cases_complete_two_empty_reviews_using_real_host_transport(tmp_path):
    from team_miner.ov_compile_mining import SKILL_BODY
    from team_miner.ov_round_orchestration import OvRoundOrchestrationMixin
    from team_miner.ov_staged_orchestration import _read, _save
    from team_miner.tests.test_body_patch_pipeline import PatchOwner
    from team_miner.tests.test_ov_staged_orchestration import start
    from team_miner.tests.test_round_reuse import POLICY

    class Owner(PatchOwner):
        def new(self, count=1):
            job, entries = super().new(count)
            manifest = self.repo._job_dir(job["job_id"]) / "round_inputs/round-1/manifest.json"
            _save(manifest, entries)
            return self.repo.update_job(job["job_id"], input_manifest=str(manifest), max_rounds=2,
                                        empty_review_policy="te-empty-review-skip.v1"), entries

    async def complete(owner, job, number):
        for _ in range(200):
            if len(job["rounds_history"]) == number or job["status"] in {"failed", "stopped", "waiting_human"}:
                return job
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
        raise AssertionError("Finite staged run did not complete")

    owner = Owner(tmp_path)
    first = await complete(owner, await start(owner), 1)
    assert first["status"] == "running" and first["pending_checkpoint"] is None, first.get("error")
    second = owner.repo.prepare_automatic_next_round(first["job_id"])
    entries = await OvRoundOrchestrationMixin._freeze_round_inputs(owner, second, staged=True)
    second = owner.repo.update_job(second["job_id"], input_manifest=str(
        owner.repo._job_dir(second["job_id"]) / "round_inputs/round-2/manifest.json"))
    owner.ov.directories.add(second["source_snapshot"].rsplit("/", 1)[0])
    result = await complete(owner, await owner._prepare_staged_round(second, SKILL_BODY, entries, POLICY, ""), 2)
    assert result["status"] == "succeeded", result.get("error")
    assert len(result["skipped_checkpoints"]) == 2 and not result["checkpoint_history"]
    for number in (1, 2):
        folder = owner.repo._job_dir(result["job_id"]) / f"rounds/round-{number}/output/compiled_skill/refund-review"
        assert len(_read(folder / "benchmark.json")["cases"]) == 12
        text = (folder / "SKILL.md").read_text()
        assert "另加40分钟必须再次征得客户同意" in text
        assert "退休后费用不会立刻下降" in text
        assert "账户称谓未映射，保持待核实" in text
    _, state = await owner._staged_load(result)
    assert not state.get("round_reuse_protocol") or not _read(
        owner._staged_root(result) / "reuse/manifest.json")["enabled"]
