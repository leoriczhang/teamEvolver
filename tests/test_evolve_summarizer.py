from teamEvolver.evolve.kernel.enums import NO_SKILL_KEY
from teamEvolver.evolve.stages.aggregate import aggregate_sessions_by_skill
from teamEvolver.evolve.stages.summarize import _extract_session_metadata


def test_injected_skill_catalog_does_not_count_as_skill_reference():
    session = {
        "session_id": "s1",
        "turns": [
            {
                "prompt_text": "task",
                "response_text": "answer",
                "injected_skills": ["api-helper", "debug-helper"],
            }
        ],
    }

    _extract_session_metadata(session)
    grouped = aggregate_sessions_by_skill([session])

    assert session["_skills_referenced"] == set()
    assert session["_skills_reference_source"] == "catalog_only"
    assert session["_skills_injected"] == {"api-helper", "debug-helper"}
    assert grouped == {NO_SKILL_KEY: [session]}


def test_read_or_modified_skills_count_as_skill_references():
    session = {
        "session_id": "s1",
        "turns": [
            {
                "read_skills": [{"skill_name": "api-helper"}],
                "modified_skills": [{"skill_name": "debug-helper"}],
                "injected_skills": ["catalog-only"],
            }
        ],
    }

    _extract_session_metadata(session)
    grouped = aggregate_sessions_by_skill([session])

    assert session["_skills_referenced"] == {"api-helper", "debug-helper"}
    assert session["_skills_reference_source"] == "explicit"
    assert session["_skills_injected"] == {"catalog-only"}
    assert set(grouped) == {"api-helper", "debug-helper"}
    assert NO_SKILL_KEY not in grouped
