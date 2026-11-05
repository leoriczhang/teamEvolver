"""Artifact language follows original prose, not code or previous generated output."""

from __future__ import annotations

import pytest

from team_miner.skill_miner import output_language
from team_miner.skill_miner.output_language import (
    output_language_instruction,
    resolve_output_language_policy,
    validate_artifact_headings,
)


def source(tmp_path, content, name="policy.md", kind="source"):
    path = tmp_path / name
    path.write_text(content, encoding="utf-8")
    return {"kind": kind, "relative_path": f"original/{name}", "local_path": str(path)}


@pytest.mark.parametrize(
    ("text", "language"),
    [
        ("退货申请应先核对订单编号与签收日期，再检查商品是否未拆封；缺少材料时暂停并转人工。", "zh-CN"),
        (
            "The user must check the original receipt before approving the refund. "
            "After approval and verification, return the payment within three working days. "
            "The return address is 北京市朝阳区测试路一号.",
            "en",
        ),
        (
            "THE USER MUST CHECK THE ORIGINAL RECEIPT BEFORE APPROVING THE REFUND. "
            "AFTER APPROVAL AND VERIFICATION RETURN THE PAYMENT WITHIN THREE WORKING DAYS. "
            "RETURN ADDRESS: 北京市朝阳区测试路一号.",
            "en",
        ),
        (
            "Los paquetes deben verificarse antes de aceptar la solicitud y calcular el importe del transporte.",
            "source",
        ),
        ("Les colis doivent être vérifiés avant de confirmer la demande et calculer les frais de transport.", "source"),
        ("返金申請では注文番号を確認し、商品が未開封であることを確認してください。", "source"),
        ("반품 신청 전에 주문 번호를 확인하고 제품 상태를 확인하세요.", "source"),
        ("9000 12000 6000", "source"),
        ("", "source"),
    ],
)
def test_source_prose_selects_chinese_obvious_english_or_conservative_fallback(tmp_path, text, language):
    assert resolve_output_language_policy([source(tmp_path, text)]) == {
        "version": 1, "basis": "frozen_original_source", "language": language,
    }


def test_chinese_technical_source_ignores_code_uris_and_machine_identifiers(tmp_path):
    english_code = "the user must check the policy before accepting the parcel" * 100
    text = (
        "# 包裹受理\n先核对原订单、付款凭证与商品状态，再检查重量和尺寸。"
        "任何材料缺失均暂停处理并转人工，不承诺退款或运输时效。\n"
        f"```python\n{english_code}\n```\n"
        f"~~~json\n{english_code}\n~~~\n"
        f"`{english_code}`\n"
        "source_uri: viking://resources/the/user/must/check/policy/before/accepting\n"
        "schema_version: team-replay.dataset\nproduct_id: TEST_SHIP_POLICY_99\n"
        "[核验依据](https://example.test/the/user/must/check/policy/before/accepting)\n"
    )
    assert resolve_output_language_policy([source(tmp_path, text)])["language"] == "zh-CN"


def test_structural_and_numbered_identifiers_do_not_outweigh_chinese_prose(tmp_path):
    text = (
        "包裹受理先核对原订单与付款凭证，任何材料缺失均暂停处理并转人工。\n"
        + "TEST-SHIP QA_EVIDENCE THE2026 SHOULD42 MUST99 " * 40
    )
    assert resolve_output_language_policy([source(tmp_path, text)])["language"] == "zh-CN"


def test_only_original_source_controls_language_not_human_or_candidate_context(tmp_path):
    english = source(tmp_path, "The user must check the policy and verify the weight before accepting a parcel.")
    chinese = "原始订单信息需要核对，缺少付款凭证必须暂停处理并转人工。" * 100
    human = source(tmp_path, chinese, "answers.md", kind="human")
    context = source(tmp_path, chinese, "SKILL.md", kind="context")
    assert resolve_output_language_policy([context, english, human])["language"] == "en"


def test_detection_reads_a_bounded_source_sample(tmp_path, monkeypatch):
    monkeypatch.setattr(output_language, "MAX_LANGUAGE_FILE_CHARS", 80)
    monkeypatch.setattr(output_language, "MAX_LANGUAGE_TOTAL_CHARS", 80)
    text = "The user must check the policy and verify the weight before accepting a parcel. "
    bounded = source(tmp_path, text + "大量中文不应改变已限定的检测文本。" * 100)
    skipped = {"kind": "source", "relative_path": "z-missing.md", "local_path": str(tmp_path / "missing")}
    assert resolve_output_language_policy([bounded, skipped])["language"] == "en"


def test_unreadable_original_source_does_not_silently_choose_a_language(tmp_path):
    with pytest.raises(ValueError, match="已冻结的原始来源"):
        resolve_output_language_policy([{"kind": "source", "local_path": str(tmp_path / "missing.md")}])


def test_instruction_applies_to_all_human_fields_and_preserves_identifiers():
    instruction = output_language_instruction({"version": 1, "basis": "frozen_original_source", "language": "zh-CN"})
    assert "所有轮次" in instruction and "简体中文" in instruction
    for field in (
        "description", "body", "task", "trigger", "inputs", "outputs", "boundary", "rationale",
        "deferred.reason", "question", "reason", "query", "check.text", "EVALUATION", "title", "Markdown heading",
    ):
        assert field in instruction
    assert "takes priority" in instruction and "previous candidates" in instruction
    assert "case_id" in instruction and "schema versions" in instruction and "source URIs" in instruction
    assert "original material text faithfully" in instruction and "must not invent facts" in instruction


@pytest.mark.parametrize("policy", [None, {}, {"version": True}, {
    "version": 1, "basis": "frozen_original_source", "language": [],
}, {"version": 1, "basis": "candidate", "language": "zh-CN"}])
def test_invalid_persisted_policy_is_rejected(policy):
    with pytest.raises(ValueError, match="语言策略无效"):
        output_language_instruction(policy)


@pytest.mark.parametrize("title,content", [
    ("Appointment", "中文正文"),
    ("电话约访", "## Completion\n完成后记录结果"),
    ("电话约访", "Completion\n----------\n完成后记录结果"),
])
def test_chinese_body_cannot_hide_untranslated_titles(title, content):
    with pytest.raises(ValueError, match="标题必须使用中文"):
        validate_artifact_headings(content, {"language": "zh-CN"}, title=title)


def test_chinese_headings_preserve_code_and_quoted_originals():
    original = "## 输入 API 参数\n> # Original Title\n\n```markdown\n# Request Example\n```\n## 完成条件\n记录结果"
    validate_artifact_headings(original, {"language": "zh-CN"}, title="电话约访方法")
    validate_artifact_headings("# Completion", {"language": "en"}, title="Appointment")


def test_internal_slug_with_chinese_report_suffix_is_not_a_chinese_display_title():
    with pytest.raises(ValueError, match="内部英文标识"):
        validate_artifact_headings(
            "# collect-financial-information 评估报告\n\n正文。", {"language": "zh-CN"},
            machine_name="collect-financial-information",
        )
    validate_artifact_headings(
        "# 收集客户财务信息评估报告\n\n内部标识：`collect-financial-information`。", {"language": "zh-CN"},
        machine_name="collect-financial-information",
    )


def test_chinese_dataset_view_keeps_machine_ids_out_of_headings_and_content_unchanged():
    from copy import deepcopy

    from team_miner.skill_miner.bounded_compile_plan import assemble_dataset
    from team_miner.skill_miner.dataset_view import dataset_markdown

    case = {"case_id": "call-case-01", "query": "请完成电话约访。", "checks": [
        {"id": "output-01", "kind": "output", "text": "先征得客户继续通话的同意。"}],
        "provenance": {"session_ids": ["viking://resources/original.md"]},
        "materials": [{"content": "> # Original Title"}]}
    document = assemble_dataset([case], skill_name="call", task="电话约访", identity="test", validate=False)
    before = deepcopy(document)
    rendered = dataset_markdown(document)
    assert document["name"] == "电话约访"
    assert "## 案例 1\n\n`call-case-01`" in rendered
    assert "### 案例材料" in rendered and case["query"] in rendered
    validate_artifact_headings(rendered, {"language": "zh-CN"})
    assert document == before
