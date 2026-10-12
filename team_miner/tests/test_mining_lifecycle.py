from __future__ import annotations

import copy
import json
from pathlib import Path

import pytest

from team_miner import benchmark_format as progressive_benchmark
from team_miner.lifecycle import (
    INTERNAL_BENCHMARK_FORMAT,
    MINED_REPLAY_BINDING_VERSION,
    MiningLifecycleError,
    benchmark_replay_cases,
    list_mined_skill_statuses,
    resolve_mined_job_skill_root,
    resolve_mined_job_workspace,
    resolve_mined_skill_dir,
    submit_mined_skill,
)
from team_miner.tests.dataset_fixtures import build_document
from team_skills.candidates.store import ValidationStore
from teamEvolver.storage import InMemoryObjectStore


def _store(root: Path) -> ValidationStore:
    return ValidationStore.from_bucket(bucket=InMemoryObjectStore(str(root / "objects")))


def _write_mined_skill(root: Path, name: str = "demo-skill") -> Path:
    skill_dir = root / "compiled_skill" / name
    skill_dir.mkdir(parents=True)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        f"name: {name}\n"
        "description: Demo mined skill\n"
        "category: support\n"
        "---\n\n"
        "# Procedure\n\nFollow the verified workflow.\n",
        encoding="utf-8",
    )
    (skill_dir / "EVALUATION.md").write_text("# Evaluation\n", encoding="utf-8")
    question = {
        "id": "BM-01",
        "target_dimensions": ["accuracy"],
        "difficulty": "medium",
        "input": "Handle this support case.",
        "source_session_ids": ["viking://resources/team/manual.md"],
        "gold": {
            "expected_label": {"result": "accepted"},
            "must_hit": ["cite the verified rule"],
            "must_avoid": ["invent evidence"],
        },
    }
    document = build_document(name, [question])
    progressive_benchmark.write_document(skill_dir / "benchmark.json", document)
    (skill_dir / "BENCHMARK.md").write_text("# Benchmark\n", encoding="utf-8")
    return skill_dir


def test_submit_mined_skill_creates_internal_candidate_and_is_idempotent(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    _write_mined_skill(miner_root)
    store = _store(tmp_path)

    first = submit_mined_skill(
        store,
        "demo-skill",
        submitted_by="admin",
        skillminer_root=miner_root,
    )
    second = submit_mined_skill(
        store,
        "demo-skill",
        submitted_by="admin",
        skillminer_root=miner_root,
    )

    assert first["created"] is True
    assert second["created"] is False
    assert second["job"]["job_id"] == first["job"]["job_id"]
    job = store.load_job(first["job"]["job_id"])
    assert job is not None
    assert job["source"]["kind"] == "skillminer"
    assert job["source"]["dataset_format"] == INTERNAL_BENCHMARK_FORMAT
    assert job["source"]["question_count"] == 1
    document = json.loads((miner_root / "compiled_skill/demo-skill/benchmark.json").read_text())
    expected_cases = copy.deepcopy(document["cases"])
    for case in expected_cases:
        case["replay"]["execution"] = {
            "kind": "document", "runtime_type": "document", "binding": "tenant_replay_adapter",
        }
    assert job["replay_cases"] == expected_cases
    assert job["source"]["replay_binding_version"] == MINED_REPLAY_BINDING_VERSION
    assert job["runtime_validation_policy"]["required_runtimes"] == ["document"]
    assert job["current_skill"] is None
    assert job["replay_cases"][0]["metadata"]["gold"]["must_hit"] == ["cite the verified rule"]
    assert "benchmark.json" in {item["path"] for item in job["candidate_skill"]["bundle"]["files"]}
    assert "EVALUATION.md" in {item["path"] for item in job["candidate_skill"]["bundle"]["files"]}


def test_mined_skill_status_tracks_candidate_and_publish_decision(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    _write_mined_skill(miner_root)
    store = _store(tmp_path)
    submitted = submit_mined_skill(store, "demo-skill", skillminer_root=miner_root)
    job_id = submitted["job"]["job_id"]

    pending = list_mined_skill_statuses(store, skillminer_root=miner_root)
    assert pending[0]["status"] == "candidate"
    assert pending[0]["job_id"] == job_id

    store.save_decision(job_id, {"status": "published", "accepted": True})
    published = list_mined_skill_statuses(
        store,
        registered_skill_names=["demo-skill"],
        skillminer_root=miner_root,
    )
    assert published[0]["status"] == "published"
    assert published[0]["registered"] is True


def test_submit_requires_complete_artifacts_and_rejects_path_escape(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    skill_dir = _write_mined_skill(miner_root)
    (skill_dir / "benchmark.json").unlink()

    with pytest.raises(MiningLifecycleError, match="benchmark.json"):
        submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)
    with pytest.raises(MiningLifecycleError):
        resolve_mined_skill_dir("../demo-skill", skillminer_root=miner_root)


def test_submit_mined_skill_accepts_progressive_benchmark_json(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    skill_dir = _write_mined_skill(miner_root)
    (skill_dir / "benchmark.json").unlink()
    benchmark = build_document(
        "demo-skill",
        [{
            "id": "BM-01",
            "input": "Handle this support case.",
            "gold": {"must_hit": [f"criterion {index}" for index in range(1, 13)]},
            "trajectory_requirements": ["confirm facts"],
        }],
    )
    progressive_benchmark.write_document(skill_dir / "benchmark.json", benchmark)

    submitted = submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)

    assert submitted["created"] is True
    assert submitted["job"]["source"]["question_count"] == 1
    assert submitted["job"]["replay_cases"][0]["metadata"]["gold"]["must_hit"] == [
        f"criterion {index}" for index in range(1, 13)
    ]


def test_completed_job_workspace_can_submit_edited_artifacts(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    job_id = "mine-001"
    job_dir = miner_root / "mining_jobs" / job_id
    workspace = job_dir / "workspace"
    _write_mined_skill(workspace)
    (workspace / "compiled_skill" / "demo-skill" / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: Human refined skill\n"
        "category: support\n"
        "---\n\n"
        "# Refined procedure\n",
        encoding="utf-8",
    )
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": job_id, "status": "succeeded"}),
        encoding="utf-8",
    )

    resolved = resolve_mined_job_workspace(job_id, skillminer_root=miner_root)
    submitted = submit_mined_skill(
        _store(tmp_path),
        "demo-skill",
        skillminer_root=resolved,
        mining_job_id=job_id,
    )

    assert resolved == workspace
    assert submitted["job"]["candidate_skill"]["description"] == "Human refined skill"
    assert submitted["job"]["source"]["mining_job_id"] == job_id


def test_completed_job_workspace_submits_current_benchmark_json(tmp_path: Path) -> None:
    """The task-detail hand-off must accept the current non-JSONL artifact."""
    miner_root = tmp_path / "skillminer"
    job_id = "mine-progressive-json"
    job_dir = miner_root / "mining_jobs" / job_id
    workspace = job_dir / "workspace"
    skill_dir = _write_mined_skill(workspace)
    (skill_dir / "benchmark.json").unlink()
    progressive_benchmark.write_document(
        skill_dir / "benchmark.json",
        build_document(
            "demo-skill",
            [{
                "id": "BM-01",
                "input": "Handle this support case.",
                "requirements": [f"criterion {index}" for index in range(1, 13)],
                "trajectory_requirements": ["confirm facts"],
            }],
        ),
    )
    job_dir.mkdir(parents=True, exist_ok=True)
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": job_id, "status": "succeeded"}),
        encoding="utf-8",
    )

    workspace_root = resolve_mined_job_skill_root(
        job_id,
        "demo-skill",
        skillminer_root=miner_root,
    )
    submitted = submit_mined_skill(
        _store(tmp_path),
        "demo-skill",
        skillminer_root=workspace_root,
        mining_job_id=job_id,
    )

    assert submitted["created"] is True
    assert submitted["job"]["source"]["mining_job_id"] == job_id
    assert submitted["job"]["source"]["question_count"] == 1


def test_non_completed_job_cannot_enter_evolution(tmp_path: Path) -> None:
    miner_root = tmp_path / "skillminer"
    job_id = "mine-running"
    job_dir = miner_root / "mining_jobs" / job_id
    _write_mined_skill(job_dir / "workspace")
    (job_dir / "job.json").write_text(
        json.dumps({"job_id": job_id, "status": "running"}),
        encoding="utf-8",
    )

    with pytest.raises(MiningLifecycleError, match="已完成"):
        resolve_mined_job_workspace(job_id, skillminer_root=miner_root)
    with pytest.raises(MiningLifecycleError):
        resolve_mined_job_workspace("../mine-running", skillminer_root=miner_root)




def test_console_mined_submission_uses_tenant_hub_and_complete_bundles(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from team_skills.library.bundle import candidate_skill_bundle
    from team_skills.library.hub import SkillHub
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.proxy import ProxyServer, routes

    miner_root = tmp_path / "miner"
    skill_dir = _write_mined_skill(miner_root)
    published_bundle = {"SKILL.md": (skill_dir / "SKILL.md").read_bytes(), "references/rule.md": b"published rule"}
    store = _store(tmp_path)
    captured = []

    class Hub:
        def list_skills(self):
            return [{"name": "demo-skill"}]

        def read_skill_bundle(self, name):
            assert name == "demo-skill"
            return published_bundle

    def hub_for_config(config, *, tenant_id):
        captured.append(tenant_id)
        return Hub()

    monkeypatch.setattr(SkillHub, "team_from_config", hub_for_config)
    monkeypatch.setattr(routes.ValidationStore, "from_config", lambda *args, **kwargs: store)
    original_submit = routes.submit_mined_skill
    monkeypatch.setattr(routes, "submit_mined_skill", lambda *args, **kwargs: original_submit(
        *args, **kwargs, skillminer_root=miner_root
    ))
    server = ProxyServer(TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "cache"),
        sharing_skill_mirror_enabled=False,
    ))
    client = TestClient(server.app)
    assert client.get("/api/mined-skills").status_code == 401
    response = client.post("/api/auth/bootstrap", json={"username": "admin", "password": "test-password"})
    assert response.status_code == 200
    from teamEvolver.admin.identity_store import IdentityStore

    IdentityStore(server.config).set_member("default", "admin", "tenant_admin", "active", "admin")
    response = client.post("/api/mined-skills/demo-skill/submit", headers={"X-Tenant-Id": "default"})
    assert response.status_code == 200, response.text
    job = store.load_job(response.json()["job_id"])
    assert candidate_skill_bundle(job["current_skill"]) == published_bundle
    assert (
        candidate_skill_bundle(job["candidate_skill"])["benchmark.json"]
        == (skill_dir / "benchmark.json").read_bytes()
    )
    assert job["replay_cases"][0]["checks"]
    assert captured and set(captured) == {"default"}


def test_jsonl_artifact_cannot_enter_candidate_review(tmp_path):
    from team_miner.lifecycle import load_internal_benchmark

    skill_dir = _write_mined_skill(tmp_path / "miner")
    (skill_dir / "benchmark.json").unlink()
    (skill_dir / "benchmark.jsonl").write_text('{"input": "Old task"}\n')
    with pytest.raises(MiningLifecycleError, match="缺少 benchmark.json"):
        load_internal_benchmark(skill_dir)


def test_document_runtime_binding_preserves_evidence_but_discards_model_execution_settings():
    uri = "viking://resources/team/manual.md"
    raw = build_document(
        "demo-skill",
        [{
            "query": "Produce the requested report.",
            "source_session_ids": [uri],
            "materials": [{"path": "input.txt", "content_b64": "YQ=="}],
        }],
    )["cases"][0]
    raw["provenance"].update({
        "session_id": "invented-session",
        "session_snapshot": {"session_id": "invented-session"},
        "source_runtime": {"type": "model-selected-runtime"},
        "source_runtime_context": {"user_id": "another-user"},
        "context_snapshot": {"private": "context"},
        "context_snapshot_id": "invented-context",
    })
    raw["replay"].update({
        "execution": {"kind": "session", "runtime_type": "model-selected-runtime"},
        "adapter": "model-selected.py",
        "timeout_seconds": 999999,
    })
    original = copy.deepcopy(raw)

    case = benchmark_replay_cases("demo-skill", [raw])[0]

    assert raw == original
    for key in ("case_id", "query", "checks", "materials", "metadata"):
        assert case[key] == raw[key]
    assert case["provenance"] == {
        "session_ids": [uri], "evidence_window": "historical", "synthesis_mode": "fixture",
    }
    assert case["replay"] == {
        "progressive_disclosure": raw["replay"]["progressive_disclosure"],
        "execution": {"kind": "document", "runtime_type": "document", "binding": "tenant_replay_adapter"},
    }


def test_same_bundle_without_producer_runtime_binding_does_not_reuse_old_candidate(tmp_path):
    miner_root = tmp_path / "miner"
    _write_mined_skill(miner_root)
    store = _store(tmp_path)
    first = submit_mined_skill(store, "demo-skill", skillminer_root=miner_root)
    old_job = copy.deepcopy(first["job"])
    old_job["source"].pop("replay_binding_version")
    old_job.pop("runtime_validation_policy")
    old_job["replay_cases"][0]["replay"].pop("execution")
    store.save_job(old_job)
    stored_old = store.load_job(old_job["job_id"])

    bound = submit_mined_skill(store, "demo-skill", skillminer_root=miner_root)
    duplicate = submit_mined_skill(store, "demo-skill", skillminer_root=miner_root)

    assert bound["created"] and not duplicate["created"]
    assert bound["job"]["job_id"] != old_job["job_id"]
    assert duplicate["job"]["job_id"] == bound["job"]["job_id"]
    assert bound["job"]["source"]["artifact_sha256"] == old_job["source"]["artifact_sha256"]
    assert store.load_job(old_job["job_id"]) == stored_old


@pytest.mark.parametrize("has_published", [False, True])
def test_mined_submission_reaches_replay_factory_with_independent_branch_contexts(tmp_path, monkeypatch, has_published):
    """Exercise the producer→spec→gateway→engine seam with a unit-test customer factory."""
    from team_replay import engine
    from team_replay.artifacts import decode_bundle_payload, validate_skill_treatment
    from team_replay.contracts import skill_replay_spec
    from team_replay.execution import run_branch
    from team_replay.gateway import EmbeddedReplayGateway
    from team_replay.hooks import AgentObservation
    from team_replay.host import configure_host, current_host
    from team_replay.runtime_matrix import evaluate_runtime_compatibility, runtime_type_for_case

    miner_root = tmp_path / "miner"
    skill_dir = _write_mined_skill(miner_root)
    dataset = json.loads((skill_dir / "benchmark.json").read_text())
    dataset["cases"][0]["materials"] = [{"path": "input.txt", "content_b64": "YQ=="}]
    progressive_benchmark.write_document(skill_dir / "benchmark.json", dataset)
    published = {"name": "demo-skill", "description": "Published method", "content": "Published procedure"}
    job = submit_mined_skill(
        _store(tmp_path), "demo-skill", skillminer_root=miner_root,
        current_skill=published if has_published else None,
    )["job"]
    contexts, sessions = [], []

    class Session:
        def __init__(self, context):
            self.context, self.messages, self.closed = context, [], False

        def send(self, message):
            self.messages.append(message)
            tokens = 20 if self.context.treatment.branch == "baseline" else 10
            return AgentObservation("Completed report", metrics={"total_tokens": tokens})

        def close(self):
            self.closed = True

    class Factory:
        def open(self, context):
            contexts.append(copy.deepcopy(context))
            session = Session(context)
            sessions.append(session)
            return session

    class Host:
        def resolve_replay_factory(self, runtime_type, source):
            assert runtime_type == "document"
            assert not source.get("session_id")
            return Factory()

        def load_source_session(self, session_id):
            pytest.fail("Document Evidence must not be loaded as an Agent Session")

        def load_context_snapshot(self, *args):
            pytest.fail("Document cases have no historical private Context snapshot")

        def validate_skill_treatment(self, skill):
            return validate_skill_treatment(skill)

        def judge_harness(self):
            return {}

    def judge(**kwargs):
        return {
            "judge": "model", "all_satisfied": True,
            "items": [{**item, "satisfied": True, "evidence": "unit-test report"} for item in kwargs["checklist"]],
        }

    monkeypatch.setattr(engine, "run_branch", lambda *args, **kwargs: run_branch(*args, **kwargs, judge=judge))
    previous = current_host(required=False)
    configure_host(Host())
    try:
        result = EmbeddedReplayGateway(engine.evaluate_job).evaluate(skill_replay_spec(job))
    finally:
        configure_host(previous)

    assert result["status"] == "evaluated" and result["runtime_type"] == "document"
    assert runtime_type_for_case(job["replay_cases"][0]) == "document"
    assert evaluate_runtime_compatibility(job["runtime_validation_policy"], [result])["status"] == "passed"
    assert len(contexts) == len(sessions) == 2
    baseline, candidate = contexts
    assert baseline.request_id != candidate.request_id
    assert baseline.treatment.branch == "baseline" and candidate.treatment.branch == "candidate"
    assert baseline.treatment.skill == job["current_skill"]
    assert {key: value for key, value in candidate.treatment.skill.items() if key != "bundle"} == {
        key: value for key, value in job["candidate_skill"].items() if key != "bundle"
    }
    execution_files = decode_bundle_payload(candidate.treatment.skill["bundle"])
    audit_files = decode_bundle_payload(job["candidate_skill"]["bundle"])
    assert set(execution_files) == {"SKILL.md"}
    assert {"benchmark.json", "BENCHMARK.md", "EVALUATION.md"} <= set(audit_files)
    assert execution_files["SKILL.md"] == audit_files["SKILL.md"]
    assert baseline.context_snapshot == candidate.context_snapshot == {}
    assert baseline.materials == candidate.materials == ({"path": "input.txt", "content_b64": "YQ=="},)
    assert all(session.messages == [job["replay_cases"][0]["query"]] and session.closed for session in sessions)


def test_mined_replay_without_tenant_factory_is_inconclusive_and_cannot_pass_runtime_gate(tmp_path):
    from team_replay import engine
    from team_replay.artifacts import validate_skill_treatment
    from team_replay.contracts import skill_replay_spec
    from team_replay.gateway import EmbeddedReplayGateway
    from team_replay.host import configure_host, current_host
    from team_replay.runtime_matrix import evaluate_runtime_compatibility

    miner_root = tmp_path / "miner"
    _write_mined_skill(miner_root)
    job = submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)["job"]
    resolutions = []

    class Host:
        def resolve_replay_factory(self, runtime_type, source):
            resolutions.append(runtime_type)
            return None

        def load_source_session(self, session_id):
            pytest.fail("Evidence URI is not an Agent Session")

        def validate_skill_treatment(self, skill):
            return validate_skill_treatment(skill)

    previous = current_host(required=False)
    configure_host(Host())
    try:
        result = EmbeddedReplayGateway(engine.evaluate_job).evaluate(skill_replay_spec(job))
    finally:
        configure_host(previous)

    assert resolutions == ["document"]
    assert result["status"] == "skipped" and result["verdict"] == "inconclusive"
    assert not result["accepted"] and not result["cases"]
    assert "adapter" in result["reason"].lower()
    gate = evaluate_runtime_compatibility(job["runtime_validation_policy"], [result])
    assert gate["status"] == "inconclusive" and not gate["matrix"]["document"]["accepted"]


def test_folded_mined_description_is_normalized_before_actual_host_static_validation(tmp_path):
    from team_replay import engine
    from team_replay.contracts import skill_replay_spec
    from team_replay.gateway import EmbeddedReplayGateway
    from team_replay.host import configure_host, current_host
    from team_skills.candidates.static_checks import validate_candidate_bundle
    from team_skills.library.bundle import (
        bundle_tree_sha256,
        decode_bundle_payload,
        encode_bundle_payload,
        read_skill_bundle,
    )
    from team_skills.library.frontmatter import parse_skill_md, validate_skill_md
    from teamEvolver.replay_adapter import TeamEvolverReplayHost

    miner_root = tmp_path / "miner"
    skill_dir = _write_mined_skill(miner_root)
    (skill_dir / "SKILL.md").write_text(
        "---\n"
        "name: demo-skill\n"
        "description: >-\n"
        "  Verify TEST-RETURN ordinary goods:\n"
        "  keep unopened items within seven days,\n"
        "  and refund paid goods only.\n"
        "metadata:\n"
        "  teamEvolver: {category: support}\n"
        "  source: synthetic-qa\n"
        "allowed-tools: [Read]\n"
        "---\n\n"
        "# Procedure\n\n未拆封且日期差不超过 7 日才受理；退款不含原运费。\n",
        encoding="utf-8",
    )
    (skill_dir / "references").mkdir()
    (skill_dir / "references/policy.md").write_text("# Original Evidence\n", encoding="utf-8")
    original = read_skill_bundle(skill_dir)
    parsed = parse_skill_md(str(skill_dir / "SKILL.md"))
    raw_validation = validate_candidate_bundle({"name": "demo-skill", "bundle": encode_bundle_payload(original)})
    assert not raw_validation["passed"]
    assert any("folded YAML" in error for error in raw_validation["errors"])

    job = submit_mined_skill(_store(tmp_path), "demo-skill", skillminer_root=miner_root)["job"]
    submitted_bundle = decode_bundle_payload(job["candidate_skill"]["bundle"])
    wire = validate_skill_md(submitted_bundle["SKILL.md"], expected_name="demo-skill")

    assert read_skill_bundle(skill_dir) == original
    assert job["source"]["artifact_sha256"] == bundle_tree_sha256(original)
    assert job["candidate_skill"]["bundle"]["tree_sha256"] != job["source"]["artifact_sha256"]
    assert submitted_bundle["SKILL.md"] != original["SKILL.md"]
    assert {key: value for key, value in submitted_bundle.items() if key != "SKILL.md"} == {
        key: value for key, value in original.items() if key != "SKILL.md"
    }
    assert wire["frontmatter"]["description"] == job["candidate_skill"]["description"] == parsed["description"]
    assert wire["body"] == job["candidate_skill"]["content"] == parsed["content"]
    assert wire["frontmatter"]["metadata"] == parsed["metadata"]
    assert wire["frontmatter"]["allowed-tools"] == parsed["_extra_frontmatter"]["allowed-tools"]
    assert "description: >" not in submitted_bundle["SKILL.md"].decode("utf-8")
    validations, resolutions = [], []

    class Host(TeamEvolverReplayHost):
        def validate_skill_treatment(self, skill):
            validation = super().validate_skill_treatment(skill)
            validations.append(validation)
            return validation

        def resolve_replay_factory(self, runtime_type, source):
            resolutions.append(runtime_type)
            return None

        def load_source_session(self, session_id):
            pytest.fail("Document Evidence must not be loaded as a historical Session")

    previous = current_host(required=False)
    configure_host(Host())
    try:
        result = EmbeddedReplayGateway(engine.evaluate_job).evaluate(skill_replay_spec(job))
    finally:
        configure_host(previous)

    assert len(validations) == 1 and validations[0]["passed"]
    assert resolutions == ["document"]
    assert result["status"] == "skipped" and result["verdict"] == "inconclusive"
    assert "adapter" in result["reason"].lower()
