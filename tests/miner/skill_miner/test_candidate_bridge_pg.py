"""Real PG mining-to-candidate handoff: complete bytes, isolation and Worker fences."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
from contextlib import asynccontextmanager
from types import SimpleNamespace

import pytest

from team_miner.skill_miner import candidate_bridge
from team_miner.skill_miner.cache import restore_cache
from team_miner.skill_miner.candidate_materials import inline_path
from team_miner.skill_miner.dataset_view import dataset_markdown
from team_miner.skill_miner.mining_completion import POLICY
from team_miner.skill_miner.repository import MiningRevisionConflict, make_repository
from team_miner.skill_miner.storage import PgMiningFileStore
from team_replay.contracts import skill_replay_spec
from team_replay.integration import FixedHost
from team_replay.runner import evaluate_spec
from team_skills.candidates.repository import CandidateRepository, CandidateStateError
from team_skills.config import BundleLimits
from team_skills.evolution.history import candidate_payload, skill_payload
from team_skills.library.bundle import bundle_tree_sha256
from team_skills.library.repository import SkillLibraryRepository
from teamEvolver.config_runtime import ConfigRepository
from teamEvolver.runtime.services import build_services
from tests.support.postgres import _run
from tests.support.replay import Host, judge
from tests.support.skills import _skill_md

from .fixtures import build_capability_plan, build_document

pytestmark = pytest.mark.postgres
SOURCE = "viking://resources/documents"


def engine(env, tmp_path, *, tenant="alpha", command="submit-1"):
    services = build_services(role="worker", tx=env.tx, extras={
        "config_repo": ConfigRepository(env.tx), "skill_mining_cache_root": tmp_path,
    })

    async def claim(conn):
        await services.tasks.enqueue(conn, tenant_id=tenant, task_id=command, kind="miner.submit-candidate")
        return await services.tasks.claim(conn, owner_id=command + "-owner", lease_seconds=600)

    task = _run(env, tenant, claim)
    repository = make_repository(services, tenant, task)

    async def prepare():
        return worker

    @asynccontextmanager
    async def job_lock(_job_id):
        yield

    async def restore(job_id):
        await asyncio.to_thread(restore_cache, repository, job_id)

    worker = SimpleNamespace(services=services, tenant=tenant, task=task, repository=repository,
                             prepare=prepare, job_lock=job_lock, restore=restore, settings={})
    return worker


def seed(worker, *, job_id="job-1", name="demo", hostile=False, changes=None, materials=None):
    source = SOURCE + "/source.md"
    dataset = build_document(name, [{"id": f"case-{n}", "query": f"Complete task {n}",
                                   "source_session_ids": [source]} for n in range(12)])
    if materials is not None:
        dataset["cases"][0]["materials"] = materials
    if hostile:
        dataset["cases"][0]["provenance"].update(
            session_id="fabricated-session", session_snapshot={"private": "untrusted"},
            source_runtime={"type": "compiler-chosen"}, source_runtime_context={"user_id": "another-user"},
            context_snapshot={"items": [{"content": "fabricated private Context"}]},
            context_snapshot_id="forged-context", runtime_context={"tenant_id": "another-tenant"},
        )
        dataset["cases"][0]["replay"]["execution"] = {"kind": "arbitrary", "adapter_id": "compiler-selected"}
    bundle = {
        "SKILL.md": (f"---\nname: {name}\ndescription: >\n  Perform a complete\n  documented task.\n"
                     "allowed-tools:\n  - Read\n---\n\n# 方法\n\n执行已记录的步骤。\n").encode(),
        "EVALUATION.md": b"# Evaluation\nReview the observed task.",
        "benchmark.json": json.dumps(dataset, ensure_ascii=False).encode(),
        "BENCHMARK.md": dataset_markdown(dataset).encode(),
        "METHOD.json": json.dumps({"name": name, "source_refs": [source]}).encode(),
        "references/rules.md": "完整来源规则，不丢失参数。".encode(),
        "scripts/run.py": b"print('reviewed source')\n",
    }
    job = {"job_id": job_id, "name": "Document mining", "status": "succeeded", "account": "team",
           "workspace": f"mining_jobs/{job_id}/workspace", "source_uri": SOURCE,
           "candidate_contract_version": 2, "execution_protocol": "te-native-mining.v1",
           "current_round": 1, "max_rounds": 1, "content_validated": True,
           **(changes or {})}
    files = {f"workspace/compiled_skill/{name}/{path}": content for path, content in bundle.items()}
    files["workspace/semantic_reports/capability-plan.json"] = json.dumps(
        build_capability_plan([name], SOURCE),
    ).encode()
    worker.repository.backend.save_job(job)
    PgMiningFileStore(worker.repository.backend).save(job_id, files)
    return bundle, files


def submit(env, worker, *, name="demo", job_id="job-1", **body):
    async def accepted(conn):
        row = await conn.fetchrow(
            f"SELECT revision,files_revision FROM {env.schema}.skill_mining_jobs WHERE job_id=$1", job_id,
        )
        if row is None:
            raise KeyError(job_id)
        return {"_accepted_job_revision": int(row["revision"]),
                "_accepted_files_revision": int(row["files_revision"])}
    versions = _run(env, worker.tenant, accepted)
    return env.tx.pool.run(candidate_bridge.submit(
        worker, {"skill_name": name, **versions, **body}, job_id, "reviewer",
    ))


def read(env, worker, candidate_id):
    async def load(conn):
        repository = CandidateRepository(env.tx)
        item = await repository.require(conn, candidate_id)
        return item, await repository.read_bundle(conn, candidate_id), await repository.events(conn, candidate_id)
    return _run(env, worker.tenant, load)


def test_complete_bundle_is_reviewable_without_publishing_or_trusting_compiler_runtime(pg_env, tmp_path):
    worker = engine(pg_env, tmp_path)
    original, _ = seed(worker, hostile=True)
    result = submit(pg_env, worker, artifact_path="mining_jobs/job-1/workspace/compiled_skill/demo/SKILL.md")
    item, bundle, events = read(pg_env, worker, result["job_id"])
    assert result["created"] is True and item["status"] == "open" and item["revision"] == 1
    assert result["question_count"] == 12 and len(item["detail"]["replay_cases"]) == 12
    assert set(bundle) == set(original)
    assert {p: value for p, value in bundle.items() if p != "SKILL.md"} == {
        p: value for p, value in original.items() if p != "SKILL.md"
    }
    assert b"description: Perform a complete documented task." in bundle["SKILL.md"]
    assert b"allowed-tools:" in bundle["SKILL.md"]
    assert b"description: >" in PgMiningFileStore(worker.repository.backend).load("job-1")[
        "workspace/compiled_skill/demo/SKILL.md"
    ]
    case = item["detail"]["replay_cases"][0]
    assert not (set(case["provenance"]) & candidate_bridge._PRIVATE_SOURCE_FIELDS)
    assert case["provenance"]["session_ids"] == [SOURCE + "/source.md"]
    assert set(case["replay"]) == {"progressive_disclosure"}
    assert item["detail"]["runtime_validation_policy"] == candidate_bridge.DOCUMENT_POLICY
    assert [event["kind"] for event in events] == ["created"]
    assert _run(pg_env, "alpha", lambda c: SkillLibraryRepository(pg_env.tx).list_skills(c)) == []
    payload = _run(pg_env, "alpha", lambda c: candidate_payload(c, pg_env.tx, item))
    assert payload["candidate_skill"]["bundle"] and payload["review_status"] == "open"
    # This local factory validates compatibility, not production model behavior.
    host = Host()
    fixed = FixedHost(host.resolve_replay_factory("tenant-selected", {}), {})
    spec = skill_replay_spec({**item["detail"], "job_id": item["candidate_id"],
                              "candidate_skill": skill_payload("demo", bundle)})
    replayed = evaluate_spec(spec, host=fixed, judge=judge, case_index=0)
    assert replayed["status"] == "evaluated" and replayed["runtime_type"] == "document"
    assert len(host.contexts) == 2


def test_retry_on_a_new_replica_returns_the_same_candidate_without_revision_or_event_changes(pg_env, tmp_path):
    first = engine(pg_env, tmp_path, command="submit-1")
    seed(first)
    created = submit(pg_env, first)
    second = engine(pg_env, tmp_path, command="submit-2")
    assert first.repository.project_root != second.repository.project_root
    again = submit(pg_env, second)
    item, _, events = read(pg_env, second, again["job_id"])
    assert again["created"] is False and again["job_id"] == created["job_id"]
    assert item["revision"] == 1 and len(events) == 1


def test_concurrent_workers_create_one_candidate_and_never_revise_it(pg_env, tmp_path):
    first = engine(pg_env, tmp_path, command="submit-1")
    seed(first)
    second = engine(pg_env, tmp_path, command="submit-2")

    async def run():
        return await asyncio.gather(
            candidate_bridge.submit(first, {"skill_name": "demo", "_accepted_job_revision": 1,
                                            "_accepted_files_revision": 1}, "job-1", "one"),
            candidate_bridge.submit(second, {"skill_name": "demo", "_accepted_job_revision": 1,
                                             "_accepted_files_revision": 1}, "job-1", "two"),
        )
    results = pg_env.tx.pool.run(run())
    assert sorted(result["created"] for result in results) == [False, True]
    assert results[0]["job_id"] == results[1]["job_id"]
    item, _, events = read(pg_env, first, results[0]["job_id"])
    assert item["revision"] == 1 and len(events) == 1


def test_identical_mining_ids_remain_isolated_between_tenants(pg_env, tmp_path):
    alpha = engine(pg_env, tmp_path, tenant="alpha")
    seed(alpha)
    alpha_result = submit(pg_env, alpha)
    beta = engine(pg_env, tmp_path, tenant="beta")
    with pytest.raises(KeyError):
        submit(pg_env, beta)
    assert _run(pg_env, "beta", lambda c: CandidateRepository(pg_env.tx).get(c, alpha_result["job_id"])) is None
    seed(beta)
    beta_result = submit(pg_env, beta)
    assert beta_result["created"] is True
    assert read(pg_env, alpha, alpha_result["job_id"])[0]["revision"] == 1
    assert read(pg_env, beta, beta_result["job_id"])[0]["revision"] == 1


@pytest.mark.parametrize("stop", ["expire", "cancel"])
def test_a_stale_or_cancelled_worker_cannot_create_a_candidate(pg_env, tmp_path, stop):
    worker = engine(pg_env, tmp_path)
    seed(worker)

    async def stop_task(conn):
        if stop == "expire":
            await conn.execute(f"UPDATE {pg_env.schema}.worker_tasks SET lease_until=now()-interval '1 second'")
            newer = await worker.services.tasks.claim(conn, owner_id="replacement", lease_seconds=600)
            assert newer.fence > worker.task.fence
        else:
            await worker.services.tasks.request_cancel(conn, task_id=worker.task.task_id)
    _run(pg_env, "alpha", stop_task)
    with pytest.raises(RuntimeError, match="lost its lease|cancelled"):
        submit(pg_env, worker)
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).list(c))[1] == 0


@pytest.mark.parametrize("missing", ["EVALUATION.md", "benchmark.json", "BENCHMARK.md"])
def test_incomplete_selected_bundles_are_rejected_before_any_candidate_write(pg_env, tmp_path, missing):
    worker = engine(pg_env, tmp_path)
    _, files = seed(worker)
    del files[f"workspace/compiled_skill/demo/{missing}"]
    PgMiningFileStore(worker.repository.backend).save("job-1", files)
    with pytest.raises(ValueError):
        submit(pg_env, worker)
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).list(c))[1] == 0


def test_completion_only_deliverables_cannot_be_presented_as_validated_candidates(pg_env, tmp_path):
    worker = engine(pg_env, tmp_path)
    seed(worker, changes={"completion_policy": POLICY})
    with pytest.raises(ValueError, match="未执行内容验收"):
        submit(pg_env, worker)


def test_existing_unrelated_live_candidate_is_never_overwritten(pg_env, tmp_path):
    worker = engine(pg_env, tmp_path)
    seed(worker)

    async def create_other(conn):
        return await CandidateRepository(pg_env.tx).create(
            conn, tenant_id="alpha", skill_name="demo", bundle={"SKILL.md": _skill_md("demo", "Human edits")},
            limits=BundleLimits(), detail={"source": {"kind": "evolution"}},
        )
    previous = _run(pg_env, "alpha", create_other)
    with pytest.raises(CandidateStateError, match="已有待评审候选"):
        submit(pg_env, worker)
    item, bundle, events = read(pg_env, worker, previous["candidate_id"])
    assert item["revision"] == 1 and b"Human edits" in bundle["SKILL.md"] and len(events) == 1


def test_changed_pg_snapshot_cannot_be_committed_after_validation(pg_env, tmp_path, monkeypatch):
    worker = engine(pg_env, tmp_path)
    _, files = seed(worker)
    original = candidate_bridge._prepare

    def race(*args):
        result = original(*args)
        files["workspace/compiled_skill/demo/references/rules.md"] = b"changed after validation"
        PgMiningFileStore(worker.repository.backend).save("job-1", files)
        return result
    monkeypatch.setattr(candidate_bridge, "_prepare", race)
    with pytest.raises(MiningRevisionConflict, match="changed"):
        submit(pg_env, worker)
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).list(c))[1] == 0


def test_disposable_cache_edits_cannot_replace_the_authoritative_pg_bundle(pg_env, tmp_path):
    worker = engine(pg_env, tmp_path)
    seed(worker)
    restore = worker.restore

    async def altered(job_id):
        await restore(job_id)
        path = worker.repository.project_root / "mining_jobs/job-1/workspace/compiled_skill/demo/SKILL.md"
        path.write_bytes(_skill_md("demo", "Injected only into a local cache"))
    worker.restore = altered
    result = submit(pg_env, worker)
    _, bundle, _ = read(pg_env, worker, result["job_id"])
    assert b"Injected" not in bundle["SKILL.md"] and "执行已记录" in bundle["SKILL.md"].decode()


def test_compiler_cannot_choose_a_runtime_through_submission_fields(pg_env, tmp_path):
    worker = engine(pg_env, tmp_path)
    seed(worker)
    with pytest.raises(ValueError, match="only skill_name and artifact_path"):
        submit(pg_env, worker, runtime={"type": "compiler-selected"})
    with pytest.raises(ValueError, match="artifact_path"):
        submit(pg_env, worker, artifact_path="../../other-tenant/SKILL.md")


@pytest.mark.parametrize("state", ["validating", "rejected"])
def test_repeat_submission_never_resets_an_existing_review_state(pg_env, tmp_path, state):
    worker = engine(pg_env, tmp_path)
    seed(worker)
    first = submit(pg_env, worker)

    async def transition(conn):
        return await CandidateRepository(pg_env.tx).transition(
            conn, tenant_id="alpha", candidate_id=first["job_id"], to_status=state,
            actor="reviewer", validate_task_id="validation-1" if state == "validating" else None,
        )
    _run(pg_env, "alpha", transition)
    again = submit(pg_env, worker)
    item, _, events = read(pg_env, worker, again["job_id"])
    assert again["created"] is False and item["status"] == state
    assert item["revision"] == 1 and len(events) == 2


def test_one_incomplete_sibling_does_not_block_a_complete_independent_candidate(pg_env, tmp_path):
    worker = engine(pg_env, tmp_path)
    _, files = seed(worker)
    files["workspace/semantic_reports/capability-plan.json"] = json.dumps(
        build_capability_plan(["demo", "incomplete"], SOURCE),
    ).encode()
    files["workspace/compiled_skill/incomplete/SKILL.md"] = b"unfinished sibling"
    PgMiningFileStore(worker.repository.backend).save("job-1", files)
    assert submit(pg_env, worker)["created"] is True


def test_worker_fence_is_rechecked_after_validation_and_before_candidate_mutation(pg_env, tmp_path, monkeypatch):
    worker = engine(pg_env, tmp_path)
    seed(worker)
    original = candidate_bridge._prepare

    def expire(*args):
        result = original(*args)

        async def reclaim(conn):
            await conn.execute(f"UPDATE {pg_env.schema}.worker_tasks SET lease_until=now()-interval '1 second'")
            await worker.services.tasks.claim(conn, owner_id="replacement", lease_seconds=600)
        _run(pg_env, "alpha", reclaim)
        return result
    monkeypatch.setattr(candidate_bridge, "_prepare", expire)
    with pytest.raises(RuntimeError, match="lost its lease"):
        submit(pg_env, worker)
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).list(c))[1] == 0


@pytest.mark.parametrize("changed", ["metadata", "files"])
def test_candidate_uses_only_the_snapshot_accepted_by_the_api(pg_env, tmp_path, changed):
    worker = engine(pg_env, tmp_path)
    _, files = seed(worker)
    if changed == "files":
        files["workspace/compiled_skill/demo/references/rules.md"] = b"a newer artifact version"
        PgMiningFileStore(worker.repository.backend).save("job-1", files)
    else:
        value = worker.repository.backend.load_job("job-1")
        worker.repository.backend.save_job({**value, "new_review": True})
    with pytest.raises(MiningRevisionConflict, match="after acceptance"):
        submit(pg_env, worker, _accepted_job_revision=1, _accepted_files_revision=1)
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).list(c))[1] == 0


@pytest.mark.parametrize("material", [
    {"content": "  原文中的场景\n原文中的对话：保留标点和换行。\n"},
    {"name": "scenario", "content": "原文中的场景", "visibility": "initial", "mime_type": "text/plain"},
    {"name": "empty attachment", "content": ""},
])
def test_inline_materials_are_real_pg_files_and_loaded_into_both_replay_branches(pg_env, tmp_path, material):
    worker = engine(pg_env, tmp_path)
    original, source_files = seed(worker, materials=[material])
    result = submit(pg_env, worker)
    item, bundle, _ = read(pg_env, worker, result["job_id"])
    content = material["content"].encode()
    path = inline_path("case-0", content)
    assert bundle[path] == content
    assert item["detail"]["source"]["artifact_sha256"] == bundle_tree_sha256(original)
    assert PgMiningFileStore(worker.repository.backend).load("job-1") == source_files
    document = json.loads(bundle["benchmark.json"])
    record = document["cases"][0]["materials"][0]
    assert record == {**{key: value for key, value in material.items() if key != "content"},
                      "path": path, "sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    assert "content_b64" not in record
    host = Host()
    spec = skill_replay_spec({**item["detail"], "job_id": item["candidate_id"],
                              "candidate_skill": skill_payload("demo", bundle)})
    fixed = FixedHost(host.resolve_replay_factory("tenant-selected", {}), {})
    replayed = evaluate_spec(spec, host=fixed, judge=judge, case_index=0)
    assert replayed["status"] == "evaluated"
    assert len(host.contexts) == 2
    for context in host.contexts:
        observed = context.materials[0]
        assert observed["path"] == path and base64.b64decode(observed["content_b64"]) == content
        assert {key: observed[key] for key in record} == record


@pytest.mark.parametrize("failure", ["collision", "missing-path", "hash", "directory"])
def test_material_references_cannot_overwrite_files_or_invent_unavailable_bytes(pg_env, tmp_path, failure):
    worker = engine(pg_env, tmp_path)
    material = {"content": "source material"}
    if failure == "missing-path":
        material = {"path": "references/missing.txt"}
    elif failure == "hash":
        material["sha256"] = "invalid"
    _, files = seed(worker, materials=[material])
    if failure in {"collision", "directory"}:
        path = inline_path("case-0", b"source material")
        if failure == "directory":
            path = path.rsplit("/", 1)[0]
        files["workspace/compiled_skill/demo/" + path] = b"different existing file"
        PgMiningFileStore(worker.repository.backend).save("job-1", files)
    with pytest.raises(ValueError, match="material|Material"):
        submit(pg_env, worker)
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).list(c))[1] == 0


def test_material_projection_respects_candidate_bundle_limits(pg_env, tmp_path, monkeypatch):
    worker = engine(pg_env, tmp_path)
    original, _ = seed(worker, materials=[{"content": "source material"}])
    real_config = candidate_bridge.load_skills_config

    async def restricted(*args):
        await real_config(*args)
        return SimpleNamespace(bundle_limits=BundleLimits(max_files=len(original)))
    monkeypatch.setattr(candidate_bridge, "load_skills_config", restricted)
    with pytest.raises(ValueError, match="files"):
        submit(pg_env, worker)
    assert _run(pg_env, "alpha", lambda c: CandidateRepository(pg_env.tx).list(c))[1] == 0


def test_existing_material_paths_load_verified_candidate_file_bytes(pg_env, tmp_path):
    worker = engine(pg_env, tmp_path)
    original, source_files = seed(worker, materials=[{"path": "references/rules.md", "visibility": "initial"}])
    result = submit(pg_env, worker)
    item, bundle, _ = read(pg_env, worker, result["job_id"])
    material = item["detail"]["replay_cases"][0]["materials"][0]
    assert material["path"] == "references/rules.md" and material["visibility"] == "initial"
    assert base64.b64decode(material["content_b64"]) == original["references/rules.md"]
    assert set(bundle) == set(original)
    assert PgMiningFileStore(worker.repository.backend).load("job-1") == source_files


@pytest.mark.parametrize("exceeded", ["count", "total", "single", "plan"])
def test_snapshot_rejects_metadata_limits_before_reading_any_binary_content(pg_env, tmp_path, monkeypatch, exceeded):
    worker = engine(pg_env, tmp_path)
    seed(worker)
    limits = BundleLimits(**{
        "count": {"max_files": 1}, "total": {"max_bytes": 1},
        "single": {"max_file_bytes": 1}, "plan": {},
    }[exceeded])
    if exceeded == "plan":
        monkeypatch.setattr(candidate_bridge, "MAX_CAPABILITY_PLAN_BYTES", 1)

    class MetadataOnly:
        def __init__(self, conn):
            self.fetchrow = conn.fetchrow

        async def fetch(self, *_args):
            raise AssertionError("oversized candidate must not read binary content")

    async def load(conn):
        return await candidate_bridge.MiningCandidateRepository(pg_env.tx).load(
            MetadataOnly(conn), "alpha", "job-1", "demo", limits=limits,
        )
    with pytest.raises(ValueError, match="limit"):
        _run(pg_env, "alpha", load)
