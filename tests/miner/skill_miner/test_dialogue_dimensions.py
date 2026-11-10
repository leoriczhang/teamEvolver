"""Source preservation, partial child isolation, bounded plans and lost-response recovery."""

import io
import json
from types import SimpleNamespace

import openpyxl
import pytest

from team_miner.skill_miner.dialogue_dimensions import orchestration
from team_miner.skill_miner.dialogue_dimensions import recovery as dialogue_recovery
from team_miner.skill_miner.dialogue_dimensions.assembly import assemble
from team_miner.skill_miner.dialogue_dimensions.compiler import SKILL_BODY
from team_miner.skill_miner.dialogue_dimensions.dataset import export
from team_miner.skill_miner.dialogue_dimensions.input import (
    canonical,
    case_key,
    parse_model,
    read_jsonl,
    read_xlsx,
    render,
    sha,
)
from team_miner.skill_miner.dialogue_dimensions.partition import batches, options
from team_miner.skill_miner.dialogue_dimensions.state import Journal
from team_miner.skill_miner.dialogue_dimensions.validation import ReceiptError, findings, merge
from team_miner.skill_miner.native_compiler import freeze_assets, frozen_assets
from tests.miner.skill_miner.support.dialogue_dimensions import Host, MemoryJournal, case, finding


def test_score_and_evaluation_changes_do_not_change_input_or_business_version():
    headers = ["对练id", "实例id", "课程名称", "章节名称", "知识点", "场景名称", "对话内容",
               "最终得分", "综合评价", "任务完成情况", "改进建议", "对练报告评分-中文"]
    def workbook(score):
        book = openpyxl.Workbook()
        book.active.append(headers)
        book.active.append(["001", 2.0, "课", "章", "点", "场", "HWP：评分100，金额1.25元！\r\nC：好……。",
                            score, score, score, score, score])
        output = io.BytesIO()
        book.save(output)
        output.seek(0)
        return output
    one = list(read_xlsx(workbook("old-private-evaluation"), filename="same.xlsx"))[0]
    two = list(read_xlsx(workbook("new-private-evaluation"), filename="same.xlsx"))[0]
    assert one[0] == two[0]
    assert one[1]["dialogue_id"] == "001"
    assert one[1]["instance_id"] == "2"
    model = render(one[0])
    assert "private-evaluation" not in model
    assert "金额1.25元" in model
    assert parse_model(model)["dialogue_text"] == one[0]["dialogue_text"]


@pytest.mark.parametrize("text", ["HWP：一🙂！\r\nC：那个个，…。\n", "HWP：```json\n{}\n```\nC：无标点",
                                  "无角色说话\n还有续行。"])
def test_wrapper_and_unicode_coordinates_preserve_original(text):
    value = case(text)
    parsed = parse_model(render(value))
    assert parsed["dialogue_text"] == text
    assert parsed == value
    for span in value["sentence_index"]:
        assert text[span["char_start"]:span["char_end"]]
    with pytest.raises(ValueError):
        parse_model(render(value).replace('"course":', '"最终得分":', 1))


def test_default_quote_missing_and_zero_findings_are_valid():
    value = case()
    accepted = findings(finding(value), value)
    assert accepted["observations"][0]["display_quotes"] == []
    assert not accepted["issues"]
    assert findings(finding(value, observations=[], empty_reason="没有可复用行为"), value)["observations"] == []


def test_changed_quote_is_preserved_as_issue_and_restored_from_valid_sentence():
    value = case("HWP：那个个，您好……。\nC：好。")
    observation = {"behavior": "问候", "sentence_refs": ["s000001"],
                   "quote": {"sentence_ids": ["s000001"], "text": "那个，您好。"}}
    accepted = findings(finding(value, observations=[observation]), value)
    assert accepted["issues"][0]["submitted"]["text"] == "那个，您好。"
    restored = accepted["observations"][0]["display_quotes"][0]
    assert restored["text"] == "那个个，您好……。"
    assert restored["audit"] == "host_restored_from_sentence_refs"
    assert accepted["observations"][0]["needs_review"]


def test_unique_quote_relocation_and_unlocatable_behavior_are_local():
    value = case("HWP：独特句子。\nC：好。")
    accepted = findings(finding(value, observations=[
        {"behavior": "问候", "quote": {"text": "独特句子。", "char_start": 999, "char_end": 1000}},
        {"behavior": "不能确定位置", "sentence_refs": ["not-real"]}]), value)
    assert len(accepted["observations"]) == 1
    assert accepted["observations"][0]["display_quotes"][0]["char_start"] == 4
    assert len(accepted["isolated"]) == 1
    with pytest.raises(ReceiptError):
        findings(finding(value, case_version="0" * 64), value)


def test_capacity_limits_keep_complete_cases_and_pending_records(tmp_path):
    values = [case("HWP：" + "字" * i + "。", i) for i in (10, 11, 100)]
    items = [{"content": render(v), "dialogue_chars": len(v["dialogue_text"]), "case": v} for v in values]
    limits = options({"extract_input_chars": len(items[0]["content"]) + len(items[1]["content"]) - 1,
                      "extract_input_bytes": 100000, "max_case_chars": 50})
    result = list(batches(items, limits))
    assert sorted(len(group) for group, _ in result) == [0, 1, 1]
    assert sum(len(pending) for _, pending in result) == 1
    manifest = export(((v, {"row": i}, []) for i, v in enumerate(values)), tmp_path / "export", limits)
    assert manifest["case_count"] == 3
    assert manifest["capacity_pending_count"] == 1


def test_unknown_jsonl_fields_are_whitelisted_and_missing_rows_remain_pending(tmp_path):
    value = case()
    records = read_jsonl([canonical({**value, "最终得分": 100, "综合评价": "private"}), '{"dialogue_text":""}'])
    manifest = export(records, tmp_path / "export")
    assert manifest["row_count"] == 2 and manifest["case_count"] == 1
    text = (tmp_path / "export" / manifest["pages"][0]["issues"]["path"]).read_text()
    assert "input_pending" in text
    assert "private" not in render(value)


def test_merge_does_not_have_to_reduce_and_never_invents_similar_title_membership():
    value = case()
    members = findings(finding(value, observations=[
        {"behavior": "问候", "sentence_refs": ["s000001"]},
        {"behavior": "问候但条件不同", "sentence_refs": ["s000002"]}]), value)["observations"]
    window = {"window_id": "w1", "partition": {"knowledge_point": "点"},
              "members": [{**v, "member_ref": v["observation_ref"]} for v in members]}
    receipt = merge(canonical({"window_id": "w1", "kept_separate": [
        {"member_ref": m["member_ref"], "reason": "条件不同"} for m in window["members"]]}), window)
    dimensions, links = assemble(window, receipt)
    assert len(dimensions) == len(links) == 2
    assert assemble(window, receipt) == (dimensions, links)
    with pytest.raises(ReceiptError):
        merge(canonical({"groups": [{"definition": "x", "member_refs": ["foreign"]}]}), window)
    with pytest.raises(ReceiptError):
        merge('{"groups":[]}', window)


def test_frozen_skill_assets_reject_mutation(tmp_path):
    freeze_assets(tmp_path, SKILL_BODY)
    assert "CONTRACT.yaml" in frozen_assets(tmp_path, SKILL_BODY)
    (tmp_path / "compiler-assets" / "references/extract.md").write_text("changed")
    with pytest.raises(ValueError, match="已改变"):
        frozen_assets(tmp_path, SKILL_BODY)


@pytest.mark.asyncio
async def test_lost_post_acknowledgement_reuses_original_key_after_host_restart(monkeypatch):
    value = case()
    job = {"job_id": "job1", "account": "account", "current_round": 1, "compiler_body_snapshot": SKILL_BODY,
           "dialogue_options": options(), "status": "preparing"}
    host, journal = Host(job), MemoryJournal()
    uri = "viking://resources/source/case.md"
    host.remote[uri] = render(value)
    unit = {"stage": "extract", "number": 1, "files": [{"uri": uri, "sha256": sha(render(value)), "case": value}],
            "attempt": 0, "status": "pending", "accepted": {}}
    async def provision(*_):
        return "viking://agent/skills/frozen"
    monkeypatch.setattr(orchestration, "provision", provision)
    host.lose_response = True
    with pytest.raises(ConnectionError):
        await orchestration.submit(host, job, journal, unit)
    persisted = await journal.get("extract/1")
    assert persisted["key"] in host.admission.live and len(host.posts) == 1
    restarted = Host(host.job)
    restarted.posts, restarted.remote, restarted.admission = host.posts, host.remote, host.admission
    accepted = await orchestration.submit(restarted, restarted.job, journal, persisted)
    assert accepted["task_id"] == next(iter(host.posts.values()))
    assert len(restarted.posts) == 1


@pytest.mark.asyncio
async def test_partial_children_are_preserved_without_retrying_successful_case(monkeypatch):
    one, two = case(number=1), case(number=2)
    host = Host({"job_id": "j", "account": "a"})
    journal = MemoryJournal()
    target = "viking://resources/job/output"
    unit = {"stage": "extract", "number": 1, "attempt": 0, "accepted": {},
            "request": {"to": target}, "files": [{"case": one}, {"case": two}]}
    path = f"/analysis/dialogue_dimensions/receipts/{one['case_ref']}-{one['case_version']}.json"
    host.remote[target + path] = finding(one)
    async def artifact(*_):
        pass
    monkeypatch.setattr(orchestration, "artifact", artifact)
    result = await orchestration.collect(host, host.job, journal, unit)
    assert result["status"] == "invalid"
    assert set(result["accepted"]) == {case_key(one)}
    assert result["errors"][0]["case_ref"] == two["case_ref"]


@pytest.mark.asyncio
async def test_versions_of_same_case_do_not_overwrite_each_others_receipts(monkeypatch):
    one, two = case("HWP：版本一。"), case("HWP：版本二。")
    assert one["case_ref"] == two["case_ref"] and one["case_version"] != two["case_version"]
    host = Host({"job_id": "j", "account": "a"})
    target = "viking://resources/job/output"
    unit = {"stage": "extract", "number": 1, "attempt": 0, "accepted": {},
            "request": {"to": target}, "files": [{"case": one}, {"case": two}]}
    for value in (one, two):
        path = f"/analysis/dialogue_dimensions/receipts/{value['case_ref']}-{value['case_version']}.json"
        host.remote[target + path] = finding(value)
    async def artifact(*_):
        pass
    monkeypatch.setattr(orchestration, "artifact", artifact)
    result = await orchestration.collect(host, host.job, MemoryJournal(), unit)
    assert result["status"] == "accepted"
    assert set(result["accepted"]) == {case_key(one), case_key(two)}


@pytest.mark.asyncio
async def test_full_bounded_pipeline_terminates_with_no_forced_merge_and_survives_restart(tmp_path, monkeypatch):
    values = [case(number=i) for i in range(20)]
    directory = tmp_path / "export"
    manifest = export(((v, {"row": i}, []) for i, v in enumerate(values)), directory)
    root = "viking://resources/dataset"
    manifest_raw = (directory / "dataset_manifest.json").read_bytes().decode()
    job = {"job_id": "j", "account": "a", "current_round": 1, "compiler_body_snapshot": SKILL_BODY,
           "dataset_manifest_ref": root + "/dataset_manifest.json", "dataset_manifest_sha256": sha(manifest_raw),
           "input_dir": root + "/model_cases", "dialogue_options": options(), "status": "preparing",
           "runtime_timeout_seconds": 3600, "dialogue_output_root": "viking://resources/j/analysis/dialogue_dimensions"}
    host = Host(job)
    for file in directory.rglob("*"):
        if file.is_file():
            host.remote[root + "/" + file.relative_to(directory).as_posix()] = file.read_bytes().decode()
    journal = MemoryJournal()
    projections = {}
    async def provision(*_):
        return "viking://agent/skills/frozen"
    async def artifact(_, current, path, text):
        key = current["dialogue_output_root"] + "/" + path
        assert key not in projections or projections[key] == text
        projections[key] = text
    async def write(_, __, uri, text):
        host.remote[uri] = text
    monkeypatch.setattr(orchestration, "Journal", lambda *_: journal)
    monkeypatch.setattr(orchestration, "provision", provision)
    monkeypatch.setattr(orchestration, "artifact", artifact)
    monkeypatch.setattr(orchestration, "write", write)
    for number in range(200):
        await orchestration.tick(host, host.job)
        if host.job["status"] in {"succeeded", "interrupted", "failed"}:
            break
        if number == 3:
            restarted = Host(host.job)
            restarted.remote, restarted.posts, restarted.requests = host.remote, host.posts, host.requests
            restarted.admission = host.admission
            host = restarted
    assert host.job["status"] == "succeeded", host.job
    assert host.job["processed_case_count"] == manifest["case_count"] == 20
    assert host.job["dimension_count"] == 20  # Keeping every member separate terminates normally.
    assert host.job["pending_case_count"] == 0
    assert len(host.posts) == manifest["extract_unit_count"] + 2
    assert all(len(request["from"]) <= 8 for request in host.requests)
    assert all("/model_cases/" in uri for r in host.requests[:3] for uri in r["from"])
    validation = json.loads(projections[job["dialogue_output_root"] + "/validation.json"])
    assert validation["delivery_status"] == "complete" and validation["content_validated"] is False
    assert not host.admission.live


@pytest.mark.asyncio
async def test_partial_delivery_resume_only_runs_missing_case_and_changed_window(tmp_path, monkeypatch):
    values = [case(number=i) for i in range(20)]
    directory = tmp_path / "export"
    export(((v, {"row": i}, []) for i, v in enumerate(values)), directory)
    root = "viking://resources/dataset"
    raw = (directory / "dataset_manifest.json").read_bytes().decode()
    job = {"job_id": "j", "account": "a", "current_round": 1, "compiler_body_snapshot": SKILL_BODY,
           "dataset_manifest_ref": root + "/dataset_manifest.json", "dataset_manifest_sha256": sha(raw),
           "input_dir": root + "/model_cases", "dialogue_options": options({"repair_attempts_per_unit": 0}),
           "status": "preparing", "runtime_timeout_seconds": 3600,
           "dialogue_output_root": "viking://resources/j/analysis/dialogue_dimensions"}
    host = Host(job)
    host.omit_cases.add(case_key(values[-1]))
    for file in directory.rglob("*"):
        if file.is_file():
            host.remote[root + "/" + file.relative_to(directory).as_posix()] = file.read_bytes().decode()
    journals, projections = {}, {}
    def journal(_, current):
        return journals.setdefault(current.get("dialogue_recovery_epoch", 0), MemoryJournal())
    async def provision(*_):
        return "viking://agent/skills/frozen"
    async def artifact(_, current, path, text):
        key = current["dialogue_output_root"] + "/" + path
        assert key not in projections or projections[key] == text
        projections[key] = text
    async def write(_, __, uri, text):
        host.remote[uri] = text
    monkeypatch.setattr(orchestration, "Journal", journal)
    monkeypatch.setattr(dialogue_recovery, "Journal", journal)
    monkeypatch.setattr(orchestration, "provision", provision)
    monkeypatch.setattr(orchestration, "artifact", artifact)
    monkeypatch.setattr(orchestration, "write", write)
    for _ in range(200):
        await orchestration.tick(host, host.job)
        if host.job["status"] == "interrupted":
            break
    assert host.job["delivery_status"] == "partial"
    assert host.job["processed_case_count"] == 19 and host.job["pending_case_count"] == 1
    before = len(host.posts)
    host.omit_cases.clear()
    await dialogue_recovery.resume(host, host.job, {"request_id": "resume-once"})
    for _ in range(200):
        await orchestration.tick(host, host.job)
        if host.job["status"] in {"succeeded", "interrupted"}:
            break
    assert host.job["status"] == "succeeded"
    assert host.job["processed_case_count"] == 20 and host.job["pending_case_count"] == 0
    assert len(host.posts) - before == 2  # One missing case and only its changed merge window.
    assert host.job["dimension_count"] == 20
    assert len([key for key in projections if key.endswith("/manifest.json")]) == 2


@pytest.mark.postgres
def test_pg_unit_journal_survives_new_owner_and_is_tenant_scoped(pg_env):
    async def run():
        async def guard(_):
            pass
        engine = SimpleNamespace(services=SimpleNamespace(tx=pg_env.tx), tenant="default", _guard_connection=guard)
        journal = Journal(engine, {"job_id": "dialogue-test"})
        await journal.save(**{"extract/1": {"key": "original", "task_id": "remote1", "accepted": {"case1": {}}}})
        next_owner = Journal(engine, {"job_id": "dialogue-test"})
        assert (await next_owner.get("extract/1"))["task_id"] == "remote1"
        engine.tenant = "alpha"
        assert await Journal(engine, {"job_id": "dialogue-test"}).get("extract/1") is None
    pg_env.tx.pool.run(run())
