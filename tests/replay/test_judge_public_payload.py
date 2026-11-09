"""The Judge receives only the complete Case Checks and public execution evidence."""
import copy

from team_replay import judging

ORIGINAL_LIST = "结束列表=【先看看工作安排｜周六下午两点以后｜周日傍晚】"


def checks(*, definition_present):
    values = [{"id": f"R{i:02}", "kind": "output", "text": text} for i, text in enumerate([
        "保持客户角色", "不追问概念", "不复述已有客户发言", "不编造承诺", "只返回一条纯文本回复",
        "再次触发时按原结束列表的含义回应", "保持原有条件和例外",
    ], 1)]
    if definition_present:
        values[5]["text"] = "再次触发时按本Case R07定义的结束列表含义回应"
        values[6]["text"] = ORIGINAL_LIST + "；按原条件与例外适用，不新增承诺"
    return values


def test_actual_judge_payload_has_only_full_case_checks_and_public_execution_not_implicit_source_definitions():
    checklist = checks(definition_present=True)
    captured = []

    def completion(**request):
        captured.append(copy.deepcopy(request))
        return {"items": [{"id": check["id"], "satisfied": True, "evidence": "Synthetic evidence"}
                          for check in checklist], "all_satisfied": True, "positive_observations": []}

    result = judging.judge_checklist(harness={"model": "fake"}, checklist=checklist,
        interactions=[{"prompt": "原始用户请求", "response": "公开客户回复"}],
        messages=[{"role": "assistant", "content": "公开客户回复"}], artifacts=[], completion=completion)
    assert result["judge"] == "model" and len(captured) == 1
    payload = captured[0]["payload"]
    assert set(payload) == {"checklist", "interactions", "messages", "artifacts"}
    assert payload["checklist"] == checklist and payload["checklist"][6]["text"].count(ORIGINAL_LIST) == 1
    assert payload["interactions"] == [{"user": "原始用户请求", "agent": "公开客户回复"}]
    assert payload["messages"] == [{"role": "assistant", "content": "公开客户回复"}]
    assert payload["artifacts"] == []
