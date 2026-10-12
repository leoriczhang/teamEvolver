"""Exercise durable OV round submissions and human decisions without model calls."""

from __future__ import annotations

import asyncio
import base64
import copy
import json
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from team_miner import ov_round_orchestration
from team_miner.bridge import SkillMinerBridgeMixin
from team_miner.ov_compile_mining import REVIEW_SCHEMA_VERSION, RUNS_ROOT, SKILL_NAME, OvSkillMiningRepository
from team_miner.tests.dataset_fixtures import build_capability_plan, build_document

SOURCE = "viking://resources/team/source"
APPROVED = "viking://resources/team/approved/policy.md"


class FakeOV:
    def __init__(self):
        self.content = {f"{SOURCE}/policy.md": "Original policy: divisor 9000.", APPROVED: "Approved regional scope."}
        self.compile_calls = []
        self.accepted = {}
        self.published = []
        self.compiler_skills = {}
        self.compiler_reads = []
        self.mkdirs = []
        self.tasks = {}
        self.cancelled = []
        self.lose_next_acceptance = False
        self.lookup_supported = False
        self.submission_lookups = []

    def tree(self, uri):
        entries = [
            {"uri": key, "is_dir": False, "size": len(value.encode())}
            for key, value in sorted(self.content.items())
            if key.startswith(uri.rstrip("/") + "/")
        ]
        return {"exists": bool(entries), "entries": entries}

    async def request(self, account, method, path, **kwargs):
        assert account == "default"
        if path == "/api/v1/content/read":
            uri = kwargs["params"]["uri"]
            if uri not in self.content:
                raise HTTPException(404, "Evidence file missing")
            return {"content": self.content[uri]}
        if path == "/api/v1/content/write":
            body = kwargs["json"]
            assert body["mode"] == "create"
            if body["uri"] in self.content:
                raise HTTPException(409, "ALREADY_EXISTS")
            self.content[body["uri"]] = body["content"]
            return {"ok": True}
        if path.startswith("/api/v1/compile/submissions/"):
            key = path.rsplit("/", 1)[-1]
            self.submission_lookups.append(key)
            if not self.lookup_supported or key not in self.accepted:
                raise HTTPException(404, "Submission not found")
            return {"task_id": self.accepted[key][0]}
        if path == "/api/v1/compile":
            body = copy.deepcopy(kwargs["json"])
            key = kwargs["extra_headers"]["Idempotency-Key"]
            self.compile_calls.append((key, body))
            if key in self.accepted:
                task_id, previous = self.accepted[key]
                assert previous == body, "A retry must submit the identical Compile payload"
            else:
                task_id = f"compile-{len(self.accepted) + 1}"
                self.accepted[key] = (task_id, body)
                self.tasks[task_id] = {"status": "queued", "stage": "compile: queued"}
            if self.lose_next_acceptance:
                self.lose_next_acceptance = False
                raise httpx.ReadTimeout("accepted remotely, response lost")
            return {"task_id": task_id}
        if path.startswith("/api/v1/tasks/"):
            task_id = path.split("/")[4]
            if path.endswith("/cancel"):
                self.cancelled.append(task_id)
                self.tasks[task_id] = {"status": "cancelled", "stage": "compile: cancelled"}
                return {"status": "cancelled"}
            return self.tasks[task_id]
        raise AssertionError((method, path, kwargs))

    def complete(self, job, *, questions=True, skill_names=None):
        source = job["source_snapshot"] + "/original/policy.md"
        names = ["policy-review"] if skill_names is None else skill_names
        review = {
            "schema_version": REVIEW_SCHEMA_VERSION,
            "questions": [
                {
                    "qid": "scope",
                    "question": "Which regions are supported?",
                    "reason": "Scope is absent.",
                    "source_refs": [source],
                    "required": True,
                },
            ]
            if questions
            else [],
        }
        files = {
            "analysis/evidence-map.md": f"# Evidence\n{source}",
            "analysis/gaps.md": "# Gaps\nReview regional scope.",
            "analysis/review.json": json.dumps(review),
        }
        if int(job.get("candidate_contract_version") or 1) >= 2:
            files["analysis/capability-plan.json"] = json.dumps(build_capability_plan(names, source))
        for name in names:
            document = build_document(
                name,
                [
                    {
                        "case_id": f"{name}-{case:03d}",
                        "input": f"Apply policy in scenario {case}",
                        "requirements": [f"Condition {n}" for n in range(12)],
                        "source_session_ids": [source],
                    }
                    for case in range(12)
                ],
            )
            files.update(
                {
                    f"compiled_skill/{name}/SKILL.md": (
                        f"---\nname: {name}\ndescription: Apply sourced policy.\n---\n# {name}"
                    ),
                    f"compiled_skill/{name}/EVALUATION.md": "# Evaluation",
                    f"compiled_skill/{name}/BENCHMARK.md": "# Test Dataset",
                    f"compiled_skill/{name}/benchmark.json": json.dumps(document),
                }
            )
        self.content.update({job["output_uri"] + "/" + name: content for name, content in files.items()})
        self.tasks[job["ov_task_id"]] = {"status": "completed", "stage": "compile: completed"}


class FakeCompileClient:
    def __init__(self, remote, **kwargs):
        self.remote = remote

    async def get_skill(self, *, skill_name):
        self.remote.compiler_reads.append(skill_name)
        body = self.remote.compiler_skills.get(skill_name)
        if body is None:
            return {"ok": False, "exit_code": 404, "stderr": "Shared Skill not found", "result": {}}
        return {
            "ok": True,
            "result": {
                "root_uri": f"viking://agent/skills/{skill_name}",
                "content": body,
                "content_sha256": ov_round_orchestration._digest(body),
                "revision": "compiler-revision",
            },
        }

    async def install_skill(self, **kwargs):
        self.remote.published.append(copy.deepcopy(kwargs))
        name = kwargs["skill_name"]
        assert kwargs.get("parent_uri") == "viking://agent/skills"
        if name in self.remote.compiler_skills:
            return {"ok": False, "exit_code": 409, "stderr": "ALREADY_EXISTS", "result": {}}
        self.remote.compiler_skills[name] = kwargs["skill_body"]
        return {"ok": True, "result": {"root_uri": f"viking://agent/skills/{name}", "revision": "compiler-revision"}}

    async def publish_shared_skill(self, **kwargs):
        raise AssertionError("Compiler pins must never PUT or overwrite an existing shared Skill")

    async def mkdir(self, uri):
        self.remote.mkdirs.append(uri)
        return {"ok": True}


class Owner(SkillMinerBridgeMixin):
    config = SimpleNamespace()

    def __init__(self, repository, remote):
        self.repository = repository
        self.remote = remote
        self.config = SimpleNamespace(_config_file=str(repository.state_root / "test-config.yaml"))
        self.submissions = []
        self.refreshes = []
        self.compiler_body = ov_round_orchestration.SKILL_BODY

    def _ov_skill_mining_repository(self):
        return self.repository

    def _ov_skill_compiler_body(self):
        return self.compiler_body

    def _knowledge_endpoint(self):
        return "http://fake-ov.test"

    def _knowledge_team_user(self):
        return "team-user"

    def _knowledge_api_key(self):
        return "fixture-only"

    def _knowledge_account(self, value):
        return value or "default"

    async def _knowledge_request(self, *args, **kwargs):
        return await self.remote.request(*args, **kwargs)

    async def _knowledge_tree(self, account, uri):
        assert account == "default"
        return self.remote.tree(uri)

    def _start_ov_skill_monitor(self):
        pass

    def _schedule_ov_skill_round_submission(self, job):
        self.submissions.append(copy.deepcopy(job))

    def _schedule_ov_skill_job_refresh(self, job, **kwargs):
        self.refreshes.append(copy.deepcopy(job))

    async def _dispatch_skillminer_request(self, request):
        raise AssertionError(f"Round route must not fall back to legacy mining: {request.url.path}")


@pytest.fixture
def rig(tmp_path, monkeypatch):
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")
    remote = FakeOV()
    monkeypatch.setattr(ov_round_orchestration, "CompileClient", lambda **kwargs: FakeCompileClient(remote, **kwargs))
    return Owner(repository, remote)


def create(owner, *, rounds=3, human=True, name="Round integration"):
    return owner.repository.create_job(
        name=name,
        account="default",
        source_uri=SOURCE,
        source_snapshot_uri=f"{RUNS_ROOT}/pending/source",
        output_uri=f"{RUNS_ROOT}/pending/output",
        document_count=1,
        max_rounds=rounds,
        human_checkpoints=human,
    )


async def complete(owner, job, **kwargs):
    submitted = await owner._submit_ov_skill_round(job)
    owner.remote.complete(submitted, **kwargs)
    await owner._refresh_ov_skill_job(submitted)
    return owner.repository.get_job(job["job_id"])


def app_for(owner, *, admin=True):
    app = FastAPI()

    @app.middleware("http")
    async def identity(request, call_next):
        request.state.console_user = {"role": "admin" if admin else "viewer", "username": "reviewer"}
        return await call_next(request)

    owner._register_skillminer_routes(app)
    return app


@pytest.mark.asyncio
async def test_first_round_freezes_sources_and_accepts_candidate_collection_contract(rig):
    job = create(rig)
    waiting = await complete(rig, job)
    assert waiting["status"] == "waiting_human" and waiting["current_round"] == 1
    assert waiting["compile_request"]["from"] == [waiting["source_snapshot"]]
    assert len(rig.repository.public_job(waiting, detail=True)["rounds"][0]["artifacts"]) == 8
    assert not waiting["finished_at"] and not waiting["artifact_quality"]["can_submit"]
    assert Path(waiting["input_manifest"]).is_file()
    assert "analysis/review.json" in rig.remote.published[0]["skill_body"]
    assert rig.remote.published[0]["skill_name"].startswith(SKILL_NAME + "-")
    assert waiting["compile_request"]["skill"] == waiting["compiler_uri"]
    assert waiting["compiler_uri"].endswith(rig.remote.published[0]["skill_name"])


@pytest.mark.asyncio
async def test_lost_acceptance_restarts_with_same_payload_key_and_frozen_content(rig, monkeypatch):
    initial = create(rig)
    rig.remote.lose_next_acceptance = True
    pending = await rig._submit_ov_skill_round(initial)
    assert pending["status"] == "preparing" and pending["compile_request"] and not pending["ov_task_id"]
    assert pending["ov_sync_failures"] == 1 and pending["ov_next_retry_at"]
    rig.remote.content[f"{SOURCE}/policy.md"] = "Changed live policy must not enter the retry."
    rig.compiler_body += "\nChanged compiler must not enter the retry.\n"
    restarted_repo = OvSkillMiningRepository(rig.repository.state_root, rig.repository.project_root)
    restarted = Owner(restarted_repo, rig.remote)
    restarted.compiler_body = rig.compiler_body
    resumed = await restarted._submit_ov_skill_round(restarted_repo.get_job(initial["job_id"]))
    assert resumed["status"] == "queued" and resumed["ov_task_id"] == "compile-1"
    assert len(rig.remote.accepted) == 1 and rig.remote.compile_calls[0] == rig.remote.compile_calls[1]
    assert rig.remote.content[resumed["source_snapshot"] + "/original/policy.md"] == "Original policy: divisor 9000."
    assert len(rig.remote.published) == 1
    assert resumed["ov_sync_failures"] == 0 and not resumed["submission_error"]


@pytest.mark.asyncio
async def test_tampered_frozen_cache_fails_before_compile(rig):
    initial = create(rig)
    frozen = await rig._freeze_round_inputs(initial)
    Path(frozen[0]["local_path"]).write_text("Tampered frozen Evidence")
    failed = await rig._submit_ov_skill_round(initial)
    assert failed["status"] == "failed" and "完整性" in failed["error"]
    assert not rig.remote.compile_calls


@pytest.mark.asyncio
async def test_api_continue_freezes_human_evidence_and_separates_previous_candidate_context(rig):
    waiting = await complete(rig, create(rig, rounds=2))
    checkpoint = waiting["pending_checkpoint"]["id"]
    skill_path = rig.repository.workspace(waiting) / "compiled_skill/policy-review/SKILL.md"
    skill_path.write_text(skill_path.read_text() + "\nHuman reviewed candidate revision\n")
    transport = httpx.ASGITransport(app=app_for(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        upload = await client.post(
            f"/api/mining/jobs/{waiting['job_id']}/supplements",
            json={
                "checkpoint_id": checkpoint,
                "files": [
                    {
                        "name": "region.md",
                        "content_base64": base64.b64encode(b"# Approved attachment\nRegional terms.").decode(),
                    }
                ],
            },
        )
        assert upload.status_code == 200, upload.text
        assert len(upload.json()["saved"]) == 1
        assert "local_path" not in json.dumps(upload.json())
        payload = {
            "checkpoint_id": checkpoint,
            "answers": {"scope": "Approved regions only."},
            "unresolved": [],
            "references": {"scope": [APPROVED]},
            "feedback": "Recheck applicability.",
            "request_id": "review-round-1",
        }
        continued = await client.post(f"/api/mining/jobs/{waiting['job_id']}/continue", json=payload)
        assert continued.status_code == 200, continued.text
        duplicate = await client.post(f"/api/mining/jobs/{waiting['job_id']}/continue", json=payload)
        assert duplicate.status_code == 200
    second = rig.repository.get_job(waiting["job_id"])
    assert second["current_round"] == 2 and second["status"] == "preparing"
    assert second["supplements"][0]["submitted_by"] == "reviewer"
    rig.remote.content[APPROVED] = "Live reference changed after the human decision."
    submitted = await rig._submit_ov_skill_round(second)
    source = submitted["source_snapshot"]
    context = submitted["round_context_uri"]
    assert submitted["compile_request"]["from"] == [source]
    assert context not in submitted["compile_request"]["from"]
    assert "Human reviewed candidate revision" in submitted["compile_request"]["instruction"]
    assert "Recheck applicability." in submitted["compile_request"]["instruction"]
    assert "scope" in submitted["compile_request"]["instruction"]
    assert source.endswith("/round-2/source") and context.endswith("/round-2/context")
    assert rig.remote.content[source + "/original/policy.md"] == "Original policy: divisor 9000."
    human = rig.remote.content[source + "/supplements/round-1/answers.md"]
    assert "Approved regions only." in human and "reviewer" in human and "unresolved items are NOT facts" in human
    assert any(
        "Approved regional scope." in value
        for uri, value in rig.remote.content.items()
        if uri.startswith(source + "/supplements/")
    )
    assert any(
        "Regional terms." in value
        for uri, value in rig.remote.content.items()
        if uri.startswith(source + "/supplements/")
    )
    assert "Human reviewed candidate revision" in rig.remote.content[context + "/compiled_skill/policy-review/SKILL.md"]
    assert "NOT source Evidence" in rig.remote.content[context + "/REVISION_CONTEXT.md"]
    assert "Recheck applicability." in rig.remote.content[context + "/human-review/round-1.md"]
    assert "Recheck applicability." not in human
    assert "Which regions are supported?" in human
    assert "clarification_question_not_a_fact" in human
    assert all(not uri.startswith(source + "/compiled_skill/") for uri in rig.remote.content)
    assert len(rig.remote.accepted) == 2


@pytest.mark.asyncio
async def test_without_human_wait_bridge_runs_two_distinct_compile_rounds(rig):
    first = await complete(rig, create(rig, rounds=2, human=False), questions=False)
    assert first["status"] == "preparing" and first["current_round"] == 2
    assert len(rig.submissions) == 1 and rig.submissions[0]["current_round"] == 2
    assert not first["pending_checkpoint"]
    final = await complete(rig, first, questions=False)
    assert final["status"] == "succeeded" and final["current_round"] == 2
    assert len(final["rounds_history"]) == 2 and len(rig.remote.accepted) == 2
    assert rig.remote.compile_calls[0][0] != rig.remote.compile_calls[1][0]
    assert rig.remote.compile_calls[1][1]["from"] == [final["source_snapshot"]]
    assert final["round_context_uri"] not in rig.remote.compile_calls[1][1]["from"]
    assert not final["supplements"] and not final["pending_checkpoint"]


@pytest.mark.asyncio
async def test_second_round_receives_every_candidate_plan_and_matching_case_ids(rig):
    names = ["policy-review", "regional-assessment"]
    second = await complete(rig, create(rig, rounds=2, human=False), questions=False, skill_names=names)
    first_archive = rig.repository.jobs_root / second["job_id"] / "rounds/round-1/reviewed_output"
    previous_plan = (first_archive / "semantic_reports/capability-plan.json").read_text()
    submitted = await rig._submit_ov_skill_round(second)
    instruction = submitted["compile_request"]["instruction"]
    revision = json.loads(instruction.rsplit("\n", 1)[-1])
    assert revision["candidate_contract_version"] == 2
    assert json.loads(revision["previous_capability_plan"]) == json.loads(previous_plan)
    assert not revision["previous_capability_plan_truncated"] and not revision["previous_capability_plan_missing"]
    assert {row["skill_name"] for row in revision["previous_candidates"]} == set(names)
    for row in revision["previous_candidates"]:
        name = row["skill_name"]
        assert row["case_ids"] == [f"{name}-{number:03d}" for number in range(12)]
        assert f"name: {name}" in row["previous_skill_body"]
        assert not row["previous_skill_body_truncated"] and not row["case_ids_truncated"]
        assert not row["test_dataset_missing"] and not row["test_dataset_invalid"]
    assert submitted["compile_request"]["from"] == [submitted["source_snapshot"]]
    assert "拆分、合并或暂缓" in instruction
    archived_plan_uri = submitted["round_context_uri"] + "/semantic_reports/capability-plan.json"
    assert rig.remote.content[archived_plan_uri] == previous_plan


@pytest.mark.asyncio
async def test_zero_candidate_round_retains_deferred_capability_plan_for_revision(rig):
    second = await complete(rig, create(rig, rounds=2, human=False), questions=False, skill_names=[])
    assert second["current_round"] == 2 and second["status"] == "preparing"
    submitted = await rig._submit_ov_skill_round(second)
    revision = json.loads(submitted["compile_request"]["instruction"].rsplit("\n", 1)[-1])
    assert revision["previous_candidates"] == []
    plan = json.loads(revision["previous_capability_plan"])
    assert plan["skills"] == [] and plan["deferred"][0]["reason"]
    assert not revision["previous_capability_plan_missing"]


@pytest.mark.asyncio
async def test_collection_revision_bounds_all_eight_candidate_bodies_without_losing_identity(rig, tmp_path):
    names = [f"task-{number}" for number in range(8)]
    entries = []

    def add(relative, content):
        local = tmp_path / "context" / relative
        local.parent.mkdir(parents=True, exist_ok=True)
        local.write_text(content)
        entries.append({"kind": "context", "relative_path": relative, "local_path": str(local)})

    for name in names:
        add(f"compiled_skill/{name}/SKILL.md", f"name: {name}\n" + '"Claim with escaping" ' * 10_000)
        add(
            f"compiled_skill/{name}/benchmark.json",
            json.dumps({"cases": [{"case_id": f"{name}-{number:03d}"} for number in range(24)]}),
        )
    plan = build_capability_plan(names, f"{SOURCE}/policy.md")
    plan["skills"][0]["rationale"] = "Detailed method rationale. " * 2000
    add("semantic_reports/capability-plan.json", json.dumps(plan))
    instruction = await rig._round_revision_instruction({"current_round": 2, "candidate_contract_version": 2}, entries)
    assert len(instruction) <= 30_000
    revision = json.loads(instruction.rsplit("\n", 1)[-1])
    assert revision["previous_capability_plan_truncated"]
    assert {row["skill_name"] for row in revision["previous_candidates"]} == set(names)
    for row in revision["previous_candidates"]:
        assert row["previous_skill_body_truncated"]
        assert row["case_ids"] == [f"{row['skill_name']}-{number:03d}" for number in range(24)]
        assert not row["case_ids_truncated"] and row["case_ids_omitted_count"] == 0


@pytest.mark.asyncio
async def test_revision_retains_invalid_candidate_identity_without_breaking_other_candidates(rig):
    names = ["policy-review", "regional-assessment"]
    submitted = await rig._submit_ov_skill_round(create(rig, rounds=2, human=False))
    rig.remote.complete(submitted, questions=False, skill_names=names)
    rig.remote.content[submitted["output_uri"] + "/compiled_skill/regional-assessment/benchmark.json"] = "invalid JSON"
    await rig._refresh_ov_skill_job(submitted)
    second = rig.repository.get_job(submitted["job_id"])
    assert second["current_round"] == 2
    accepted = await rig._submit_ov_skill_round(second)
    assert accepted["status"] == "queued"
    revision = json.loads(accepted["compile_request"]["instruction"].rsplit("\n", 1)[-1])
    candidates = {row["skill_name"]: row for row in revision["previous_candidates"]}
    assert candidates["regional-assessment"]["test_dataset_invalid"]
    assert candidates["regional-assessment"]["case_ids"] == []
    assert not candidates["policy-review"]["test_dataset_invalid"]
    assert len(candidates["policy-review"]["case_ids"]) == 12


@pytest.mark.asyncio
async def test_revision_keeps_planned_candidate_with_missing_skill_body(rig):
    names = ["policy-review", "regional-assessment"]
    submitted = await rig._submit_ov_skill_round(create(rig, rounds=2, human=False))
    rig.remote.complete(submitted, questions=False, skill_names=names)
    del rig.remote.content[submitted["output_uri"] + "/compiled_skill/regional-assessment/SKILL.md"]
    await rig._refresh_ov_skill_job(submitted)
    second = rig.repository.get_job(submitted["job_id"])
    assert second["current_round"] == 2
    accepted = await rig._submit_ov_skill_round(second)
    revision = json.loads(accepted["compile_request"]["instruction"].rsplit("\n", 1)[-1])
    candidates = {row["skill_name"]: row for row in revision["previous_candidates"]}
    assert set(candidates) == set(names)
    assert candidates["regional-assessment"]["previous_skill_missing"]
    assert candidates["regional-assessment"]["previous_skill_body"] == ""
    assert len(candidates["regional-assessment"]["case_ids"]) == 12
    assert not candidates["policy-review"]["previous_skill_missing"]


@pytest.mark.asyncio
async def test_collection_job_rejects_later_single_candidate_compiler_before_remote_submission(rig):
    second = await complete(rig, create(rig, rounds=2, human=False), questions=False)
    rig.compiler_body = ov_round_orchestration.LEGACY_SKILL_BODY
    failed = await rig._submit_ov_skill_round(second)
    assert failed["status"] == "failed" and "skill-miner.plan.v1" in failed["error"]
    assert len(rig.remote.compile_calls) == 1 and len(rig.remote.published) == 1
    assert not (rig.repository.jobs_root / second["job_id"] / "compiler-round-2.md").exists()


@pytest.mark.asyncio
@pytest.mark.parametrize("contract_version", [None, 1])
async def test_legacy_job_uses_legacy_compiler_and_single_candidate_revision(rig, contract_version):
    initial = create(rig, rounds=2, human=False)
    initial = rig.repository.update_job(initial["job_id"], candidate_contract_version=contract_version)
    second = await complete(rig, initial, questions=False)
    assert rig.remote.published[0]["skill_body"] == ov_round_orchestration._compiler_package(
        ov_round_orchestration.LEGACY_SKILL_BODY
    )[1]
    assert len(rig.repository.public_job(second, detail=True)["rounds"][0]["artifacts"]) == 7
    accepted = await rig._submit_ov_skill_round(second)
    legacy_hash = ov_round_orchestration._digest(ov_round_orchestration.LEGACY_SKILL_BODY)
    assert accepted["compiler_source_sha256"] == legacy_hash
    revision = json.loads(accepted["compile_request"]["instruction"].rsplit("\n", 1)[-1])
    assert revision["previous_skill_name"] == "policy-review" and len(revision["case_ids"]) == 12
    assert "previous_candidates" not in revision


@pytest.mark.asyncio
async def test_legacy_job_preserves_custom_and_previously_frozen_compiler_sources(rig):
    custom = ov_round_orchestration.LEGACY_SKILL_BODY + "\nCustom compiler instruction.\n"
    rig.compiler_body = custom
    first = create(rig, rounds=1)
    first = rig.repository.update_job(first["job_id"], candidate_contract_version=1)
    accepted = await rig._submit_ov_skill_round(first)
    assert accepted["compiler_source_sha256"] == ov_round_orchestration._digest(custom)

    second = create(rig, rounds=1)
    second = rig.repository.update_job(second["job_id"], candidate_contract_version=1)
    frozen = ov_round_orchestration.SKILL_BODY
    (rig.repository.jobs_root / second["job_id"] / "compiler-round-1.md").write_text(frozen)
    accepted_second = await rig._submit_ov_skill_round(second)
    assert accepted_second["compiler_source_sha256"] == ov_round_orchestration._digest(frozen)


@pytest.mark.asyncio
async def test_finish_keeps_unresolved_warning_and_never_publishes_candidate(rig):
    waiting = await complete(rig, create(rig, rounds=1))
    transport = httpx.ASGITransport(app=app_for(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            f"/api/mining/jobs/{waiting['job_id']}/finish",
            json={
                "checkpoint_id": waiting["pending_checkpoint"]["id"],
                "answers": {},
                "unresolved": ["scope"],
                "references": {},
                "feedback": "Unknown scope remains.",
                "request_id": "finish-request",
            },
        )
        assert response.status_code == 200, response.text
    final = rig.repository.get_job(waiting["job_id"])
    assert final["status"] == "succeeded" and final["artifact_quality"]["level"] == "caution"
    assert final["unresolved_questions"] == ["scope"] and final["finished_at"]
    assert all(row["skill_name"].startswith(SKILL_NAME + "-") for row in rig.remote.published)
    assert len(rig.remote.accepted) == 1 and not rig.remote.cancelled and not rig.submissions


@pytest.mark.asyncio
@pytest.mark.parametrize("waiting", [False, True])
async def test_stop_before_submission_or_while_waiting_is_local_and_delete_requires_stop(rig, waiting):
    job = create(rig)
    if waiting:
        job = await complete(rig, job)
    transport = httpx.ASGITransport(app=app_for(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        delete = await client.delete(f"/api/mining/jobs/{job['job_id']}")
        assert delete.status_code == 409
        stopped = await client.post(f"/api/mining/jobs/{job['job_id']}/stop")
        assert stopped.status_code == 200, stopped.text
        assert stopped.json()["job"]["status"] == "stopped"
        delete = await client.delete(f"/api/mining/jobs/{job['job_id']}")
        assert delete.status_code == 200
    assert not rig.remote.cancelled and not rig.submissions


@pytest.mark.asyncio
async def test_stop_after_lost_acceptance_recovers_idempotently_then_cancels_remote(rig):
    initial = create(rig)
    rig.remote.lose_next_acceptance = True
    rig.remote.lookup_supported = True
    pending = await rig._submit_ov_skill_round(initial)
    transport = httpx.ASGITransport(app=app_for(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/mining/jobs/{initial['job_id']}/stop")
        assert response.status_code == 200 and response.json()["job"]["status"] == "stopping"
    stopping = rig.repository.get_job(initial["job_id"])
    recovered = await rig._submit_ov_skill_round(stopping)
    assert recovered["ov_task_id"] == "compile-1" and recovered["status"] == "stopping"
    await rig._refresh_ov_skill_job(recovered)
    await rig._refresh_ov_skill_job(rig.repository.get_job(initial["job_id"]))
    final = rig.repository.get_job(initial["job_id"])
    assert final["status"] == "stopped" and rig.remote.cancelled == ["compile-1"]
    assert len(rig.remote.compile_calls) == 1 and len(rig.remote.accepted) == 1
    assert rig.remote.submission_lookups == [pending["submission_idempotency_key"]]
    assert pending["compile_request"] == recovered["compile_request"]


@pytest.mark.asyncio
async def test_created_job_is_durable_before_async_submission_and_admin_only(rig):
    source = rig.repository.save_source(account="default", uri=SOURCE, name="Policy", tags=["skill"])
    transport = httpx.ASGITransport(app=app_for(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(
            "/api/mining/jobs",
            json={
                "name": "long-" + "x" * 115,
                "source_id": source["source_id"],
                "input_dir": SOURCE,
                "max_rounds": 2,
                "human_checkpoints": True,
            },
        )
        assert response.status_code == 201, response.text
    job = response.json()["jobs"][0]
    persisted = rig.repository.get_job(job["job_id"])
    assert persisted["status"] == "preparing" and persisted["rounds_mode"] == "orchestrated"
    assert len(persisted["submission_idempotency_key"]) <= 128
    assert rig.submissions[0]["job_id"] == persisted["job_id"] and not rig.remote.compile_calls
    assert persisted["source_snapshot"].endswith("/rounds/round-1/source")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app_for(rig, admin=False)), base_url="http://test"
    ) as client:
        denied = await client.post(f"/api/mining/jobs/{job['job_id']}/finish", json={})
        assert denied.status_code == 403


@pytest.mark.asyncio
@pytest.mark.parametrize("human", [True, False])
async def test_restart_after_materialization_finishes_round_once_without_recompiling(rig, human):
    submitted = await rig._submit_ov_skill_round(create(rig, rounds=2, human=human))
    rig.remote.complete(submitted, questions=False)
    materialized = await rig._materialize_ov_skill_job(submitted)
    assert materialized["materialized"] and not materialized["rounds_history"]
    crashed = rig.repository.update_job(submitted["job_id"], status="running")
    restarted_repo = OvSkillMiningRepository(rig.repository.state_root, rig.repository.project_root)
    restarted = Owner(restarted_repo, rig.remote)
    await asyncio.gather(restarted._refresh_ov_skill_job(crashed), restarted._refresh_ov_skill_job(crashed))
    recovered = restarted_repo.get_job(crashed["job_id"])
    assert len(recovered["rounds_history"]) == 1 and len(rig.remote.compile_calls) == 1
    archive = restarted_repo.jobs_root / crashed["job_id"] / "rounds/round-1/output"
    assert (archive / "compiled_skill/policy-review/benchmark.json").is_file()
    if human:
        assert recovered["status"] == "waiting_human" and recovered["current_round"] == 1
        assert recovered["pending_checkpoint"] and not recovered["finished_at"]
        assert not restarted.submissions
    else:
        assert recovered["status"] == "preparing" and recovered["current_round"] == 2
        assert not recovered["pending_checkpoint"] and len(restarted.submissions) == 1
        assert restarted.submissions[0]["current_round"] == 2


@pytest.mark.asyncio
async def test_stop_unknown_submission_with_lookup_404_never_creates_remote_compile(rig):
    initial = create(rig)
    pending = rig.repository.update_job(
        initial["job_id"],
        compile_request={"from": [initial["source_snapshot"]], "to": initial["output_uri"]},
        submission_error="Unknown response",
        status="preparing",
    )
    rig.remote.lookup_supported = True
    transport = httpx.ASGITransport(app=app_for(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.post(f"/api/mining/jobs/{initial['job_id']}/stop")
        assert response.status_code == 200 and response.json()["job"]["status"] == "stopping"
    stopped = await rig._submit_ov_skill_round(rig.repository.get_job(initial["job_id"]))
    assert stopped["status"] == "stopped" and stopped["finished_at"] and not stopped["ov_task_id"]
    assert rig.remote.submission_lookups == [pending["submission_idempotency_key"]]
    assert not rig.remote.compile_calls and not rig.remote.published and not rig.remote.cancelled


@pytest.mark.asyncio
async def test_missing_reference_keeps_checkpoint_and_allows_corrected_continue(rig):
    waiting = await complete(rig, create(rig, rounds=2))
    checkpoint = waiting["pending_checkpoint"]["id"]
    payload = {
        "checkpoint_id": checkpoint,
        "answers": {"scope": "Approved regions only."},
        "unresolved": [],
        "references": {"scope": ["viking://resources/team/approved/missing.md"]},
        "feedback": "Recheck scope.",
        "request_id": "missing-reference-attempt",
    }
    transport = httpx.ASGITransport(app=app_for(rig))
    async with httpx.AsyncClient(transport=transport, base_url="http://test") as client:
        failed = await client.post(f"/api/mining/jobs/{waiting['job_id']}/continue", json=payload)
        assert failed.status_code == 404, failed.text
        unchanged = rig.repository.get_job(waiting["job_id"])
        assert unchanged["status"] == "waiting_human" and unchanged["current_round"] == 1
        assert unchanged["pending_checkpoint"]["id"] == checkpoint and not unchanged["supplements"]
        assert not rig.submissions
        corrected = await client.post(
            f"/api/mining/jobs/{waiting['job_id']}/continue",
            json={
                **payload,
                "references": {"scope": [APPROVED]},
                "request_id": "corrected-reference-attempt",
            },
        )
        assert corrected.status_code == 200, corrected.text
    assert rig.repository.get_job(waiting["job_id"])["current_round"] == 2 and len(rig.submissions) == 1


@pytest.mark.asyncio
async def test_remote_partial_output_warns_with_complete_bundle_and_next_round_freezes_new_compiler(rig, monkeypatch):
    compiler_v1 = ov_round_orchestration.SKILL_BODY
    submitted = await rig._submit_ov_skill_round(create(rig, rounds=2))
    rig.remote.complete(submitted, questions=False)
    rig.remote.tasks[submitted["ov_task_id"]].update(
        {
            "result": {
                "warnings": ["partial_output: source coverage incomplete", {"message": "extract worker failed"}]
            },
            "meta": {"pipeline": {"errors": [{"error": "extract worker failed"}]}},
        }
    )
    await rig._refresh_ov_skill_job(submitted)
    waiting = rig.repository.get_job(submitted["job_id"])
    expected = ["partial_output: source coverage incomplete", "extract worker failed"]
    assert waiting["status"] == "waiting_human" and waiting["materialized"]
    assert waiting["remote_warnings"] == expected
    assert waiting["artifact_quality"]["warnings"] == expected and waiting["artifact_quality"]["level"] == "caution"
    public = rig.repository.public_job(waiting, detail=True)
    assert len(public["rounds"][0]["artifacts"]) == 8
    assert public["rounds"][0]["remote_warnings"] == expected
    assert public["rounds"][0]["compiler_source_sha256"] == ov_round_orchestration._digest(compiler_v1)
    assert public["rounds"][0]["compiler_sha256"] == ov_round_orchestration._digest(
        ov_round_orchestration._compiler_package(compiler_v1)[1]
    )
    archive = rig.repository.jobs_root / waiting["job_id"] / "rounds" / "round-1"
    assert json.loads((archive / "meta.json").read_text())["remote_warnings"] == expected
    assert (archive / "input/compiler-source.md").read_text() == ov_round_orchestration._compiler_package(compiler_v1)[
        1
    ]
    assert (archive / "input/compiler-base.md").read_text() == compiler_v1
    assert "compiler_path" not in public

    compiler_v2 = compiler_v1 + "\n# Improved second-round compiler guidance\n"
    rig.compiler_body = compiler_v2
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(rig)), base_url="http://test") as client:
        response = await client.post(
            f"/api/mining/jobs/{waiting['job_id']}/continue",
            json={
                "checkpoint_id": waiting["pending_checkpoint"]["id"],
                "answers": {},
                "unresolved": [],
                "references": {},
                "request_id": "advance-compiler",
            },
        )
        assert response.status_code == 200, response.text
    second = rig.repository.get_job(waiting["job_id"])
    assert not second["remote_warnings"] and not second["compiler_sha256"]
    submitted_second = await rig._submit_ov_skill_round(second)
    assert rig.remote.published[-1]["skill_body"] == ov_round_orchestration._compiler_package(compiler_v2)[1]
    assert submitted_second["compiler_source_sha256"] == ov_round_orchestration._digest(compiler_v2)
    assert submitted_second["compiler_sha256"] == ov_round_orchestration._digest(
        ov_round_orchestration._compiler_package(compiler_v2)[1]
    )
    assert Path(submitted_second["compiler_path"]).read_text() == compiler_v2
    rig.remote.complete(submitted_second, questions=False)
    await rig._refresh_ov_skill_job(submitted_second)
    final_waiting = rig.repository.get_job(waiting["job_id"])
    assert final_waiting["artifact_quality"]["level"] == "ready" and not final_waiting["artifact_quality"]["warnings"]
    final = rig.repository.finish_job(final_waiting["job_id"], final_waiting["pending_checkpoint"]["id"])
    assert final["artifact_quality"]["level"] == "ready"
    assert json.loads((archive / "meta.json").read_text())["remote_warnings"] == expected
    assert (archive / "input/compiler-source.md").read_text() == ov_round_orchestration._compiler_package(compiler_v1)[
        1
    ]
    assert (archive / "input/compiler-base.md").read_text() == compiler_v1


@pytest.mark.asyncio
async def test_before_acceptance_network_failure_retries_with_frozen_round_compiler(rig, monkeypatch):
    initial = create(rig, rounds=1)
    frozen_body = ov_round_orchestration.SKILL_BODY
    write_immutable = rig._round_write_immutable

    async def fail_write_once(account, uri, content):
        raise httpx.ReadTimeout("Connection failed before Compile acceptance")

    monkeypatch.setattr(rig, "_round_write_immutable", fail_write_once)
    pending = await rig._submit_ov_skill_round(initial)
    assert pending["status"] == "preparing" and not pending.get("compile_request") and not rig.remote.compile_calls
    assert rig.remote.published[0]["skill_body"] == ov_round_orchestration._compiler_package(frozen_body)[1]
    rig.compiler_body = frozen_body + "\nChanged during failed submission\n"
    monkeypatch.setattr(rig, "_round_write_immutable", write_immutable)
    accepted = await rig._submit_ov_skill_round(pending)
    assert accepted["status"] == "queued" and len(rig.remote.accepted) == 1
    assert rig.remote.published[-1]["skill_body"] == ov_round_orchestration._compiler_package(frozen_body)[1]
    assert accepted["compiler_source_sha256"] == ov_round_orchestration._digest(frozen_body)
    assert accepted["compiler_sha256"] == ov_round_orchestration._digest(
        ov_round_orchestration._compiler_package(frozen_body)[1]
    )
    assert Path(accepted["compiler_path"]).read_text() == frozen_body
    assert (Path(accepted["input_manifest"]).parent / "compiler-source.md").read_text() == (
        ov_round_orchestration._compiler_package(frozen_body)[1]
    )
    assert (Path(accepted["input_manifest"]).parent / "compiler-base.md").read_text() == frozen_body


def test_native_compile_warning_parser_bounds_deduplicates_and_retains_structured_errors():
    task = {
        "result": {"warnings": [" partial_output ", "partial_output", {"message": "Worker timed out"}]},
        "meta": {"pipeline": {"errors": [{"error": "Worker timed out"}, "Missing source shard"]}},
    }
    assert SkillMinerBridgeMixin._ov_compile_warnings(task) == [
        "partial_output",
        "Worker timed out",
        "Missing source shard",
    ]
    assert SkillMinerBridgeMixin._ov_compile_warnings({"result": {"warnings": "partial_output"}}) == ["partial_output"]
    many = {"result": {"warnings": [f"Warning {n}: " + "x" * 10_000 for n in range(100)]}}
    warnings = SkillMinerBridgeMixin._ov_compile_warnings(many)
    assert len(warnings) <= 20 and all(len(warning) <= 4000 for warning in warnings)
    assert len(warnings) == len(set(warnings))


@pytest.mark.asyncio
async def test_large_revision_context_is_bounded_explicitly_partial_and_fully_archived(rig):
    submitted = await rig._submit_ov_skill_round(create(rig, rounds=2))
    rig.remote.complete(submitted, questions=False)
    skill_uri = submitted["output_uri"] + "/compiled_skill/policy-review/SKILL.md"
    huge_skill = rig.remote.content[skill_uri] + "\n" + "Prior candidate claim. " * 50_000 + "\nFULL_CANDIDATE_TAIL"
    rig.remote.content[skill_uri] = huge_skill
    review_questions = [
        {
            "qid": f"q-{index:02d}",
            "question": f"Question {index} " + "Which region is supported? " * 120,
            "reason": "Missing scope confirmation.",
            "required": False,
            "source_refs": [submitted["source_snapshot"] + "/original/policy.md"],
        }
        for index in range(1, 51)
    ]
    review_uri = submitted["output_uri"] + "/analysis/review.json"
    rig.remote.content[review_uri] = json.dumps(
        {"schema_version": REVIEW_SCHEMA_VERSION, "questions": review_questions}
    )
    await rig._refresh_ov_skill_job(submitted)
    waiting = rig.repository.get_job(submitted["job_id"])
    feedback = ("Feedback: confirm regional boundaries. " * 400).strip()
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app_for(rig)), base_url="http://test") as client:
        response = await client.post(
            f"/api/mining/jobs/{waiting['job_id']}/continue",
            json={
                "checkpoint_id": waiting["pending_checkpoint"]["id"],
                "answers": {"q-01": "Approved regions only."},
                "unresolved": [],
                "references": {},
                "feedback": feedback,
                "request_id": "large-context",
            },
        )
        assert response.status_code == 200, response.text
    second = await rig._submit_ov_skill_round(rig.repository.get_job(waiting["job_id"]))
    payload = second["compile_request"]
    assert payload["from"] == [second["source_snapshot"]]
    assert second["round_context_uri"] not in payload["from"]
    instruction = payload["instruction"]
    assert len(instruction) <= 32_000
    revision = json.loads(instruction.rsplit("\n", 1)[-1])
    assert revision["previous_candidates"][0]["previous_skill_body_truncated"]
    assert revision["review_question_text_truncated"]
    assert revision["feedback_truncated"]
    assert {row["qid"] for row in revision["review_questions"]} | set(revision["omitted_review_qids"]) == {
        row["qid"] for row in review_questions
    }
    assert len(revision["previous_candidates"][0]["case_ids"]) == 12
    assert "FULL_CANDIDATE_TAIL" not in instruction
    archive = rig.repository.jobs_root / waiting["job_id"] / "rounds" / "round-1"
    assert (archive / "output/compiled_skill/policy-review/SKILL.md").read_text() == huge_skill
    assert (archive / "reviewed_output/compiled_skill/policy-review/SKILL.md").read_text() == huge_skill
    persisted_review = json.loads((archive / "checkpoint_submission.json").read_text())
    assert (
        persisted_review["feedback"] == feedback
        and persisted_review["questions"][0]["question"] == review_questions[0]["question"]
    )
    assert rig.remote.content[second["round_context_uri"] + "/compiled_skill/policy-review/SKILL.md"] == huge_skill
    human_source = rig.remote.content[second["source_snapshot"] + "/supplements/round-1/answers.md"]
    assert "Approved regions only." in human_source and review_questions[0]["question"] in human_source
    assert "clarification_question_not_a_fact" in human_source and "FULL_CANDIDATE_TAIL" not in human_source


@pytest.mark.asyncio
async def test_other_job_compiler_update_cannot_change_unknown_acceptance_retry_pin(rig, monkeypatch):
    source_v1 = ov_round_orchestration.SKILL_BODY
    name_v1, body_v1, hash_v1 = ov_round_orchestration._compiler_package(source_v1)
    rig.remote.lose_next_acceptance = True
    pending = await rig._submit_ov_skill_round(create(rig, name="First immutable pin"))
    uri_v1 = f"viking://agent/skills/{name_v1}"
    assert pending["compile_request"]["skill"] == uri_v1
    assert rig.remote.compiler_skills[name_v1] == body_v1

    source_v2 = source_v1 + "\nImproved compiler guidance for another job.\n"
    rig.compiler_body = source_v2
    other = await rig._submit_ov_skill_round(create(rig, name="Different immutable pin"))
    assert other["compiler_uri"] != uri_v1 and other["compiler_source_sha256"] != hash_v1
    assert rig.remote.compiler_skills[name_v1] == body_v1
    assert rig.remote.compile_calls[1][1]["skill"] == other["compiler_uri"]

    # Submission lookup is deliberately 404; fallback POST must retain the
    # original URI even after another job installed a different compiler.
    recovered = await rig._submit_ov_skill_round(rig.repository.get_job(pending["job_id"]))
    assert recovered["status"] == "queued"
    assert recovered["compile_request"] == pending["compile_request"]
    assert recovered["compiler_uri"] == uri_v1 and recovered["skill_uri"] == uri_v1
    assert rig.remote.compile_calls[-1][1]["skill"] == uri_v1
    assert rig.remote.compiler_skills[name_v1] == body_v1
    assert len(rig.remote.published) == 2 and len(rig.remote.accepted) == 2
    assert rig.remote.submission_lookups[-1] == pending["submission_idempotency_key"]


@pytest.mark.asyncio
async def test_tampered_existing_compiler_pin_is_rejected_without_install_or_put(rig):
    name, body, _ = ov_round_orchestration._compiler_package(ov_round_orchestration.SKILL_BODY)
    tampered = body + "\nDifferent bytes under the same pin\n"
    rig.remote.compiler_skills[name] = tampered
    failed = await rig._submit_ov_skill_round(create(rig))
    assert failed["status"] == "failed" and "内容不一致" in failed["error"]
    assert not rig.remote.published and not rig.remote.compile_calls
    assert rig.remote.compiler_skills[name] == tampered


@pytest.mark.asyncio
async def test_same_content_compiler_is_reused_without_overwriting_shared_skill(rig):
    first = await rig._submit_ov_skill_round(create(rig, name="Reuse pin first"))
    second = await rig._submit_ov_skill_round(create(rig, name="Reuse pin second"))
    assert first["status"] == second["status"] == "queued"
    assert first["compiler_uri"] == second["compiler_uri"]
    assert first["compiler_sha256"] == second["compiler_sha256"]
    assert len(rig.remote.published) == 1 and len(rig.remote.accepted) == 2


@pytest.mark.asyncio
async def test_same_content_install_race_verifies_winner_without_overwrite(rig, monkeypatch):
    original_install = FakeCompileClient.install_skill

    async def install_after_peer_wins(client, **kwargs):
        client.remote.compiler_skills[kwargs["skill_name"]] = kwargs["skill_body"]
        return await original_install(client, **kwargs)

    monkeypatch.setattr(FakeCompileClient, "install_skill", install_after_peer_wins)
    accepted = await rig._submit_ov_skill_round(create(rig))
    assert accepted["status"] == "queued"
    name, body, _ = ov_round_orchestration._compiler_package(ov_round_orchestration.SKILL_BODY)
    assert accepted["compile_request"]["skill"] == f"viking://agent/skills/{name}"
    assert rig.remote.compiler_skills[name] == body
    assert rig.remote.compiler_reads.count(name) == 2
    assert len(rig.remote.published) == 1


@pytest.mark.asyncio
async def test_unknown_acceptance_retry_restores_missing_original_pin_from_frozen_source(rig, monkeypatch):
    source_body = ov_round_orchestration.SKILL_BODY
    name, pinned_body, _ = ov_round_orchestration._compiler_package(source_body)
    rig.remote.lose_next_acceptance = True
    pending = await rig._submit_ov_skill_round(create(rig))
    del rig.remote.compiler_skills[name]
    rig.compiler_body = source_body + "\nChanged future compiler.\n"
    recovered = await rig._submit_ov_skill_round(rig.repository.get_job(pending["job_id"]))
    assert recovered["status"] == "queued" and recovered["ov_task_id"] == "compile-1"
    assert recovered["compile_request"] == pending["compile_request"]
    assert recovered["compiler_uri"] == pending["compiler_uri"]
    assert recovered["compiler_sha256"] == pending["compiler_sha256"]
    assert rig.remote.compiler_skills[name] == pinned_body
    assert len(rig.remote.published) == 2
    assert rig.remote.published[-1]["skill_body"] == pinned_body
    assert len(rig.remote.accepted) == 1 and rig.remote.compile_calls[0] == rig.remote.compile_calls[1]


@pytest.mark.asyncio
async def test_unknown_acceptance_retry_rejects_tampered_pin_before_reposting_compile(rig):
    name, pinned_body, _ = ov_round_orchestration._compiler_package(ov_round_orchestration.SKILL_BODY)
    rig.remote.lose_next_acceptance = True
    pending = await rig._submit_ov_skill_round(create(rig))
    tampered = pinned_body + "\nTampered after lost acceptance\n"
    rig.remote.compiler_skills[name] = tampered
    rejected = await rig._submit_ov_skill_round(rig.repository.get_job(pending["job_id"]))
    assert rejected["status"] == "failed" and "内容不一致" in rejected["error"]
    assert rejected["compile_request"] == pending["compile_request"]
    assert len(rig.remote.compile_calls) == 1 and len(rig.remote.published) == 1
    assert rig.remote.compiler_skills[name] == tampered
    assert rig.remote.submission_lookups[-1] == pending["submission_idempotency_key"]


@pytest.mark.asyncio
async def test_chinese_original_language_survives_english_prior_review_and_live_source_changes(rig):
    original = "包裹受理应先核对原订单与付款凭证，再检查重量和尺寸；材料缺失时暂停并转人工。"
    rig.remote.content[f"{SOURCE}/policy.md"] = original
    waiting = await complete(rig, create(rig, rounds=2))
    policy = {"version": 1, "basis": "frozen_original_source", "language": "zh-CN"}
    assert waiting["output_language_policy"] == policy
    assert "所有轮次新生成的面向人文字必须使用简体中文" in waiting["compile_request"]["instruction"]
    archive = rig.repository.jobs_root / waiting["job_id"] / "rounds/round-1"
    prior = (archive / "output/compiled_skill/policy-review/SKILL.md").read_text()
    assert "description: Apply sourced policy." in prior
    assert json.loads((archive / "input.json").read_text())["output_language_policy"] == policy
    assert json.loads((archive / "meta.json").read_text())["output_language_policy"] == policy
    rig.remote.content[f"{SOURCE}/policy.md"] = "The changed live source must not replace the original Evidence."
    second = rig.repository.prepare_next_round(
        waiting["job_id"], waiting["pending_checkpoint"]["id"],
        answers={"scope": "Only use approved regions."}, unresolved=[], references={},
        feedback="Keep the previous candidate's scope and check the supplied answer.",
    )
    submitted = await rig._submit_ov_skill_round(second)
    assert submitted["output_language_policy"] == policy
    instruction = submitted["compile_request"]["instruction"]
    assert "必须使用简体中文" in instruction
    assert instruction.index("TE output_language_policy:") < instruction.index("下面 JSON 是上一轮")
    revision = json.loads(instruction.rsplit("\n", 1)[-1])
    assert revision["previous_candidates"][0]["previous_skill_body"] == prior
    assert revision["previous_candidates"][0]["skill_name"] == "policy-review"
    assert revision["previous_candidates"][0]["case_ids"] == [f"policy-review-{i:03d}" for i in range(12)]
    assert rig.remote.content[submitted["source_snapshot"] + "/original/policy.md"] == original
    assert "Only use approved regions." in rig.remote.content[
        submitted["source_snapshot"] + "/supplements/round-1/answers.md"
    ]
    assert submitted["compile_request"]["from"] == [submitted["source_snapshot"]]
    assert submitted["round_context_uri"] not in submitted["compile_request"]["from"]


@pytest.mark.asyncio
async def test_language_policy_and_request_survive_lost_acceptance_and_restart(rig):
    rig.remote.content[f"{SOURCE}/policy.md"] = (
        "退货申请应核对订单和付款凭证，未拆封状态须明确；异常情况暂停处理并转人工。"
    )
    rig.remote.lose_next_acceptance = True
    pending = await rig._submit_ov_skill_round(create(rig))
    assert pending["output_language_policy"]["language"] == "zh-CN"
    rig.remote.content[f"{SOURCE}/policy.md"] = "The user must verify the policy before accepting the parcel."
    restarted = Owner(OvSkillMiningRepository(rig.repository.state_root, rig.repository.project_root), rig.remote)
    restarted.compiler_body += "\nDifferent language guidance must not change a frozen retry.\n"
    resumed = await restarted._submit_ov_skill_round(restarted.repository.get_job(pending["job_id"]))
    assert resumed["status"] == "queued"
    assert resumed["output_language_policy"] == pending["output_language_policy"]
    assert resumed["compile_request"] == pending["compile_request"]
    assert resumed["compiler_uri"] == pending["compiler_uri"]
    assert resumed["submission_idempotency_key"] == pending["submission_idempotency_key"]
    assert len(rig.remote.compile_calls) == 2 and rig.remote.compile_calls[0] == rig.remote.compile_calls[1]


@pytest.mark.asyncio
async def test_existing_frozen_request_without_language_policy_is_reposted_unchanged(rig, monkeypatch):
    job = create(rig)
    frozen = {
        "from": [job["source_snapshot"]], "to": job["output_uri"], "skill": job["skill_uri"],
        "instruction": "Frozen request from before output language selection.",
    }
    job = rig.repository.update_job(job["job_id"], compile_request=frozen)

    async def no_refreeze(_job):
        raise AssertionError("A frozen request must not read source text or choose a new language")

    monkeypatch.setattr(rig, "_freeze_round_inputs", no_refreeze)
    accepted = await rig._submit_ov_skill_round(job)
    assert accepted["status"] == "queued" and not accepted.get("output_language_policy")
    assert accepted["compile_request"] == frozen
    assert rig.remote.compile_calls == [(job["submission_idempotency_key"], frozen)]


@pytest.mark.asyncio
async def test_old_job_without_policy_chooses_next_round_language_from_frozen_original_source(rig):
    rig.remote.content[f"{SOURCE}/policy.md"] = (
        "包裹需要核对订单号、尺寸和付款凭证，异常必须转人工，不得承诺未证实的运费。"
    )
    waiting = await complete(rig, create(rig, rounds=2), questions=False)
    # Represent a pre-policy job, retaining its previously frozen source and output.
    path = rig.repository.jobs_root / waiting["job_id"] / "job.json"
    old = json.loads(path.read_text())
    old.pop("output_language_policy")
    path.write_text(json.dumps(old), encoding="utf-8")
    rig.remote.content[f"{SOURCE}/policy.md"] = "The user must check the newer live policy before shipping."
    second = rig.repository.prepare_next_round(
        waiting["job_id"], waiting["pending_checkpoint"]["id"], answers={}, unresolved=[], references={},
    )
    submitted = await rig._submit_ov_skill_round(second)
    assert submitted["output_language_policy"]["language"] == "zh-CN"
    assert "必须使用简体中文" in submitted["compile_request"]["instruction"]
