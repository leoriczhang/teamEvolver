import asyncio
import json
from types import SimpleNamespace

from fastapi.testclient import TestClient

from session_ingestion.service import _classify_session
from team_skills.evolution.experience_library import ExperienceLibraryStore, session_experiences
from team_skills.evolution.session_filter import SessionValueClassifier
from team_skills.evolution.stages.analyze import _compose_system
from team_skills.evolution.stages.judge import _parse_scores
from teamEvolver.config import TeamEvolverConfig
from teamEvolver.proxy import ProxyServer, routes
from teamEvolver.session_store import SessionStore
from teamEvolver.storage import InMemoryObjectStore


def _bootstrap_tenant_admin(client):
    assert client.post(
        "/api/auth/bootstrap",
        json={"username": "admin", "password": "test-password"},
    ).status_code == 200
    client.headers["X-Tenant-Id"] = "default"


def _session(session_id, kind, key, description, *, user="u1", score=0.8):
    return {
        "session_id": session_id,
        "user_alias": user,
        "timestamp": f"2026-09-{session_id[-2:]}T10:00:00+00:00",
        "used_skills": ["code-review"],
        "_judge_scores": {
            "overall_score": score,
            "evolution_evidence": kind,
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


def test_parse_scores_keeps_normalized_skill_experiences():
    payload = {
        "task_completion": 0.8,
        "response_quality": 0.9,
        "efficiency": 0.7,
        "tool_usage": 1,
        "evolution_evidence": "defect",
        "skill_experiences": [
            {
                "skill_name": "code-review",
                "kind": "DEFECT",
                "experience_key": "missed_test_regression",
                "description": "Agent 未运行回归测试，导致问题未被及时发现。",
            },
            {
                "skill_name": "",
                "kind": "exemplary",
                "experience_key": "invalid",
                "description": "不应保留。",
            },
        ],
    }
    scores = _parse_scores(json.dumps(payload, ensure_ascii=False))

    assert scores is not None
    assert scores["skill_experiences"] == [
        {
            "skill_name": "code-review",
            "kind": "defect",
            "experience_key": "missed_test_regression",
            "description": "Agent 未运行回归测试，导致问题未被及时发现。",
        }
    ]


def test_custom_analysis_prompt_receives_experience_contract():
    prompt = _compose_system("custom analysis prompt", ["code-review"])

    assert "skill_experiences" in prompt
    assert "code-review" in prompt


def test_experience_library_aggregates_unique_session_occurrences():
    bucket = InMemoryObjectStore()
    session_store = SessionStore(bucket)
    store = ExperienceLibraryStore(session_store)
    sessions = [
        _session(
            "session-01",
            "defect",
            "missed_test_regression",
            "Agent 未运行回归测试，导致问题未被及时发现。",
        ),
        _session(
            "session-02",
            "defect",
            "missed_test_regression",
            "Agent 跳过了回归测试；修改后应执行相关测试。",
            user="u2",
            score=0.4,
        ),
        _session(
            "session-03",
            "exemplary",
            "evidence_first_review",
            "Agent 先核对变更证据再给出结论，避免了无依据判断。",
        ),
    ]

    for session in sessions:
        session_store.save_skipped(session)
    session_store.save_skipped(sessions[0])
    result = store.list_experiences()

    assert result["stats"] == {
        "total_experiences": 2,
        "total_occurrences": 3,
        "defect_experiences": 1,
        "defect_occurrences": 2,
        "exemplary_experiences": 1,
        "exemplary_occurrences": 1,
        "skills": 1,
    }
    defect = next(item for item in result["items"] if item["kind"] == "defect")
    assert defect["occurrence_count"] == 2
    assert defect["session_ids"] == ["session-01", "session-02"]
    assert defect["user_aliases"] == ["u1", "u2"]
    assert defect["description"] == "Agent 跳过了回归测试；修改后应执行相关测试。"


def test_experience_library_supports_kind_skill_and_text_filters():
    bucket = InMemoryObjectStore()
    session_store = SessionStore(bucket)
    store = ExperienceLibraryStore(session_store)
    for session in [
        _session(
            "session-01",
            "defect",
            "missed_test_regression",
            "Agent 未运行回归测试。",
        ),
        _session(
            "session-02",
            "exemplary",
            "evidence_first_review",
            "Agent 先核对证据再判断。",
        ),
    ]:
        session_store.save_skipped(session)

    result = store.list_experiences(
        kind="exemplary",
        skill="code-review",
        search="核对证据",
    )

    assert len(result["items"]) == 1
    assert result["items"][0]["experience_key"] == "evidence_first_review"


def test_experience_library_api_returns_filtered_page(tmp_path, monkeypatch):
    bucket = InMemoryObjectStore()
    session_store = SessionStore(bucket)
    store = ExperienceLibraryStore(session_store)
    session_store.save_skipped(
        _session(
            "session-01",
            "defect",
            "missed_test_regression",
            "Agent 未运行回归测试。",
        )
    )
    monkeypatch.setattr(
        routes,
        "_build_experience_library_store",
        lambda _config: store,
    )
    server = ProxyServer(
        TeamEvolverConfig(
            _config_file=str(tmp_path / "config.yaml"),
            skills_runtime_cache_dir=str(tmp_path / "skills"),
            sharing_skill_mirror_enabled=False,
        )
    )
    client = TestClient(server.app)
    _bootstrap_tenant_admin(client)

    response = client.get(
        "/api/skill-experiences?kind=defect&search=回归&refresh=true"
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 1
    assert payload["items"][0]["occurrence_count"] == 1
    assert payload["stats"]["defect_occurrences"] == 1


def _unuploaded_skill_session(session_id="local-skill-01"):
    return {
        "session_id": session_id,
        "timestamp": "2026-09-18T10:00:00+00:00",
        "ingested_at": "2026-09-20T10:00:00+00:00",
        "used_skills": ["local-only-review"],
        "turns": [{"prompt_text": "使用本地 Skill 审查代码", "response_text": "已完成审查和验证"}],
        "_judge_scores": {
            "overall_score": 0.9,
            "evolution_evidence": "exemplary",
            "skill_experiences": [{
                "skill_name": "local-only-review",
                "kind": "exemplary",
                "experience_key": "verify_review_findings",
                "description": "提出审查结论前执行相关验证，确保结论有运行结果支持。",
            }],
        },
    }


def test_analysis_keeps_experience_for_used_skill_not_uploaded_to_library():
    scores = {
        **_unuploaded_skill_session()["_judge_scores"],
        "task_completion": 0.9, "response_quality": 0.9, "efficiency": 0.9, "tool_usage": 0.9,
    }
    scores["skill_experiences"].append({
        "skill_name": "not-actually-used",
        "kind": "defect",
        "experience_key": "invented",
        "description": "不能凭空绑定 Skill。",
    })
    reply = (
        '<classification>{"decision":"valuable","confidence":0.9,"reason":"有可复用方法"}</classification>'
        "<summary>修改后执行验证并确认任务结果。</summary>"
        f"<judge>{json.dumps(scores, ensure_ascii=False)}</judge>"
    )
    calls = []

    async def chat(messages, **_kwargs):
        calls.append(messages)
        return reply

    session = _unuploaded_skill_session()
    asyncio.run(_classify_session(
        session, SessionValueClassifier(client=SimpleNamespace(chat=chat, model="test"))
    ))
    assert len(calls) == 1
    assert session["_judge_scores"]["skill_experiences"] == [{
        "skill_name": "local-only-review",
        "kind": "exemplary",
        "experience_key": "verify_review_findings",
        "description": "提出审查结论前执行相关验证，确保结论有运行结果支持。",
    }]
    assert session["judge"]["skill_experiences"] == session["_judge_scores"]["skill_experiences"]


def test_custom_prompt_requires_experience_even_when_skill_is_not_uploaded():
    prompt = _compose_system("custom analysis prompt", ["local-only-review"])
    assert "local-only-review" in prompt
    assert "无需预先上传" in prompt


def test_session_experiences_reads_only_explicit_named_skill_lessons():
    session = _unuploaded_skill_session()
    assert session_experiences(session)[0]["skill_name"] == "local-only-review"
    session["_judge_scores"].pop("skill_experiences")
    assert session_experiences(session) == []
    session["_judge_scores"]["skill_experiences"] = []
    assert session_experiences(session) == []


def test_indexed_experiences_include_queued_skipped_and_unuploaded_skills():
    bucket = InMemoryObjectStore()
    sessions = SessionStore(bucket)
    store = ExperienceLibraryStore(sessions)
    sessions.save_queued(_unuploaded_skill_session())
    sessions.save_skipped(_unuploaded_skill_session("local-skill-02"))
    result = store.list_experiences()
    assert result["stats"]["total_experiences"] == 1
    assert result["stats"]["total_occurrences"] == 2
    assert result["skill_counts"] == {"local-only-review": 1}
    assert result["items"][0]["skill_name"] == "local-only-review"
    assert result["items"][0]["occurrence_count"] == 2
    assert result["items"][0]["last_ingested_at"] == "2026-09-20T10:00:00+00:00"
    assert not list(bucket.iter_objects(prefix="skills/"))
    # Reprocessing replaces the indexed lessons rather than retaining stale
    # occurrences or inflating counts.
    updated = _unuploaded_skill_session()
    updated["_judge_scores"]["skill_experiences"] = []
    sessions.save_queued(updated)
    assert store.list_experiences()["stats"]["total_occurrences"] == 1


def test_incomplete_index_is_not_backfilled_from_archives(monkeypatch):
    bucket = InMemoryObjectStore()
    sessions = SessionStore(bucket)
    session = _unuploaded_skill_session()
    sessions.save_skipped(session)
    rows = sessions.load_index_rows()
    rows[0].pop("experiences")
    bucket.put_object(sessions.session_index_key(), json.dumps(rows).encode())
    store = ExperienceLibraryStore(sessions)
    original_get = bucket.get_object

    def no_archive_reads(key):
        assert not key.startswith("session_archive/")
        return original_get(key)

    monkeypatch.setattr(bucket, "get_object", no_archive_reads)
    assert store.list_experiences()["stats"]["total_experiences"] == 0


def test_experience_store_preserves_tenant_prefixes():
    bucket = InMemoryObjectStore()
    tenant_a = SessionStore(bucket, "tenants/a/")
    tenant_b = SessionStore(bucket, "tenants/b/")
    tenant_a.save_skipped(_unuploaded_skill_session())
    store_a = ExperienceLibraryStore(tenant_a)
    store_b = ExperienceLibraryStore(tenant_b)
    assert store_a.list_experiences()["stats"]["total_experiences"] == 1
    assert store_b.list_experiences()["stats"]["total_experiences"] == 0


def test_experience_api_works_without_skill_upload_or_runtime(tmp_path):
    config = TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "empty-skills"),
        sharing_local_root=str(tmp_path / "session-state"),
        sharing_skill_local_root=str(tmp_path / "skill-state"),
        sharing_session_backend="local",
        sharing_skill_backend="local",
        sharing_skill_mirror_enabled=False,
    )
    sessions = SessionStore.from_config(config)
    sessions.save_skipped(_unuploaded_skill_session())
    server = ProxyServer(config)
    client = TestClient(server.app)
    _bootstrap_tenant_admin(client)
    response = client.get("/api/skill-experiences?refresh=true")
    assert response.status_code == 200
    assert response.json()["total"] == 1
    assert response.json()["items"][0]["skill_name"] == "local-only-review"
    assert response.json()["skill_counts"] == {"local-only-review": 1}
    assert not (tmp_path / "empty-skills" / "SKILL.md").exists()
    assert client.get("/api/skill-experiences?skill=unknown").json()["total"] == 0
    assert client.get("/api/skill-experiences?offset=1").json()["items"] == []


def test_experience_api_reports_storage_failure(tmp_path, monkeypatch):
    def broken_store(_config):
        raise RuntimeError("storage unavailable")

    monkeypatch.setattr(routes, "_build_experience_library_store", broken_store)
    server = ProxyServer(TeamEvolverConfig(
        _config_file=str(tmp_path / "config.yaml"),
        skills_runtime_cache_dir=str(tmp_path / "skills"),
        sharing_skill_mirror_enabled=False,
    ))
    client = TestClient(server.app)
    _bootstrap_tenant_admin(client)
    response = client.get("/api/skill-experiences?refresh=true")
    assert response.status_code == 503
    assert "storage unavailable" in response.json()["detail"]
