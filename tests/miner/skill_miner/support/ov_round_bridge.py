# ruff: noqa: F401
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

from team_miner.skill_miner import ov_round_orchestration
from team_miner.skill_miner.ov_compile_mining import (
    REVIEW_SCHEMA_VERSION,
    RUNS_ROOT,
    SKILL_NAME,
    OvSkillMiningRepository,
)
from tests.miner.skill_miner.fixtures import MemoryMetadataBackend, build_capability_plan, build_document
from tests.miner.skill_miner.support.host import DomainHost

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
        self.capabilities = {"configured": True, "can_create": True}
        self.capability_reads = 0
        self.content_reads = []

    def tree(self, uri):
        entries = [
            {"uri": key, "is_dir": False, "size": len(value.encode())}
            for key, value in sorted(self.content.items())
            if key.startswith(uri.rstrip("/") + "/")
        ]
        return {"exists": bool(entries), "entries": entries}

    async def request(self, account, method, path, **kwargs):
        assert account == "default"
        if path == "/api/v1/fs/ls":
            assert method == "GET" and kwargs["params"]["recursive"] == "false"
            parent = kwargs["params"]["uri"].rstrip("/") + "/"
            entries = {}
            for uri, content in sorted(self.content.items()):
                if not uri.startswith(parent):
                    continue
                relative = uri[len(parent) :]
                name = relative.split("/", 1)[0]
                child = parent + name
                entries[child] = {
                    "uri": child,
                    "name": name,
                    "isDir": "/" in relative,
                    "size": 0 if "/" in relative else len(content.encode()),
                }
            rows = sorted(entries.values(), key=lambda row: (not row["isDir"], row["name"].lower(), row["name"]))
            offset = kwargs["params"]["offset"]
            return rows[offset : offset + kwargs["params"]["node_limit"]]
        if path == "/api/v1/compile/capabilities":
            self.capability_reads += 1
            if self.capabilities is None:
                raise HTTPException(404, "Capability probe unavailable")
            return copy.deepcopy(self.capabilities)
        if path == "/api/v1/content/read":
            uri = kwargs["params"]["uri"]
            self.content_reads.append(uri)
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

class Owner(DomainHost):
    config = SimpleNamespace()

    def __init__(self, repository, remote):
        self.repository = repository
        self.remote = remote
        self.config = SimpleNamespace(_config_file=str(repository.state_root / "test-config.yaml"))
        self.submissions = []
        self.refreshes = []
        # This fixture exercises the existing one-request/custom compiler path.
        # Public-API staged execution has separate end-to-end transport fixtures.
        self.compiler_body = ov_round_orchestration.SKILL_BODY.replace("te-staged-compile.v1", "fixture-custom-compile")

    def _ov_skill_mining_repository(self):
        return self.repository

    def _ov_skill_compiler_body(self):
        return self.compiler_body

    def _round_client(self, job):
        return FakeCompileClient(self.remote)

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
    # These fixtures model one public Compile request. Keep their frozen compiler
    # consistent while the staged default is exercised with its own transport.
    monkeypatch.setattr(
        ov_round_orchestration,
        "SKILL_BODY",
        ov_round_orchestration.SKILL_BODY.replace("te-staged-compile.v1", "fixture-custom-compile"),
    )
    repository = OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner",
        backend=MemoryMetadataBackend.shared(tmp_path / "state"))
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

async def _large_reviewed_candidate_collection(rig, count=160):
    """Expand the reviewed archive, preserving a real frozen first-round source."""
    second = await complete(rig, create(rig, rounds=2, human=False), questions=False)
    archive = rig.repository.jobs_root / second["job_id"] / "rounds/round-1/reviewed_output"
    names = [f"task-{index:04d}-" + "a" * 69 for index in range(count)]
    source = second["original_source_snapshot"] + "/original/policy.md"
    plan = build_capability_plan(names, source)
    (archive / "semantic_reports/capability-plan.json").write_text(json.dumps(plan), encoding="utf-8")
    # The original candidate is replaced by a reviewed collection. Every file
    # remains in this archive; the second round must not copy them as Evidence.
    original = archive / "compiled_skill/policy-review"
    for path in original.iterdir():
        path.unlink()
    original.rmdir()
    for name in names:
        candidate = archive / "compiled_skill" / name
        candidate.mkdir()
        (candidate / "SKILL.md").write_text(f"---\nname: {name}\n---\n" + "方法正文。" * 7000, encoding="utf-8")
        (candidate / "benchmark.json").write_text(
            json.dumps({"cases": [{"case_id": f"{name}-{case:03d}"} for case in range(24)]}), encoding="utf-8"
        )
        (candidate / "EVALUATION.md").write_text("# Evaluation", encoding="utf-8")
        (candidate / "BENCHMARK.md").write_text("# Test Dataset", encoding="utf-8")
    return second, archive, names
