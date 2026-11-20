"""Version evidence stays historical and preserves main/child attribution."""
import copy

from session_ingestion.skill_versions import session_skill_versions


def recorded():
    return {
        "runtime": {"agent_graph": {"entry_agent_id": "root", "agents": {
            "root": {"name": "director", "version": 8, "system": "PRIVATE",
                     "skills": [{"skill_id": "shared", "version": "1"}]},
            "child": {"name": "writer", "version": 3, "skills": [{"skill_id": "shared", "version": "2"}]},
        }}},
        "turns": [{
            "used_skills": ["notes"],
            "skill_refs": [{"name": "notes", "used": True, "source": "skill-tool",
                            "event_id": "call", "session_thread_id": "thread"}],
            "agent_threads": [{
                "agent_name": "writer", "session_thread_id": "thread", "used_skills": ["notes"],
                "skill_refs": [{"name": "notes", "used": True, "source": "skill-tool",
                                "event_id": "call", "session_thread_id": "thread"}],
            }],
        }],
    }


def test_child_usage_is_matched_to_its_own_fixed_version_without_double_counting():
    session = recorded()
    before = copy.deepcopy(session)
    rows = session_skill_versions(session, {"shared": "notes"})
    assert [(r["agent_id"], r["version"], r["usage"]) for r in rows] == [
        ("root", "1", "configured"), ("child", "2", "used")]
    assert rows[1]["event_ids"] == ["call"]
    assert session == before and "PRIVATE" not in str(rows)


def test_unknown_caller_with_multiple_versions_does_not_mark_all_agents_as_used():
    session = recorded()
    session["turns"] = [{"used_skills": ["notes"]}]
    rows = session_skill_versions(session, {"shared": "notes"})
    assert [r["usage"] for r in rows[:2]] == ["configured", "configured"]
    assert rows[2]["version"] is None and rows[2]["usage"] == "used"
    assert rows[2]["agent_role"] == "unknown" and rows[2]["unknown_reason"]


def test_unpinned_and_missing_historical_references_never_infer_a_latest_version():
    session = recorded()
    session["runtime"]["agent_graph"]["agents"]["child"]["skills"][0]["version"] = "latest"
    rows = session_skill_versions(session, {"shared": "notes"})
    assert rows[1]["usage"] == "used" and rows[1]["version"] is None
    assert rows[1]["unknown_reason"] == "会话引用未记录固定版本"
    missing = session_skill_versions({}, meta={"used_skills": ["notes"]})
    assert missing[0]["version"] is None and missing[0]["name"] == "notes"
    assert missing[0]["usage"] == "used"


def test_unknown_agent_does_not_borrow_the_only_other_agents_version():
    session = recorded()
    del session["runtime"]["agent_graph"]["agents"]["root"]
    session["turns"] = [{"skill_refs": [{
        "name": "notes", "agent_name": "outside", "used": True, "source": "skill-tool",
    }]}]
    rows = session_skill_versions(session, {"shared": "notes"})
    assert rows[0]["usage"] == "configured"
    assert rows[1]["version"] is None and rows[1]["agent_name"] == "outside"


def test_incomplete_environment_does_not_discard_a_recorded_agent_skill_version():
    session = recorded()
    session["runtime"].update(snapshot_missing=True, snapshot_reason="Environment config unavailable")
    rows = session_skill_versions(session, {"shared": "notes"})
    assert rows[1]["version"] == "2"


def test_duplicate_agent_names_are_ambiguous_even_with_only_one_mounted_reference():
    session = recorded()
    session["runtime"]["agent_graph"]["agents"]["root"].update(name="writer", skills=[])
    rows = session_skill_versions(session, {"shared": "notes"})
    assert rows[0]["usage"] == "configured"
    assert rows[1]["version"] is None
    assert rows[1]["unknown_reason"] == "存在多个同名 Agent，调用归属无法确认"
