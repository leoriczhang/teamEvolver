from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pytest
from fastapi import HTTPException

MINER_ROOT = Path(__file__).resolve().parents[1]
if str(MINER_ROOT) not in sys.path:
    sys.path.insert(0, str(MINER_ROOT))
if str(MINER_ROOT.parent) not in sys.path:
    sys.path.insert(0, str(MINER_ROOT.parent))

from team_miner.bridge import SkillMinerBridgeMixin  # noqa: E402
from team_miner.lifecycle import mined_artifact_descriptor  # noqa: E402
from team_miner.ov_compile_mining import (  # noqa: E402
    DEFAULT_KNOWLEDGE_SOURCE_URI,
    SKILL_BODY,
    SOURCE_TAG_KNOWLEDGE,
    SOURCE_TAG_SKILL,
    OvSkillMiningRepository,
    source_has_tag,
    validate_descendant,
    validate_resource_uri,
)


def _repository(tmp_path: Path) -> OvSkillMiningRepository:
    return OvSkillMiningRepository(tmp_path / "state", tmp_path / "miner")


def test_partial_skill_artifact_exposes_body_title_without_changing_machine_identity(tmp_path):
    from team_skills.library.frontmatter import render_skill_md

    repository = _repository(tmp_path)
    workspace = repository.project_root / "partial"
    path = workspace / "compiled_skill" / "phone-appointment" / "SKILL.md"
    path.parent.mkdir(parents=True)
    content = render_skill_md(
        name="phone-appointment", description="电话约访训练", content="# 电话约访方法\n\n先征得同意。"
    )
    path.write_text(content, encoding="utf-8")
    artifact = repository._artifact_rows(workspace)[0]
    assert artifact["display_title"] == "电话约访方法"
    assert artifact["skill_name"] == "phone-appointment"
    assert artifact["name"] == "SKILL.md"
    assert path.read_text(encoding="utf-8") == content


def test_source_registry_is_reference_only_and_idempotent(tmp_path):
    repository = _repository(tmp_path)
    uri = "viking://resources/team/runbooks"

    first = repository.save_source("account-a", uri)
    second = repository.save_source("account-a", uri)

    assert first == second
    assert first["tags"] == [SOURCE_TAG_SKILL]
    assert repository.list_source_records() == [first]
    removed = repository.remove_source(first["source_id"])
    assert removed["uri"] == uri
    assert repository.list_source_records() == []


def test_source_registry_supports_dual_purpose_and_protects_default(tmp_path):
    repository = _repository(tmp_path)

    default_source = repository.ensure_default_knowledge_source("account-a")
    assert default_source["uri"] == DEFAULT_KNOWLEDGE_SOURCE_URI
    assert default_source["default"] is True
    assert source_has_tag(default_source, SOURCE_TAG_KNOWLEDGE)
    assert not source_has_tag(default_source, SOURCE_TAG_SKILL)

    dual = repository.save_source(
        "account-a",
        DEFAULT_KNOWLEDGE_SOURCE_URI,
        tags=[SOURCE_TAG_SKILL, SOURCE_TAG_KNOWLEDGE],
    )
    assert dual["tags"] == [SOURCE_TAG_SKILL, SOURCE_TAG_KNOWLEDGE]

    with pytest.raises(ValueError, match="默认知识库知识源"):
        repository.update_source(dual["source_id"], tags=[SOURCE_TAG_SKILL])
    with pytest.raises(ValueError, match="不能移除"):
        repository.remove_source(dual["source_id"])


def test_legacy_source_record_is_migrated_to_skill_tag(tmp_path):
    repository = _repository(tmp_path)
    repository.sources_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "sources": [
                    {
                        "source_id": "legacy",
                        "account": "account-a",
                        "uri": "viking://resources/legacy",
                        "name": "legacy",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )

    assert repository.list_source_records()[0]["tags"] == [SOURCE_TAG_SKILL]


def test_resource_uri_boundaries_reject_escape():
    root = validate_resource_uri("viking://resources/team/runbooks")
    assert validate_descendant(root, f"{root}/guide.md") == f"{root}/guide.md"
    with pytest.raises(ValueError, match="Resources"):
        validate_resource_uri("viking://agent/skills/private")
    with pytest.raises(ValueError, match="不在"):
        validate_descendant(root, "viking://resources/other/guide.md")


def test_compile_skill_uses_original_full_mining_contract():
    assert "12-24 diverse test cases" in SKILL_BODY
    assert "`compiled_skill/<skill-name>/BENCHMARK.md`" in SKILL_BODY


def test_ov_task_observability_keeps_bounded_stage_and_event_timeline():
    first = SkillMinerBridgeMixin._ov_task_observability(
        {},
        {
            "created_at": "2026-09-30T00:00:00+00:00",
            "events": [
                {"stage": "snapshot", "timestamp": "2026-09-30T00:00:01+00:00", "status": "completed"},
            ],
        },
        "compile: pipeline",
        "2026-09-30T00:00:02+00:00",
    )
    second = SkillMinerBridgeMixin._ov_task_observability(
        first,
        {"events": []},
        "compile: pipeline",
        "2026-09-30T00:00:07+00:00",
    )

    assert second["ov_stage_history"] == [
        {
            "stage": "compile: pipeline",
            "first_seen_at": "2026-09-30T00:00:02+00:00",
            "last_seen_at": "2026-09-30T00:00:07+00:00",
            "observations": 2,
        }
    ]
    assert second["ov_event_timeline"] == [
        {"label": "snapshot", "at": "2026-09-30T00:00:01+00:00", "status": "completed"}
    ]


@pytest.mark.parametrize("benchmark_body", ["# Benchmark\n\nTwelve reviewed support scenarios.\n", None, " \n"])
def test_materialized_compile_bundle_keeps_existing_candidate_contract(tmp_path, benchmark_body):
    repository = _repository(tmp_path)
    job = repository.create_job(
        name="Support Skill",
        candidate_contract_version=1,
        account="account-a",
        source_uri="viking://resources/team/runbooks",
        source_snapshot_uri="viking://resources/agent_skill_workspace/runs/test/source",
        output_uri="viking://resources/agent_skill_workspace/runs/test/output",
        document_count=3,
        max_rounds=3,
    )
    repository.write_output(
        job["job_id"],
        "compiled_skill/support-triage/SKILL.md",
        "---\nname: support-triage\ndescription: Triage support requests.\n---\n\n# Support triage\n",
    )
    repository.write_output(
        job["job_id"],
        "compiled_skill/support-triage/EVALUATION.md",
        "# Evaluation\n\nVerify routing and boundaries.\n",
    )
    cases = [
        {
            "input": "A customer reports an unknown billing error. What should the agent do?",
            "requirements": [f"requirement {index}" for index in range(1, 13)],
            "difficulty": "medium",
            "target_dimensions": ["routing", "safety"],
            "source_session_ids": ["viking://resources/team/runbooks/billing.md"],
        }
    ]
    from team_miner.tests.dataset_fixtures import build_capability_plan, build_document

    cases = [{**cases[0], "input": f"Support scenario {index}: " + cases[0]["input"]} for index in range(12)]
    repository.write_output(job["job_id"], "analysis/capability-plan.json", json.dumps(
        build_capability_plan(["support-triage"], job["source_uri"] + "/billing.md")
    ))
    document = build_document("support-triage", cases)
    repository.write_output(job["job_id"], "compiled_skill/support-triage/benchmark.json", json.dumps(document))
    repository.write_output(job["job_id"], "analysis/evidence-map.md", "# Evidence\nGrounded in billing.md.\n")
    repository.write_output(job["job_id"], "analysis/gaps.md", "# Gaps\nNo unresolved gaps in this fixture.\n")
    repository.write_output(job["job_id"], "analysis/review.json", json.dumps({
        "schema_version": "skill-miner.review.v1", "questions": [],
    }))
    if benchmark_body is not None:
        repository.write_output(job["job_id"], "compiled_skill/support-triage/BENCHMARK.md", benchmark_body)
    if benchmark_body is None or not benchmark_body.strip():
        with pytest.raises(ValueError, match="BENCHMARK.md"):
            repository.finalize_materialized(job["job_id"])
        assert repository.get_job(job["job_id"])["materialized"] is False
        if benchmark_body is None:
            assert not (repository.workspace(job) / "compiled_skill/support-triage/BENCHMARK.md").exists()
        return
    finalized = repository.finalize_materialized(job["job_id"])
    public = repository.public_job(finalized, detail=True)

    assert finalized["materialized"] is True
    assert finalized["artifact_quality"]["can_submit"] is False
    assert public["skills"] == [
        {
            "name": "support-triage",
            "has_skill": True,
            "has_evaluation": True,
            "has_benchmark": True,
            "question_count": 12,
        }
    ]
    workspace = repository.workspace(finalized)
    assert (workspace / "compiled_skill/support-triage/benchmark.json").is_file()
    assert (workspace / "compiled_skill/support-triage/BENCHMARK.md").is_file()
    descriptor = mined_artifact_descriptor("support-triage", skillminer_root=workspace)
    assert descriptor["question_count"] == 12
    assert descriptor["has_evaluation"] is True


def test_public_active_job_marks_remote_state_as_stale_without_losing_status(tmp_path):
    repository = _repository(tmp_path)
    job = repository.create_job(
        name="Disconnected task",
        account="account-a",
        source_uri="viking://resources/team/runbooks",
        source_snapshot_uri="viking://resources/agent_skill_workspace/runs/test/source",
        output_uri="viking://resources/agent_skill_workspace/runs/test/output",
        document_count=3,
        max_rounds=1,
    )
    job = repository.update_job(
        job["job_id"],
        status="running",
        ov_task_id="task-1",
        ov_sync_error="OpenViking is unreachable",
        ov_sync_failures=1,
    )

    public = repository.public_job(job)

    assert public["status"] == "running"
    assert public["remote_state_stale"] is True
    assert public["ov_sync_error"] == "OpenViking is unreachable"


@pytest.mark.asyncio
async def test_remote_refresh_is_single_flight_and_uses_short_timeout(tmp_path):
    repository = _repository(tmp_path)
    job = repository.create_job(
        name="Single flight",
        account="account-a",
        source_uri="viking://resources/team/runbooks",
        source_snapshot_uri="viking://resources/agent_skill_workspace/runs/test/source",
        output_uri="viking://resources/agent_skill_workspace/runs/test/output",
        document_count=1,
        max_rounds=1,
    )
    job = repository.update_job(job["job_id"], status="running", ov_task_id="task-1")
    started = asyncio.Event()
    release = asyncio.Event()
    calls: list[float] = []

    class Owner(SkillMinerBridgeMixin):
        def _ov_skill_mining_repository(self):
            return repository

        async def _knowledge_request(self, account, method, path, *, timeout=60.0, **kwargs):
            del account, method, path, kwargs
            calls.append(timeout)
            started.set()
            await release.wait()
            return {"status": "running", "stage": "compile: pipeline"}

    owner = Owner()
    owner._schedule_ov_skill_job_refresh(job)
    owner._schedule_ov_skill_job_refresh(job)
    await asyncio.wait_for(started.wait(), timeout=1)
    assert calls == [5.0]
    release.set()
    await asyncio.wait_for(asyncio.gather(*owner._ov_skill_refresh_tasks.values()), timeout=1)

    refreshed = repository.get_job(job["job_id"])
    assert refreshed["status"] == "running"
    assert refreshed["ov_last_synced_at"]


@pytest.mark.asyncio
async def test_remote_refresh_failure_keeps_last_status_and_sets_backoff(tmp_path):
    repository = _repository(tmp_path)
    job = repository.create_job(
        name="Offline task",
        account="account-a",
        source_uri="viking://resources/team/runbooks",
        source_snapshot_uri="viking://resources/agent_skill_workspace/runs/test/source",
        output_uri="viking://resources/agent_skill_workspace/runs/test/output",
        document_count=1,
        max_rounds=1,
    )
    job = repository.update_job(job["job_id"], status="running", ov_task_id="task-1")

    class Owner(SkillMinerBridgeMixin):
        def _ov_skill_mining_repository(self):
            return repository

        async def _knowledge_request(self, *args, **kwargs):
            raise HTTPException(status_code=503, detail="OpenViking is unreachable")

    public = await Owner()._refresh_ov_skill_job(job)

    assert public["status"] == "running"
    assert public["remote_state_stale"] is True
    assert public["ov_sync_failures"] == 1
    assert public["ov_next_retry_at"]


def _streaming_job(repository):
    return repository.create_job(
        name="Stream Skill", account="account-a", source_uri="viking://resources/team/runbooks",
        source_snapshot_uri="viking://resources/agent_skill_workspace/runs/test/source",
        output_uri="viking://resources/agent_skill_workspace/runs/test/output",
        document_count=1, max_rounds=1, human_checkpoints=False, candidate_contract_version=1,
    )


@pytest.mark.parametrize("failure", ["coverage", "duplicate", "late-section", "all-skipped"])
def test_stream_output_preserves_previous_skill_when_late_validation_fails(tmp_path, failure):
    from team_miner.bounded_compile_plan import iter_skill_chunks

    repository = _repository(tmp_path)
    job = _streaming_job(repository)
    relative = "compiled_skill/stream-method/SKILL.md"
    previous = "---\nname: stream-method\ndescription: Previous method.\n---\n\n原有已保存方法。\n"
    path = repository.write_output(job["job_id"], relative, previous)
    proposed = {"name": "stream-method", "task": "核验提供的材料", "boundary": "按来源执行"}

    def sections():
        for index in range(2):
            value = {"schema_version": "skill-miner.section.v1", "input_ids": [f"input-{index}"],
                     "title": f"步骤 {index}", "content": "核对来源条件并记录结果。"}
            if failure == "all-skipped":
                value = {"schema_version": "skill-miner.section-skip.v1", "input_ids": [f"input-{index}"],
                         "reason": "本片段没有可执行规则"}
            elif index == 1:
                if failure == "coverage":
                    value["input_ids"] = ["not-assigned"]
                elif failure == "duplicate":
                    value["input_ids"] = ["input-0"]
                else:
                    value["content"] = "原" * 10_001
            yield value

    with pytest.raises(ValueError):
        repository.write_output_stream(
            job["job_id"], relative,
            iter_skill_chunks(proposed, sections(), expected_input_ids=["input-0", "input-1"]),
        )
    assert path.read_text() == previous
    assert not list(path.parent.glob(".te-stream-*"))
    with pytest.raises(ValueError, match="无效产物路径"):
        repository.write_output_stream(job["job_id"], "compiled_skill/../outside.md", ["unsafe"])


def test_large_skill_stream_keeps_memory_independent_of_total_body(tmp_path):
    import tracemalloc

    from team_miner.bounded_compile_plan import iter_skill_chunks

    repository = _repository(tmp_path)
    job = _streaming_job(repository)
    identities = [f"input-{index}" for index in range(1000)]
    consumed = []

    def sections():
        for index, identity in enumerate(identities):
            consumed.append(index)
            yield {"schema_version": "skill-miner.section.v1", "input_ids": [identity],
                   "title": f"分段 {index}", "content": "原文支持的操作步骤。" * 900}

    tracemalloc.start()
    try:
        path = repository.write_output_stream(
            job["job_id"], "compiled_skill/stream-method/SKILL.md",
            iter_skill_chunks(
                {"name": "stream-method", "task": "核验提供的材料", "boundary": "按来源执行"},
                sections(), expected_input_ids=identities,
            ),
        )
        _current, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert consumed == list(range(1000)) and path.stat().st_size > 20 * 1024 * 1024
    assert peak < 3 * 1024 * 1024


def test_final_candidate_validation_and_archive_stream_body_without_full_reads(tmp_path, monkeypatch):
    from team_miner.dataset_view import dataset_markdown
    from team_miner.tests.dataset_fixtures import build_document

    repository = _repository(tmp_path)
    job = _streaming_job(repository)
    header = "---\nname: stream-method\ndescription: 核验提供的材料。\n---\n\n# 操作规则\n"
    skill = repository.write_output_stream(
        job["job_id"], "compiled_skill/stream-method/SKILL.md",
        (header if index == 0 else "完整中文规则。" * 1000 for index in range(1001)),
    )
    document = build_document(
        "stream-method", [{"query": f"独立场景 {index}", "source_session_ids": [job["source_uri"] + "/policy.md"]}
                          for index in range(12)],
    )
    for relative, text in {
        "compiled_skill/stream-method/EVALUATION.md": "# 评测说明",
        "compiled_skill/stream-method/benchmark.json": json.dumps(document, ensure_ascii=False),
        "compiled_skill/stream-method/BENCHMARK.md": dataset_markdown(document),
        "analysis/evidence-map.md": "# 来源映射",
        "analysis/gaps.md": "# 来源缺口",
        "analysis/review.json": json.dumps({"schema_version": "skill-miner.review.v1", "questions": []}),
    }.items():
        repository.write_output(job["job_id"], relative, text)
    original_open, original_read_text, original_read_bytes = Path.open, Path.read_text, Path.read_bytes
    sizes = []

    class BoundedReader:
        def __init__(self, stream):
            self.stream = stream

        def __enter__(self):
            self.stream.__enter__()
            return self

        def __exit__(self, *args):
            return self.stream.__exit__(*args)

        def read(self, size=-1):
            assert 0 < size <= 64 * 1024, "Candidate validation and SHA must never read the complete body"
            sizes.append(size)
            return self.stream.read(size)

        def readline(self, size=-1):
            assert 0 < size <= 64 * 1024 + 1
            return self.stream.readline(size)

    def bounded_open(path, mode="r", *args, **kwargs):
        stream = original_open(path, mode, *args, **kwargs)
        return BoundedReader(stream) if path.name == "SKILL.md" and "r" in mode else stream

    def no_full_text(path, *args, **kwargs):
        assert path.name != "SKILL.md"
        return original_read_text(path, *args, **kwargs)

    def no_full_bytes(path):
        assert path.name != "SKILL.md"
        return original_read_bytes(path)

    monkeypatch.setattr(Path, "open", bounded_open)
    monkeypatch.setattr(Path, "read_text", no_full_text)
    monkeypatch.setattr(Path, "read_bytes", no_full_bytes)
    finalized = repository.finalize_materialized(job["job_id"])
    assert finalized["valid_candidate_count"] == 1 and finalized["materialized"]
    finished = repository.finish_materialized_round(job["job_id"])
    assert finished["status"] == "succeeded" and sizes
    archive = repository.jobs_root / job["job_id"] / "rounds/round-1"
    metadata = json.loads((archive / "meta.json").read_text())
    assert len(metadata["output_sha256"]["compiled_skill/stream-method/SKILL.md"]) == 64
    assert skill.stat().st_size == (archive / "output/compiled_skill/stream-method/SKILL.md").stat().st_size


def test_candidate_metadata_scan_rejects_invalid_utf8_late_in_body(tmp_path):
    from team_miner.ov_compile_mining import _skill_metadata

    path = tmp_path / "SKILL.md"
    path.write_bytes(b"---\nname: method\ndescription: Verify supplied Evidence.\n---\n" + b"rule\n" * 50_000 + b"\xe4")
    with pytest.raises(ValueError, match="完整有效的 UTF-8"):
        _skill_metadata(path)
