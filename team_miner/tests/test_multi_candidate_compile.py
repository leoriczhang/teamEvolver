"""Multiple independent candidates, empty results and safe legacy hand-off."""

import hashlib
import json

import pytest

from team_miner.artifacts import save_artifact
from team_miner.dataset_view import dataset_markdown
from team_miner.lifecycle import (
    MiningLifecycleError,
    mined_artifact_descriptor,
    resolve_mined_job_skill_root,
    submit_mined_skill,
)
from team_miner.ov_compile_mining import OvSkillMiningRepository
from team_miner.tests.dataset_fixtures import build_capability_plan, build_document
from team_skills.candidates.store import ValidationStore
from team_skills.library.bundle import bundle_tree_sha256, read_skill_bundle
from teamEvolver.storage import LocalObjectStore


@pytest.fixture
def rig(tmp_path):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")
    job = repository.create_job(
        name="Independent tasks",
        account="default",
        source_uri="viking://resources/team/source",
        source_snapshot_uri="viking://resources/test/source",
        output_uri="viking://resources/test/output",
        document_count=5,
        max_rounds=1,
        human_checkpoints=False,
    )
    repository.update_job(job["job_id"], status="running")
    for name in ("evidence-map.md", "gaps.md"):
        repository.write_output(job["job_id"], "analysis/" + name, "# Grounded analysis")
    repository.write_output(
        job["job_id"],
        "analysis/review.json",
        json.dumps(
            {
                "schema_version": "skill-miner.review.v1",
                "questions": [],
            }
        ),
    )
    return repository, job


def populate(repository, job, names):
    plan = build_capability_plan(names, job["source_uri"] + "/policy.md")
    repository.write_output(job["job_id"], "analysis/capability-plan.json", json.dumps(plan))
    for name in names:
        root = f"compiled_skill/{name}/"
        repository.write_output(
            job["job_id"], root + "SKILL.md", f"---\nname: {name}\ndescription: Execute {name}\n---\n# Task"
        )
        repository.write_output(job["job_id"], root + "EVALUATION.md", "# Task-specific validation")
        repository.write_output(job["job_id"], root + "BENCHMARK.md", "# Task-specific Test Dataset")
        dataset = build_document(
            name,
            [
                {"input": f"{name} task {i}", "source_session_ids": [job["source_uri"] + "/policy.md"]}
                for i in range(12)
            ],
        )
        repository.write_output(job["job_id"], root + "benchmark.json", json.dumps(dataset))
    return plan


def test_two_candidates_have_independent_datasets_and_idempotent_review_jobs(rig, tmp_path):
    repository, job = rig
    plan = populate(repository, job, ["supplier-admission", "contract-review"])
    result = repository.finalize_materialized(job["job_id"])
    assert result["candidate_count"] == 2 and result["valid_candidate_count"] == 2
    assert result["capability_plan"] == plan
    result = repository.finish_materialized_round(job["job_id"])
    assert result["status"] == "succeeded" and result["artifact_quality"]["can_submit"]
    store = ValidationStore.from_bucket(bucket=LocalObjectStore(tmp_path / "validation"))
    candidates = []
    for name in ["supplier-admission", "contract-review"]:
        root = resolve_mined_job_skill_root(job["job_id"], name, skillminer_root=repository.project_root)
        first = submit_mined_skill(store, name, skillminer_root=root, mining_job_id=job["job_id"])
        repeat = submit_mined_skill(store, name, skillminer_root=root, mining_job_id=job["job_id"])
        assert first["created"] and not repeat["created"]
        assert all(case["skill_ids"] == [name] for case in first["job"]["replay_cases"])
        candidates.append(first["job"]["job_id"])
    assert len(set(candidates)) == 2


def test_incomplete_sibling_does_not_block_valid_candidate(rig):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    (repository.workspace(job) / "compiled_skill/contract-review/benchmark.json").unlink()
    result = repository.finalize_materialized(job["job_id"])
    assert result["valid_candidate_count"] == 1
    assert result["candidate_quality"]["supplier-admission"]["can_submit"]
    assert not result["candidate_quality"]["contract-review"]["can_submit"]
    repository.finish_materialized_round(job["job_id"])
    resolve_mined_job_skill_root(job["job_id"], "supplier-admission", skillminer_root=repository.project_root)
    with pytest.raises(MiningLifecycleError, match="benchmark.json"):
        resolve_mined_job_skill_root(job["job_id"], "contract-review", skillminer_root=repository.project_root)


def test_zero_candidates_is_complete_but_never_submittable(rig):
    repository, job = rig
    populate(repository, job, [])
    repository.finalize_materialized(job["job_id"])
    result = repository.finish_materialized_round(job["job_id"])
    assert result["status"] == "succeeded" and result["candidate_count"] == 0
    assert not result["artifact_quality"]["can_submit"]
    assert not result["artifact_quality"]["artifacts"]
    assert result["capability_plan"]["deferred"]
    with pytest.raises(MiningLifecycleError):
        resolve_mined_job_skill_root(job["job_id"], "invented", skillminer_root=repository.project_root)


@pytest.mark.parametrize("failure", ["duplicate", "outside-source", "missing-input", "extra-output", "count-limit"])
def test_capability_plan_rejects_ambiguous_or_unproven_boundaries(rig, failure):
    repository, job = rig
    plan = populate(repository, job, ["supplier-admission", "contract-review"])
    if failure == "duplicate":
        plan["skills"][1]["name"] = plan["skills"][0]["name"]
    elif failure == "outside-source":
        plan["skills"][0]["source_refs"] = ["viking://resources/other/policy.md"]
    elif failure == "missing-input":
        plan["skills"][0]["inputs"] = []
    elif failure == "extra-output":
        plan["skills"].pop()
    else:
        plan["skills"] *= 5
    repository.write_output(job["job_id"], "analysis/capability-plan.json", json.dumps(plan))
    with pytest.raises(ValueError):
        repository.finalize_materialized(job["job_id"])
    assert not repository.get_job(job["job_id"])["materialized"]


@pytest.mark.parametrize("staged", [False, True])
def test_aggregate_candidate_count_can_exceed_a_single_compile_assignment(rig, staged):
    repository, job = rig
    names = [f"independent-task-{number}" for number in range(9)]
    populate(repository, job, names)
    if staged:
        repository.update_job(job["job_id"], staged_protocol="te-staged-compile.v1")
        result = repository.finalize_materialized(job["job_id"])
        assert result["candidate_count"] == result["valid_candidate_count"] == 9
        assert {row["name"] for row in result["capability_plan"]["skills"]} == set(names)
    else:
        with pytest.raises(ValueError, match="单次 Compile"):
            repository.finalize_materialized(job["job_id"])


def test_staged_facts_only_plan_does_not_require_a_fabricated_evidence_gap(rig):
    repository, job = rig
    repository.update_job(job["job_id"], staged_protocol="te-staged-compile.v1")
    repository.write_output(
        job["job_id"], "analysis/capability-plan.json",
        json.dumps({"schema_version": "skill-miner.plan.v1", "skills": [], "deferred": []}),
    )
    result = repository.finalize_materialized(job["job_id"])
    assert result["materialized"] and result["candidate_count"] == 0
    assert not result["artifact_quality"]["can_submit"]
    assert result["capability_plan"]["deferred"] == []


def test_legacy_single_candidate_without_plan_keeps_old_contract(rig):
    repository, job = rig
    populate(repository, job, ["supplier-admission"])
    repository.update_job(job["job_id"], candidate_contract_version=1)
    (repository.workspace(job) / "semantic_reports/capability-plan.json").unlink()
    assert repository.finalize_materialized(job["job_id"])["materialized"]
    populate(repository, job, ["supplier-admission", "contract-review"])
    with pytest.raises(ValueError, match="一个 Skill Candidate"):
        repository.finalize_materialized(job["job_id"])


def test_submission_revalidates_edited_dataset_without_blocking_other_candidates(rig):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    repository.finalize_materialized(job["job_id"])
    repository.finish_materialized_round(job["job_id"])
    path = repository.workspace(job) / "compiled_skill/contract-review/benchmark.json"
    document = json.loads(path.read_text())
    document["cases"][0]["provenance"]["session_ids"] = ["viking://resources/other/invented.md"]
    path.write_text(json.dumps(document))
    with pytest.raises(MiningLifecycleError, match="引用不属于"):
        resolve_mined_job_skill_root(job["job_id"], "contract-review", skillminer_root=repository.project_root)
    resolve_mined_job_skill_root(job["job_id"], "supplier-admission", skillminer_root=repository.project_root)


def test_missing_sibling_skill_file_keeps_valid_candidate_submittable(rig):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    (repository.workspace(job) / "compiled_skill/contract-review/SKILL.md").unlink()
    result = repository.finalize_materialized(job["job_id"])
    assert result["candidate_count"] == 2 and result["valid_candidate_count"] == 1
    assert not result["candidate_quality"]["contract-review"]["has_skill"]
    repository.finish_materialized_round(job["job_id"])
    resolve_mined_job_skill_root(job["job_id"], "supplier-admission", skillminer_root=repository.project_root)


@pytest.mark.parametrize("field", ["skills", "case_skill_ids"])
def test_independent_dataset_rejects_sibling_skill_ids(rig, field):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    path = repository.workspace(job) / "compiled_skill/contract-review/benchmark.json"
    dataset = json.loads(path.read_text())
    if field == "skills":
        dataset["skills"].append({"skill_id": "supplier-admission"})
    else:
        dataset["cases"][0]["skill_ids"] = ["supplier-admission"]
    path.write_text(json.dumps(dataset))
    result = repository.finalize_materialized(job["job_id"])
    assert result["valid_candidate_count"] == 1
    assert "Skill" in result["candidate_quality"]["contract-review"]["warnings"][0]


def test_plan_rejects_source_uri_traversal(rig):
    repository, job = rig
    plan = populate(repository, job, ["contract-review"])
    plan["skills"][0]["source_refs"] = [job["source_uri"] + "/../outside.md"]
    repository.write_output(job["job_id"], "analysis/capability-plan.json", json.dumps(plan))
    with pytest.raises(ValueError, match="目录路径无效"):
        repository.finalize_materialized(job["job_id"])


def test_human_dataset_view_is_derived_from_validated_cases_and_checked_after_edits(rig):
    repository, job = rig
    populate(repository, job, ["contract-review"])
    repository.finalize_materialized(job["job_id"])
    root = repository.workspace(job) / "compiled_skill/contract-review"
    dataset = json.loads((root / "benchmark.json").read_text())
    view = (root / "BENCHMARK.md").read_text()
    for case in dataset["cases"]:
        assert case["case_id"] in view and case["query"] in view
        assert all(check["text"] in view for check in case["checks"])
    repository.finish_materialized_round(job["job_id"])
    (root / "BENCHMARK.md").write_text("# Unrelated view")
    with pytest.raises(MiningLifecycleError, match="不一致"):
        resolve_mined_job_skill_root(job["job_id"], "contract-review", skillminer_root=repository.project_root)


def test_edit_refresh_disables_invalid_candidate_and_allows_repaired_candidate(rig):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    repository.finalize_materialized(job["job_id"])
    repository.finish_materialized_round(job["job_id"])
    root = repository.workspace(job) / "compiled_skill/contract-review"
    original = (root / "EVALUATION.md").read_text()
    (root / "EVALUATION.md").write_text("")
    result = repository.refresh_edited_quality(job["job_id"], "compiled_skill/contract-review/EVALUATION.md")
    assert result["status"] == "succeeded" and result["artifact_quality"]["can_submit"]
    assert not result["candidate_quality"]["contract-review"]["can_submit"]
    (root / "EVALUATION.md").write_text(original)
    result = repository.refresh_edited_quality(job["job_id"], "compiled_skill/contract-review/EVALUATION.md")
    assert result["candidate_quality"]["contract-review"]["can_submit"]
    assert not any(warning.startswith("contract-review：") for warning in result["artifact_quality"]["warnings"])


def test_edit_refresh_cannot_bypass_waiting_human_gate_and_keeps_review_warnings(rig):
    repository, job = rig
    populate(repository, job, ["contract-review"])
    repository.update_job(job["job_id"], human_checkpoints_effective=True)
    repository.finalize_materialized(job["job_id"])
    waiting = repository.finish_materialized_round(job["job_id"])
    quality = {**waiting["artifact_quality"], "warnings": ["Human uncertainty remains"]}
    repository.update_job(job["job_id"], artifact_quality=quality)
    result = repository.refresh_edited_quality(job["job_id"], "compiled_skill/contract-review/SKILL.md")
    assert result["status"] == "waiting_human" and not result["artifact_quality"]["can_submit"]
    assert "Human uncertainty remains" in result["artifact_quality"]["warnings"]


def test_edit_refresh_handles_all_invalid_candidates_then_recovers(rig):
    repository, job = rig
    populate(repository, job, ["contract-review"])
    repository.finalize_materialized(job["job_id"])
    repository.finish_materialized_round(job["job_id"])
    root = repository.workspace(job) / "compiled_skill/contract-review"
    original = (root / "benchmark.json").read_text()
    (root / "benchmark.json").write_text("broken JSON")
    result = repository.refresh_edited_quality(job["job_id"], "compiled_skill/contract-review/benchmark.json")
    assert result["status"] == "succeeded" and not result["artifact_quality"]["can_submit"]
    assert not result["materialized"]
    (root / "benchmark.json").write_text(original)
    result = repository.refresh_edited_quality(job["job_id"], "compiled_skill/contract-review/benchmark.json")
    assert result["materialized"] and result["artifact_quality"]["can_submit"]


@pytest.mark.parametrize("edited_file", ["SKILL.md", "EVALUATION.md", "benchmark.json"])
def test_repairing_initial_candidate_renders_dataset_view_and_pins_reviewed_bundle(rig, tmp_path, edited_file):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    repository.update_job(job["job_id"], human_checkpoints_effective=True)
    workspace = repository.workspace(job)
    root = workspace / "compiled_skill/contract-review"
    target = root / edited_file
    repaired_content = target.read_text()
    if edited_file == "SKILL.md":
        target.write_text(repaired_content.replace(
            "description: Execute contract-review", "description: Execute: review"
        ))
        repaired_content = repaired_content.replace(
            "description: Execute contract-review", 'description: "Execute: review"'
        ) + "\nQA editor note; this is not Evidence.\n"
    else:
        target.write_text("" if edited_file == "EVALUATION.md" else "broken JSON")
    dataset_bytes = (root / "benchmark.json").read_bytes()
    if edited_file == "benchmark.json":
        dataset_bytes = repaired_content.encode()
    raw_view = (root / "BENCHMARK.md").read_bytes()
    initial = repository.finalize_materialized(job["job_id"])
    assert initial["valid_candidate_count"] == 1
    assert not initial["candidate_quality"]["contract-review"]["can_submit"]
    if edited_file == "SKILL.md":
        assert "YAML" in initial["candidate_quality"]["contract-review"]["summary"]
        assert "name 必须与目录名一致" not in initial["candidate_quality"]["contract-review"]["summary"]
    assert (root / "BENCHMARK.md").read_bytes() == raw_view
    sibling_view = (workspace / "compiled_skill/supplier-admission/BENCHMARK.md").read_bytes()
    waiting = repository.finish_materialized_round(job["job_id"])
    archive = repository.project_root / waiting["rounds_history"][0]["path"]
    original_metadata = (archive / "meta.json").read_bytes()
    original_hashes = json.loads(original_metadata)["output_sha256"]
    view_path = "compiled_skill/contract-review/BENCHMARK.md"
    assert original_hashes[view_path] == hashlib.sha256(raw_view).hexdigest()

    relative = target.relative_to(repository.project_root).as_posix()
    assert save_artifact(repository.project_root, relative, repaired_content)["edited"]
    refreshed = repository.refresh_edited_quality(job["job_id"], relative)
    assert refreshed["valid_candidate_count"] == 2
    assert refreshed["candidate_quality"]["contract-review"]["can_submit"]
    assert refreshed["status"] == "waiting_human" and not refreshed["artifact_quality"]["can_submit"]
    assert (root / "benchmark.json").read_bytes() == dataset_bytes
    document = json.loads(dataset_bytes)
    canonical_view = dataset_markdown(document)
    assert (root / "BENCHMARK.md").read_text() == canonical_view
    assert (workspace / "compiled_skill/supplier-admission/BENCHMARK.md").read_bytes() == sibling_view
    assert (archive / "meta.json").read_bytes() == original_metadata
    assert (archive / "output" / view_path).read_bytes() == raw_view

    finished = repository.finish_job(
        job["job_id"], waiting["pending_checkpoint"]["id"], request_id="repair-review", submitted_by="admin"
    )
    assert finished["status"] == "succeeded" and finished["artifact_quality"]["can_submit"]
    reviewed_root = archive / "reviewed_output/compiled_skill/contract-review"
    reviewed_bundle = read_skill_bundle(reviewed_root)
    assert reviewed_bundle["BENCHMARK.md"] == canonical_view.encode()
    assert reviewed_bundle["benchmark.json"] == dataset_bytes
    assert bundle_tree_sha256(reviewed_bundle) == bundle_tree_sha256(read_skill_bundle(root))
    skill_workspace = resolve_mined_job_skill_root(
        job["job_id"], "contract-review", skillminer_root=repository.project_root
    )
    descriptor = mined_artifact_descriptor("contract-review", skillminer_root=skill_workspace)
    store = ValidationStore.from_bucket(bucket=LocalObjectStore(tmp_path / "review"))
    submitted = submit_mined_skill(
        store, "contract-review", skillminer_root=skill_workspace, mining_job_id=job["job_id"]
    )
    assert submitted["job"]["source"]["artifact_sha256"] == descriptor["artifact_sha256"]
    assert descriptor["artifact_sha256"] == bundle_tree_sha256(reviewed_bundle)
    assert [case["case_id"] for case in submitted["job"]["replay_cases"]] == [
        case["case_id"] for case in document["cases"]
    ]


def test_direct_dataset_view_edit_is_checked_without_overwriting_the_user_edit(rig):
    repository, job = rig
    populate(repository, job, ["contract-review"])
    repository.finalize_materialized(job["job_id"])
    repository.finish_materialized_round(job["job_id"])
    root = repository.workspace(job) / "compiled_skill/contract-review"
    dataset = (root / "benchmark.json").read_bytes()
    relative = (root / "BENCHMARK.md").relative_to(repository.project_root).as_posix()
    save_artifact(repository.project_root, relative, "# A different case set\n")
    refreshed = repository.refresh_edited_quality(job["job_id"], relative)
    assert not refreshed["candidate_quality"]["contract-review"]["can_submit"]
    assert "不一致" in refreshed["candidate_quality"]["contract-review"]["summary"]
    assert (root / "BENCHMARK.md").read_text() == "# A different case set\n"
    assert (root / "benchmark.json").read_bytes() == dataset


@pytest.mark.parametrize("missing", [True, False], ids=["missing", "empty"])
def test_skill_edit_does_not_create_missing_or_empty_required_dataset_view(rig, missing):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    root = repository.workspace(job) / "compiled_skill/contract-review"
    view = root / "BENCHMARK.md"
    if missing:
        view.unlink()
    else:
        view.write_text("")
    repository.finalize_materialized(job["job_id"])
    repository.finish_materialized_round(job["job_id"])
    relative = (root / "SKILL.md").relative_to(repository.project_root).as_posix()
    save_artifact(repository.project_root, relative, (root / "SKILL.md").read_text() + "\nHuman clarification.\n")
    refreshed = repository.refresh_edited_quality(job["job_id"], relative)
    assert not refreshed["candidate_quality"]["contract-review"]["can_submit"]
    assert "禁止自动补齐" in refreshed["candidate_quality"]["contract-review"]["summary"]
    if missing:
        assert not view.exists()
    else:
        assert view.read_bytes() == b""


def test_valid_frontmatter_with_wrong_name_still_reports_the_identity_mismatch(rig):
    repository, job = rig
    populate(repository, job, ["supplier-admission", "contract-review"])
    skill = repository.workspace(job) / "compiled_skill/contract-review/SKILL.md"
    skill.write_text(skill.read_text().replace("name: contract-review", "name: another-review"))
    result = repository.finalize_materialized(job["job_id"])
    warning = result["candidate_quality"]["contract-review"]["summary"]
    assert "name 必须与目录名一致" in warning and "YAML" not in warning
