"""Durable rounds, human Evidence supplements and immutable Compile snapshots."""

from __future__ import annotations

import json

import pytest

from team_miner.skill_miner.ov_compile_mining import MAX_PARALLEL_JOBS, REVIEW_SCHEMA_VERSION, OvSkillMiningRepository
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_capability_plan, build_document


def create_job(repository, *, rounds=3, human=True):
    job = repository.create_job(
        name="Evidence rounds",
        account="default",
        source_uri="viking://resources/team/source",
        source_snapshot_uri="viking://resources/test/round-1/source",
        output_uri="viking://resources/test/round-1/output",
        document_count=1,
        max_rounds=rounds,
        human_checkpoints=human,
    )
    return repository.update_job(
        job["job_id"], status="running", ov_task_id="round-1-task", started_at="2026-10-10T00:00:00Z"
    )


def question(source="viking://resources/team/source/policy.md"):
    return {
        "qid": "gap-1",
        "question": "适用阈值是否已确认？",
        "reason": "原文阈值存在冲突。",
        "source_refs": [source],
        "required": True,
    }


def populate(repository, job, *, questions=None, review=True):
    identity = job["job_id"]
    if int(job.get("candidate_contract_version") or 1) >= 2:
        repository.write_output(identity, "analysis/capability-plan.json", json.dumps(
            build_capability_plan(["policy-review"], f"{job['source_snapshot']}/policy.md")
        ))
    repository.write_output(identity, "analysis/evidence-map.md", "# Evidence\nSource policy.")
    repository.write_output(identity, "analysis/gaps.md", "# Gaps\nReview applicability.")
    if review:
        repository.write_output(
            identity,
            "analysis/review.json",
            json.dumps(
                {
                    "schema_version": REVIEW_SCHEMA_VERSION,
                    "questions": [question()] if questions is None else questions,
                }
            ),
        )
    repository.write_output(
        identity,
        "compiled_skill/policy-review/SKILL.md",
        "---\nname: policy-review\ndescription: Apply sourced rules.\n---\n# Policy",
    )
    repository.write_output(identity, "compiled_skill/policy-review/EVALUATION.md", "# Evaluation")
    repository.write_output(identity, "compiled_skill/policy-review/BENCHMARK.md", "# Dataset")
    document = build_document(
        "policy-review",
        [
            {
                "input": f"Scenario {i}",
                "requirements": [f"Condition {j}" for j in range(12)],
                "source_session_ids": [f"{job['source_snapshot']}/policy.md"],
            }
            for i in range(12)
        ],
    )
    repository.write_output(identity, "compiled_skill/policy-review/benchmark.json", json.dumps(document))


@pytest.fixture
def repository(tmp_path):
    return OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner", backend=MemoryMetadataBackend())


def complete_round(repository, job, **kwargs):
    populate(repository, job, **kwargs)
    repository.finalize_materialized(job["job_id"])
    return repository.finish_materialized_round(job["job_id"])


def test_human_round_waits_restarts_and_preserves_both_original_and_reviewed_outputs(repository):
    initial = create_job(repository)
    assert initial["staged_execution_protocol"] == "te-mining-optimized.v1"
    initial = repository.update_job(
        initial["job_id"],
        input_manifest="round_inputs/round-1/manifest.json",
        compile_request={"from": [initial["source_snapshot"]]},
    )
    waiting = complete_round(repository, initial)
    assert waiting["status"] == "waiting_human" and not waiting["finished_at"]
    assert not waiting["artifact_quality"]["can_submit"]
    restarted = OvSkillMiningRepository(repository.state_root, repository.project_root, backend=repository.backend)
    waiting = restarted.get_job(initial["job_id"])
    detail = restarted.public_job(waiting, detail=True)
    assert detail["pending_checkpoint"]["questions"][0]["source_refs"] == question()["source_refs"]
    assert len(detail["rounds"][0]["artifacts"]) == 8
    archive = repository.project_root / waiting["rounds_history"][0]["path"]
    manifest = json.loads((archive / "input.json").read_text())
    assert manifest["input_manifest"] == "round_inputs/round-1/manifest.json"
    assert manifest["previous_candidate_is_evidence"] is False
    skill = restarted.workspace(waiting) / "compiled_skill/policy-review/SKILL.md"
    skill.write_text(skill.read_text() + "\nHuman revision\n")
    continued = restarted.prepare_next_round(
        initial["job_id"],
        waiting["pending_checkpoint"]["id"],
        {"gap-1": "阈值为 12。"},
        [],
        {"gap-1": ["viking://resources/team/supplement/approved.md"]},
        "核对单位",
        "request-1",
        submitted_by="admin",
    )
    assert continued["status"] == "preparing" and continued["current_round"] == 2
    assert continued["staged_execution_protocol"] == initial["staged_execution_protocol"]
    assert not continued["materialized"] and not continued["ov_task_id"] and not continued["started_at"]
    assert continued["source_snapshot"].endswith("/rounds/round-2/source")
    assert continued["original_source_snapshot"] == initial["source_snapshot"]
    assert continued["submission_idempotency_key"].endswith("round-2")
    assert continued["supplements"][0]["submitted_by"] == "admin"
    assert continued["compile_request"] == {} and not continued["input_manifest"]
    assert not list(restarted.workspace(continued).rglob("*.md"))
    assert "Human revision" not in (archive / "output/compiled_skill/policy-review/SKILL.md").read_text()
    assert "Human revision" in (archive / "reviewed_output/compiled_skill/policy-review/SKILL.md").read_text()


def test_repeated_checkpoint_submission_does_not_create_extra_round(repository):
    waiting = complete_round(repository, create_job(repository))
    checkpoint = waiting["pending_checkpoint"]["id"]
    body = (waiting["job_id"], checkpoint, {"gap-1": "12"}, [], {})
    first = repository.prepare_next_round(*body, request_id="same-request")
    second = repository.prepare_next_round(*body, request_id="same-request")
    assert first == second
    with pytest.raises(ValueError, match="不一致"):
        repository.prepare_next_round(waiting["job_id"], checkpoint, {"gap-1": "13"}, [], {}, request_id="same-request")
    with pytest.raises(ValueError, match="当前没有"):
        repository.prepare_next_round(*body, request_id="another-request")


def test_last_round_requires_explicit_unknown_then_finishes_with_warning(repository):
    waiting = complete_round(repository, create_job(repository, rounds=1))
    assert waiting["status"] == "waiting_human"
    checkpoint = waiting["pending_checkpoint"]["id"]
    with pytest.raises(ValueError, match="必填"):
        repository.finish_job(waiting["job_id"], checkpoint)
    with pytest.raises(ValueError, match="上限"):
        repository.prepare_next_round(waiting["job_id"], checkpoint, {}, ["gap-1"], {})
    final = repository.finish_job(waiting["job_id"], checkpoint, unresolved=["gap-1"], request_id="finish")
    assert final["status"] == "succeeded" and final["finished_at"]
    assert final["artifact_quality"]["can_submit"] and final["artifact_quality"]["level"] == "caution"
    assert final["unresolved_questions"] == ["gap-1"]
    assert any("未解决" in warning for warning in final["artifact_quality"]["warnings"])
    assert repository.finish_job(waiting["job_id"], checkpoint, unresolved=["gap-1"], request_id="finish") == final


def test_waiting_does_not_hold_execution_capacity_and_requires_stop_before_delete(repository):
    waiting = complete_round(repository, create_job(repository))
    for _ in range(MAX_PARALLEL_JOBS):
        create_job(repository)
    with pytest.raises(RuntimeError, match="最多"):
        create_job(repository)
    with pytest.raises(RuntimeError, match="最多"):
        repository.prepare_next_round(waiting["job_id"], waiting["pending_checkpoint"]["id"], {}, ["gap-1"], {})
    assert repository.get_job(waiting["job_id"])["status"] == "waiting_human"
    with pytest.raises(RuntimeError, match="先停止"):
        repository.delete_job(waiting["job_id"])
    stopped = repository.stop_waiting(waiting["job_id"])
    assert stopped["status"] == "stopped"
    assert repository.delete_job(waiting["job_id"])["ok"]


@pytest.mark.parametrize("bad", ["missing", "qid", "source", "required", "text", "schema"])
def test_structured_review_contract_rejects_invalid_questions(repository, bad):
    job = create_job(repository)
    populate(repository, job, review=bad != "missing")
    if bad != "missing":
        review = {"schema_version": REVIEW_SCHEMA_VERSION, "questions": [question()]}
        if bad == "qid":
            review["questions"].append(question())
        elif bad == "source":
            review["questions"][0]["source_refs"] = ["viking://resources/unrelated/policy.md"]
        elif bad == "required":
            review["questions"][0]["required"] = "yes"
        elif bad == "text":
            review["questions"][0]["question"] = ""
        elif bad == "schema":
            review["schema_version"] = "anything"
        repository.write_output(job["job_id"], "analysis/review.json", json.dumps(review))
    with pytest.raises(ValueError):
        repository.finalize_materialized(job["job_id"])
    assert not repository.get_job(job["job_id"])["materialized"]


@pytest.mark.parametrize(
    "body",
    [
        ({"gap-1": 12}, [], {}),
        ({"unknown": "x"}, [], {}),
        ({}, ["unknown"], {}),
        ({"gap-1": "x"}, ["gap-1"], {}),
        ({"gap-1": "x" * 100001}, [], {}),
        ({"gap-1": "x"}, [], {"gap-1": ["viking://agent/skills/anything"]}),
    ],
)
def test_supplement_type_size_and_qid_validation(repository, body):
    waiting = complete_round(repository, create_job(repository))
    with pytest.raises(ValueError):
        repository.prepare_next_round(waiting["job_id"], waiting["pending_checkpoint"]["id"], *body)
    assert repository.get_job(waiting["job_id"])["status"] == "waiting_human"


def test_previous_candidate_never_accepted_as_evidence_reference(repository):
    waiting = complete_round(repository, create_job(repository))
    with pytest.raises(ValueError, match="不能作为 Evidence"):
        repository.prepare_next_round(
            waiting["job_id"],
            waiting["pending_checkpoint"]["id"],
            {"gap-1": "x"},
            [],
            {"gap-1": [f"{waiting['output_uri']}/compiled_skill/policy-review/SKILL.md"]},
        )


def test_empty_review_questions_still_waits_and_can_finish(repository):
    waiting = complete_round(repository, create_job(repository), questions=[])
    assert waiting["pending_checkpoint"]["questions"] == []
    final = repository.finish_job(waiting["job_id"], waiting["pending_checkpoint"]["id"])
    assert final["status"] == "succeeded" and final["artifact_quality"]["can_submit"]


def test_finish_with_unknown_answers_and_empty_reference_arrays_has_no_new_evidence_warning(repository):
    waiting = complete_round(repository, create_job(repository, rounds=1))
    final = repository.finish_job(
        waiting["job_id"],
        waiting["pending_checkpoint"]["id"],
        answers={},
        unresolved=["gap-1"],
        references={"gap-1": []},
        feedback="",
    )
    warnings = final["artifact_quality"]["warnings"]
    assert final["status"] == "succeeded" and final["artifact_quality"]["level"] == "caution"
    assert any("未解决的 Evidence 缺口" in warning for warning in warnings)
    assert not any("最终复核新增" in warning for warning in warnings)
    assert final["checkpoint_history"][0]["references"] == {"gap-1": []}


def test_automatic_rounds_are_real_compile_runs_and_keep_prior_source_citations(repository):
    job = create_job(repository, rounds=2, human=False)
    first = complete_round(repository, job, questions=[])
    assert first["status"] == "running" and first["materialized"]
    second = repository.prepare_automatic_next_round(first["job_id"])
    assert second["current_round"] == 2 and second["status"] == "preparing"
    repository.update_job(second["job_id"], status="running", ov_task_id="round-2-task")
    final = complete_round(repository, second, questions=[question(job["source_snapshot"] + "/policy.md")])
    assert final["status"] == "succeeded" and final["current_round"] == 2
    assert len(final["rounds_history"]) == 2
    assert final["artifact_quality"]["level"] == "caution"


def test_pending_attachments_persist_with_provenance_but_hide_server_paths(repository):
    waiting = complete_round(repository, create_job(repository))
    attachment = {
        "name": "approved.md",
        "uri": "viking://resources/supplements/approved.md",
        "local_path": "/private/server/path",
        "sha256": "abc",
        "content_bytes": 20,
    }
    waiting = repository.update_job(waiting["job_id"], pending_attachments=[attachment])
    public = repository.public_job(waiting, detail=True)
    assert "local_path" not in json.dumps(public)
    continued = repository.prepare_next_round(
        waiting["job_id"], waiting["pending_checkpoint"]["id"], {"gap-1": "12"}, [], {}
    )
    assert continued["supplements"][0]["attachments"] == [attachment]
    assert not continued["pending_attachments"]
    assert "/private/server/path" not in json.dumps(repository.public_job(continued, detail=True))


def test_interrupted_workspace_reset_is_completed_before_restart_submission(repository):
    waiting = complete_round(repository, create_job(repository), questions=[])
    repository.update_job(
        waiting["job_id"], status="preparing", current_round=2, materialized=False, workspace_reset_pending=True
    )
    restarted = OvSkillMiningRepository(repository.state_root, repository.project_root, backend=repository.backend)
    recovered = restarted.get_job(waiting["job_id"])
    assert recovered["workspace_reset_pending"] is False
    assert not list(restarted.workspace(recovered).rglob("*.md"))
    assert (repository._job_dir(waiting["job_id"]) / "rounds/round-1/output/semantic_reports/gaps.md").is_file()


def test_round_archive_freezes_complete_inputs_and_does_not_recapture_later_edits(repository):
    job = create_job(repository)
    inputs = repository._job_dir(job["job_id"]) / "round_inputs" / "round-1"
    inputs.mkdir(parents=True)
    (inputs / "source.md").write_text("Original Evidence")
    manifest = inputs / "manifest.json"
    manifest.write_text(json.dumps([{"relative_path": "source.md", "kind": "source"}]))
    job = repository.update_job(job["job_id"], input_manifest=str(manifest))
    waiting = complete_round(repository, job, questions=[])
    archived_inputs = repository._job_dir(job["job_id"]) / "rounds" / "round-1" / "input"
    assert (archived_inputs / "source.md").read_text() == "Original Evidence"
    (inputs / "source.md").write_text("Later change")
    repository.finish_materialized_round(waiting["job_id"])
    assert (archived_inputs / "source.md").read_text() == "Original Evidence"


def test_reference_snapshots_are_durable_private_and_do_not_change_submission_fingerprint(repository):
    waiting = complete_round(repository, create_job(repository))
    checkpoint = waiting["pending_checkpoint"]["id"]
    refs = {"gap-1": ["viking://resources/team/approved.md"]}
    initial_validation = repository._validate_submission(
        waiting,
        checkpoint,
        {"gap-1": "12"},
        [],
        refs,
        "",
        "retry",
        "admin",
        None,
    )
    snapshots = [{"uri": refs["gap-1"][0], "local_path": "/private/reference.md", "sha256": "frozen"}]
    waiting = repository.update_job(
        waiting["job_id"],
        pending_reference_snapshots=snapshots,
        round_started_at="2026-10-10T00:00:00Z",
        submission_error="old submission failure",
    )
    next_job = repository.prepare_next_round(
        waiting["job_id"],
        checkpoint,
        {"gap-1": "12"},
        [],
        refs,
        request_id="retry",
        submitted_by="admin",
    )
    submission = next_job["supplements"][0]
    assert submission["reference_snapshots"] == snapshots
    assert submission["fingerprint"] == initial_validation["fingerprint"]
    archived = repository._job_dir(waiting["job_id"]) / "rounds/round-1/checkpoint_submission.json"
    assert json.loads(archived.read_text())["reference_snapshots"] == snapshots
    assert not next_job["pending_reference_snapshots"] and not next_job["submission_error"]
    assert not next_job["round_started_at"]
    assert "/private/reference.md" not in json.dumps(repository.public_job(next_job, detail=True))
    assert (
        repository.prepare_next_round(
            waiting["job_id"],
            checkpoint,
            {"gap-1": "12"},
            [],
            refs,
            request_id="retry",
            submitted_by="admin",
        )["current_round"]
        == 2
    )


def test_remote_partial_output_warning_survives_validation_archive_and_final_review(repository):
    job = create_job(repository, rounds=2)
    warning = "partial_output: source extraction omitted one document"
    inputs = repository._job_dir(job["job_id"]) / "round_inputs" / "round-1"
    inputs.mkdir(parents=True)
    (inputs / "compiler-source.md").write_text("Frozen compiler instructions")
    manifest = inputs / "manifest.json"
    manifest.write_text("[]")
    job = repository.update_job(
        job["job_id"],
        remote_warnings=[warning],
        compiler_sha256="compiler-hash",
        compiler_source_sha256="source-hash",
        compiler_uri="viking://agent/skills/compiler-content-pin",
        skill_uri="viking://agent/skills/compiler-content-pin",
        compiler_path="/private/compiler-source.md",
        input_manifest=str(manifest),
    )
    waiting = complete_round(repository, job, questions=[])
    assert waiting["status"] == "waiting_human" and waiting["materialized"]
    assert waiting["artifact_quality"]["level"] == "caution"
    assert waiting["artifact_quality"]["warnings"] == [warning]
    public = repository.public_job(waiting, detail=True)
    assert "/private/compiler-source.md" not in json.dumps(public)
    assert public["rounds"][0]["remote_warnings"] == [warning]
    assert public["rounds"][0]["compiler_sha256"] == "compiler-hash"
    assert public["rounds"][0]["compiler_source_sha256"] == "source-hash"
    assert public["rounds"][0]["compiler_uri"] == "viking://agent/skills/compiler-content-pin"
    assert public["rounds"][0]["skill_uri"] == "viking://agent/skills/compiler-content-pin"
    archive = repository._job_dir(job["job_id"]) / "rounds" / "round-1"
    assert json.loads((archive / "meta.json").read_text())["remote_warnings"] == [warning]
    assert (archive / "input/compiler-source.md").read_text() == "Frozen compiler instructions"
    next_job = repository.prepare_next_round(waiting["job_id"], waiting["pending_checkpoint"]["id"], {}, [], {})
    assert not next_job["remote_warnings"] and not next_job["compiler_sha256"] and not next_job["compiler_path"]
    assert not next_job["compiler_source_sha256"] and not next_job["compiler_uri"]
    next_job = repository.update_job(next_job["job_id"], remote_warnings=[warning], status="running")
    last = complete_round(repository, next_job, questions=[])
    final = repository.finish_job(last["job_id"], last["pending_checkpoint"]["id"])
    assert final["artifact_quality"]["level"] == "caution"
    assert final["artifact_quality"]["warnings"] == [warning]
