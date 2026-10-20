from team_skills.evolution.agent.contract import parse_turn


def test_current_final_envelope_is_accepted() -> None:
    parsed = parse_turn('{"type":"final","decision":{"action":"skip","rationale":"done"}}')

    assert parsed["ok"] is True
    assert parsed["type"] == "final"
    assert parsed["decision"]["action"] == "skip"


def test_legacy_one_shot_decision_is_rejected() -> None:
    parsed = parse_turn('{"action":"skip","rationale":"old shape"}')

    assert parsed == {"ok": False, "error": "unrecognized turn shape"}


def test_legacy_bare_merge_skill_is_rejected() -> None:
    parsed = parse_turn('{"name":"demo","content":"old shape"}')

    assert parsed == {"ok": False, "error": "unrecognized turn shape"}
