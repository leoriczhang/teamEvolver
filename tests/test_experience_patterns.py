import asyncio
import json
from types import SimpleNamespace

from fastapi import FastAPI
from fastapi.testclient import TestClient

from team_skills.candidates.store import ValidationStore
from team_skills.evolution.kernel.settings import EvolveServerConfig
from team_skills.evolution.patterns import (
    ExperiencePatternCompiler,
    ExperiencePatternStore,
    PatternStoreUnavailable,
    aggregate_session_experiences,
    compact_pattern_context,
)
from team_skills.evolution.runtime.evidence import SkillEvidenceStore
from team_skills.evolution.runtime.orchestrator import EvolveServer
from team_skills.evolution.stages.execute import _build_cross_cycle_context
from team_skills.library.bundle import attach_bundle_payload
from team_skills.library.registry import SkillIDRegistry
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.storage import InMemoryObjectStore, LocalObjectStore


def _session(session_id, user, kind, key, description):
    return {
        "session_id": session_id,
        "user_alias": user,
        "_judge_scores": {
            "overall_score": 0.8,
            "skill_experiences": [
                {
                    "skill_name": "code-review",
                    "kind": kind,
                    "experience_key": key,
                    "description": description,
                }
            ],
        },
    }


class FakeLLM:
    def __init__(self):
        self.calls = []

    async def chat(self, messages, **kwargs):
        self.calls.append((messages, kwargs))
        experiences = json.loads(messages[-1]["content"])["experiences"]
        defects = [
            item["experience_id"]
            for item in experiences
            if item["kind"] == "defect"
        ]
        exemplaries = [
            item["experience_id"]
            for item in experiences
            if item["kind"] == "exemplary"
        ]
        return json.dumps(
            {
                "patterns": [
                    {
                        "pattern_key": "always_run_regression_tests",
                        "title": "修改后执行回归测试",
                        "problem": "修改后直接给出结论。",
                        "root_cause": "缺少验证步骤。",
                        "guidance": "完成修改后执行相关回归测试并记录结果。",
                        "experience_ids": defects,
                    },
                    {
                        "pattern_key": "cite_runtime_evidence",
                        "title": "引用运行证据",
                        "problem": "",
                        "root_cause": "",
                        "guidance": "结论应关联实际运行结果。",
                        "experience_ids": exemplaries,
                    },
                ]
            },
            ensure_ascii=False,
        )


def test_compiler_merges_semantic_experiences_and_skips_unchanged_source():
    sessions = [
        _session("s1", "u1", "defect", "skipped_tests", "修改后没有运行测试。"),
        _session("s2", "u2", "defect", "no_regression", "改动完成后跳过回归验证。"),
        _session("s3", "u2", "exemplary", "runtime_evidence", "结论引用了运行结果。"),
    ]
    experiences = aggregate_session_experiences(sessions, "code-review")
    bucket = InMemoryObjectStore()
    store = ExperiencePatternStore(bucket)
    llm = FakeLLM()
    compiler = ExperiencePatternCompiler(llm, store)

    first = asyncio.run(compiler.compile("code-review", experiences))
    second = asyncio.run(compiler.compile("code-review", experiences))

    assert first["compiled"] is True
    assert second["compiled"] is False
    assert len(llm.calls) == 1
    patterns = first["document"]["patterns"]
    defect = next(item for item in patterns if item["kind"] == "defect")
    exemplary = next(item for item in patterns if item["kind"] == "exemplary")
    assert defect["evidence_count"] == 2
    assert defect["session_count"] == 2
    assert defect["user_count"] == 2
    assert defect["status"] == "ready"
    assert exemplary["status"] == "observing"

    context, refs = compact_pattern_context(patterns)
    assert [item["pattern_key"] for item in context] == [
        "always_run_regression_tests"
    ]
    assert refs == [
        {
            "skill_name": "code-review",
            "pattern_id": defect["pattern_id"],
            "pattern_key": "always_run_regression_tests",
        }
    ]


def test_candidate_outcome_updates_existing_pattern_reference():
    sessions = [
        _session("s1", "u1", "defect", "skipped_tests", "修改后没有运行测试。"),
        _session("s2", "u2", "defect", "no_regression", "改动完成后跳过回归验证。"),
    ]
    store = ExperiencePatternStore(InMemoryObjectStore())
    document = asyncio.run(
        ExperiencePatternCompiler(FakeLLM(), store).compile(
            "code-review",
            aggregate_session_experiences(sessions, "code-review"),
        )
    )["document"]
    pattern = document["patterns"][0]
    refs = [
        {
            "skill_name": "code-review",
            "pattern_id": pattern["pattern_id"],
        }
    ]

    assert store.record_candidate_outcome(
        refs,
        job_id="job-1",
        status="queued",
        candidate_revision=1,
    ) == 1
    assert store.record_candidate_outcome(
        refs,
        job_id="job-1",
        status="published",
        candidate_revision=1,
        version=3,
    ) == 1
    saved = store.load_skill("code-review")
    assert saved is not None
    assert saved["patterns"][0]["candidate_outcomes"] == [
        {
            "job_id": "job-1",
            "status": "published",
            "recorded_at": saved["patterns"][0]["candidate_outcomes"][0][
                "recorded_at"
            ],
            "candidate_revision": 1,
            "version": 3,
        }
    ]
    listed = store.list_patterns(skill="code-review")
    assert listed["stats"]["ready_patterns"] == 1
    assert listed["location"].endswith("/patterns")


def test_compiler_drops_ungrounded_or_mixed_kind_patterns():
    experiences = aggregate_session_experiences(
        [
            _session("s1", "u1", "defect", "bad", "错误经验。"),
            _session("s2", "u2", "exemplary", "good", "优秀经验。"),
        ],
        "code-review",
    )

    class MixedLLM:
        async def chat(self, *_args, **_kwargs):
            return json.dumps(
                {
                    "patterns": [
                        {
                            "pattern_key": "invented",
                            "experience_ids": ["not-in-input"],
                        },
                        {
                            "pattern_key": "mixed",
                            "experience_ids": [item["id"] for item in experiences],
                        },
                    ]
                }
            )

    try:
        asyncio.run(
            ExperiencePatternCompiler(
                MixedLLM(),
                ExperiencePatternStore(InMemoryObjectStore()),
            ).compile("code-review", experiences)
        )
    except ValueError as exc:
        assert "grounded" in str(exc)
    else:
        raise AssertionError("ungrounded pattern output must fail")


def test_pattern_store_requires_openviking_root_key():
    try:
        ExperiencePatternStore.from_config(
            TeamEvolverConfig(sharing_viking_api_key="")
        )
    except PatternStoreUnavailable as exc:
        assert "Root Key" in str(exc)
    else:
        raise AssertionError("Pattern store must not fall back without OV credentials")


def test_pattern_store_uses_configured_experience_ov_directory():
    config = TeamEvolverConfig(
        sharing_viking_endpoint="http://localhost:1933",
        sharing_viking_api_key="root-key",
        experience_sync={
            "target_directory": "viking://resources/custom/experience-library"
        },
    )
    engine_config = EvolveServerConfig.from_teamEvolver_config(config)

    host_store = ExperiencePatternStore.from_config(config)
    engine_store = ExperiencePatternStore.from_config(engine_config)

    expected = "viking://resources/custom/experience-library/patterns"
    assert host_store.location == expected
    assert engine_store.location == expected
    assert host_store._bucket._base_uri() == f"{expected}/"


def test_evidence_ledger_restores_skill_experiences_for_later_compilation():
    store = SkillEvidenceStore(
        InMemoryObjectStore(),
        recent_limit=2,
        historical_limit=2,
    )
    store.record_sessions(
        "code-review",
        [
            _session(
                "s1",
                "u1",
                "defect",
                "skipped_tests",
                "修改后没有运行测试。",
            )
        ],
    )

    planning, _context = store.build_context("code-review", [])

    assert planning[0]["_evidence_window"] == "recent"
    assert planning[0]["_judge_scores"]["skill_experiences"] == [
        {
            "skill_name": "code-review",
            "kind": "defect",
            "experience_key": "skipped_tests",
            "description": "修改后没有运行测试。",
        }
    ]


def test_pattern_guidance_is_rendered_into_evolution_agent_context():
    rendered = _build_cross_cycle_context(
        {
            "total_evidence_sessions": 2,
            "experience_patterns": [
                {
                    "pattern_id": "pat-1",
                    "pattern_key": "always_run_regression_tests",
                    "title": "修改后执行回归测试",
                    "kind": "defect",
                    "problem": "修改完成后直接给出结论。",
                    "root_cause": "缺少验证步骤。",
                    "guidance": "执行相关回归测试并记录结果。",
                    "experience_ids": ["exp-1", "exp-2"],
                    "session_ids": ["s1", "s2"],
                }
            ],
            "pattern_store": "viking://resources/example/patterns",
        }
    )

    assert "always_run_regression_tests" in rendered
    assert "执行相关回归测试并记录结果" in rendered
    assert "exp-1" in rendered
    assert "viking://resources/example/patterns" in rendered


def test_validation_job_keeps_pattern_references(tmp_path):
    server = object.__new__(EvolveServer)
    server.config = EvolveServerConfig.from_teamEvolver_config(
        TeamEvolverConfig(validation_runtimes=["langfuse"])
    )
    server._id_registry = SkillIDRegistry()
    server._validation_store = ValidationStore.from_bucket(
        bucket=LocalObjectStore(tmp_path)
    )
    pattern_refs = [{
        "skill_name": "code-review",
        "pattern_id": "pat-1",
        "pattern_key": "always_run_regression_tests",
    }]

    result = server._queue_validation_job(
        attach_bundle_payload(
            {
                "name": "code-review",
                "description": "Review code changes.",
                "content": "Run the relevant tests.",
            },
            {},
        ),
        "improve_skill",
        [{
            "session_id": "s1",
            "source": "langfuse",
            "turns": [{
                "prompt_text": "Review this change.",
                "response_text": "Reviewed.",
            }],
        }],
        "Pattern-backed evidence",
        "skill_group",
        evolution_context={"pattern_refs": pattern_refs},
    )

    job = server._validation_store.load_job(result["validation_job_id"])
    assert result["pattern_refs"] == pattern_refs
    assert job["pattern_refs"] == pattern_refs


def test_pattern_api_lists_compilable_skills_and_compiles_selected_skill(
    monkeypatch,
):
    from teamEvolver.proxy import experience_patterns as api_module

    class ApiStore:
        location = "viking://resources/example/patterns"

        def list_patterns(self, **_filters):
            return {
                "items": [],
                "stats": {"total_patterns": 0},
                "skill_counts": {},
                "location": self.location,
            }

    class Library:
        def list_experiences(self, **_filters):
            return {
                "items": [{
                    "id": "exp-1",
                    "skill_name": "code-review",
                    "kind": "defect",
                    "experience_key": "skipped_tests",
                    "description": "修改后没有运行测试。",
                    "occurrence_count": 2,
                    "session_ids": ["s1", "s2"],
                    "user_aliases": ["u1", "u2"],
                }],
                "skill_counts": {"code-review": 1},
            }

    class Compiler:
        def __init__(self, llm, store):
            assert llm is engine._llm
            assert store is pattern_store

        async def compile(self, skill_name, experiences, *, force=False):
            assert skill_name == "code-review"
            assert experiences[0]["id"] == "exp-1"
            assert force is False
            return {
                "compiled": True,
                "document": {
                    "compiled_at": "2026-09-28T10:00:00Z",
                    "patterns": [{"pattern_id": "pat-1"}],
                },
            }

    pattern_store = ApiStore()
    engine = SimpleNamespace(_llm=object())
    owner = SimpleNamespace(
        config=SimpleNamespace(),
        _get_embedded_evolve_server=lambda _tenant_id: engine,
    )
    monkeypatch.setattr(
        api_module.ExperiencePatternStore,
        "from_config",
        classmethod(lambda _cls, _config: pattern_store),
    )
    monkeypatch.setattr(api_module, "_library", lambda _config: Library())
    monkeypatch.setattr(api_module, "ExperiencePatternCompiler", Compiler)
    app = FastAPI()

    async def user_resolver(_request):
        return {"id": "admin"}

    api_module.install(
        app,
        owner,
        lambda value: value.config,
        lambda user: None if user else (_ for _ in ()).throw(RuntimeError()),
        user_resolver,
    )
    client = TestClient(app)

    listed = client.get("/api/skill-experience-patterns")
    compiled = client.post(
        "/api/skill-experience-patterns/compile",
        json={"skill": "code-review"},
    )

    assert listed.status_code == 200
    assert listed.json()["available_skills"] == ["code-review"]
    assert compiled.status_code == 200
    assert compiled.json()["pattern_count"] == 1
