from session_ingestion.projections import session_list_meta


def test_session_list_meta_prefers_explicit_title_and_normalizes_whitespace():
    value = session_list_meta(
        {"title": "  Build   a weekly\nreport ", "turns": [{"prompt_text": "Ignored prompt"}]},
    )
    assert value["title"] == "Build a weekly report"


def test_session_list_meta_uses_first_user_prompt_when_title_is_missing():
    value = session_list_meta({
        "turns": [
            {"prompt_text": "解释缓存命中后为什么不访问数据库", "response_text": "因为结果已在缓存中"},
            {"prompt_text": "补充箭头", "response_text": "完成"},
        ],
    })
    assert value["title"] == "解释缓存命中后为什么不访问数据库"


def test_available_catalog_is_distinct_from_child_actual_usage():
    value = session_list_meta({"turns": [
        {"skill_refs": [{"name": "audio", "source": "configured", "used": False},
                        {"name": "delivery", "source": "configured", "used": False}],
         "agent_threads": [{"used_skills": ["audio"], "read_skills": [{"skill_name": "audio"}]}]},
    ]})
    assert value["used_skills"] == ["audio"]
    assert value["available_skills"] == ["audio", "delivery"]


def test_list_versions_preserve_snapshot_and_do_not_turn_configured_skills_into_usage():
    session = {"runtime": {"agent_graph": {"entry_agent_id": "root", "agents": {
        "root": {"name": "director", "skills": [{"skill_id": "audio", "version": "3"}]},
    }}}, "turns": [{"skill_refs": [{"name": "audio-name", "source": "configured", "used": False}]}]}
    archive = session_list_meta(session)
    assert archive["skill_versions_revision"] == 0
    shown = session_list_meta(session, skill_names={"audio": "audio-name"})
    assert shown["skill_versions_revision"] == 1
    assert shown["used_skills"] == shown["used_skill_versions"] == []
    assert shown["available_skills"] == ["audio-name"]
    assert shown["available_skill_versions"][0]["version"] == "3"
    assert shown["available_skill_versions"][0]["agent_name"] == "director"


def test_call_without_a_snapshot_is_used_but_cannot_be_presented_as_configured():
    shown = session_list_meta({"turns": [{"skill_refs": [{
        "name": "audio-name", "skill_id": "missing-reference", "source": "skill-tool", "used": True,
    }]}]})
    assert shown["used_skills"] == ["audio-name"]
    assert shown["used_skill_versions"][0]["version"] is None
    assert shown["available_skill_versions"] == []
