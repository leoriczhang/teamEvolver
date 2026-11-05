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
