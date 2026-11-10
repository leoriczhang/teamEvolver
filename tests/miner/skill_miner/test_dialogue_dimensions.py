"""Source preservation, partial child isolation, bounded plans and lost-response recovery."""

import io
import json
from types import SimpleNamespace

import openpyxl
import pytest

from team_miner.skill_miner.dialogue_dimensions import orchestration
from team_miner.skill_miner.dialogue_dimensions import recovery as dialogue_recovery
from team_miner.skill_miner.dialogue_dimensions.assembly import assemble, dispositions
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


def test_separate_methods_do_not_become_dimensions_and_retain_provenance():
    value = case()
    members = findings(finding(value, observations=[
        {"behavior": "问候", "sentence_refs": ["s000001"]},
        {"behavior": "问候但条件不同", "sentence_refs": ["s000002"]}]), value)["observations"]
    window = {"window_id": "w1", "partition": {"knowledge_point": "点"},
              "members": [{**v, "member_ref": v["observation_ref"]} for v in members]}
    receipt = merge(canonical({"window_id": "w1", "kept_separate": [
        {"member_ref": m["member_ref"], "reason": "条件不同", "case_ref": "untrusted-extra",
         "source_spans": []} for m in window["members"]]}), window)
    dimensions, links = assemble(window, receipt)
    assert dimensions == links == []
    assert assemble(window, receipt) == (dimensions, links)
    retained = dispositions(window, receipt)
    assert len(retained) == 2
    for member, row in zip(window["members"], retained):
        assert row["disposition"] == "kept_separate"
        assert row["reason"] == "条件不同"
        assert row["behavior"] == member["behavior"]
        assert row["case_ref"] == member["case_ref"]
        assert row["case_version"] == member["case_version"]
        assert row["source_spans"] == member["source_spans"]
    with pytest.raises(ReceiptError):
        merge(canonical({"groups": [{"definition": "x", "member_refs": ["foreign"]}]}), window)
    with pytest.raises(ReceiptError):
        merge('{"groups":[]}', window)


@pytest.mark.parametrize("notes", [
    ["评分对象：评价开场联系的针对性。", "适用与缺证据：无机会为 N/A；上下文不足为 NE。",
     "候选0分：有开场机会却未回应客户。", "候选1分：仅作泛化问候。",
     "候选2分：通过当前情境建立联系。", "候选3分：根据客户反馈恰当调整联系方法。",
     "评分步骤：定位行为证据；不同方法可以替代。", "证据与校准：所有档位为候选，待校准。"],
    ["rubric_pending：当前 Evidence 尚不足以拟定有意义的档差。"],
    [],
])
def test_singleton_capability_preserves_candidate_guidance_without_hard_rubric_gate(notes):
    value = case()
    members = findings(finding(value, observations=[
        {"behavior": "根据当前情境问候", "sentence_refs": ["s000001"]},
        {"behavior": "说明未核实的信息", "sentence_refs": ["s000002"]}]), value)["observations"]
    window = {"window_id": "w1", "partition": {"knowledge_point": "点"},
              "members": [{**v, "member_ref": v["observation_ref"]} for v in members]}
    receipt = merge(canonical({
        "groups": [{"member_refs": [window["members"][0]["member_ref"]], "name": "开场关系建立",
                    "definition": "评价通过相关情境建立联系的能力，不评价权益披露。",
                    "observable_behaviors": ["选择适合当前情境的联系方法"], "notes": notes}],
        "deferred": [{"member_ref": window["members"][1]["member_ref"], "reason": "业务事实待核实"}],
    }), window)
    dimensions, links = assemble(window, receipt)
    assert len(dimensions) == len(links) == 1
    assert dimensions[0]["name"] == "开场关系建立"
    assert dimensions[0]["notes"] == [*notes, "single_support_case"]
    assert dimensions[0]["support_case_count"] == 1
    assert links[0]["observation_ref"] == window["members"][0]["member_ref"]
    assert links[0]["source_spans"] == window["members"][0]["source_spans"]
    retained = dispositions(window, receipt)
    assert retained[0]["disposition"] == "deferred"
    assert retained[0]["reason"] == "业务事实待核实"
    assert retained[0]["source_spans"] == window["members"][1]["source_spans"]


@pytest.mark.asyncio
async def test_provision_uses_user_saved_skill_without_backend_assets():
    custom = SKILL_BODY + "\n用户规则：保留条件不同的独立维度。\n"
    calls = []

    async def install(client, name, body):
        calls.append((client, name, body))

    host = SimpleNamespace(
        _ov_skill_compiler_body=lambda: custom,
        _round_client=lambda _: "client",
        _round_provision_compiler=install,
    )
    uri = await orchestration.provision(host, {"job_id": "test"})
    assert uri == "viking://agent/skills/" + calls[0][1]
    assert "用户规则：保留条件不同的独立维度。" in calls[0][2]
    assert "compiler_assets_sha256" not in calls[0][2]
    host._ov_skill_compiler_body = lambda: "---\nname: custom\ndescription: custom\n---\n其他方法"
    with pytest.raises(ValueError, match="protocol"):
        await orchestration.provision(host, {"job_id": "test"})
    assert len(calls) == 1


def test_initial_skill_contains_complete_editable_contract():
    assert "compiler_assets_sha256" not in SKILL_BODY
    assert "## stage=extract" in SKILL_BODY
    assert "## stage=merge" in SKILL_BODY
    assert "taikang.dialogue-findings.v1" in SKILL_BODY
    assert "taikang.dimension-merge.v1" in SKILL_BODY


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
    job = {"job_id": "j", "account": "a", "current_round": 1,
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
    assert host.job["dimension_count"] == 0  # Method-only findings still complete delivery.
    assert host.job["pending_case_count"] == 0
    assert len(host.posts) == manifest["extract_unit_count"] + 2
    assert all(len(request["from"]) == 1 for request in host.requests)
    for request in host.requests:
        assert request["to"] != request["from"][0]
        entries = host.entries(request["from"][0])
        assert 1 <= len(entries) <= 8
        assert all(not e["isDir"] for e in entries)
    assert sum(len(host.entries(r["from"][0])) for r in host.requests if "stage=extract;" in r["instruction"]) == 20
    validation = json.loads(projections[job["dialogue_output_root"] + "/validation.json"])
    assert validation["delivery_status"] == "complete" and validation["content_validated"] is False
    retained = [json.loads(line) for path, text in projections.items() if "/issues/merge-" in path
                for line in text.splitlines()]
    assert len(retained) == 20
    assert {row["case_ref"] for row in retained} == {v["case_ref"] for v in values}
    assert all(row["disposition"] == "kept_separate" and row["reason"] == "独立保留" for row in retained)
    assert all(row["sentence_refs"] and row["source_spans"] and row["behavior"] for row in retained)
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
    assert len(host.entries(host.requests[-2]["from"][0])) == 1
    assert host.job["dimension_count"] == 0
    assert len([key for key in projections if key.endswith("/manifest.json")]) == 2


@pytest.mark.asyncio
async def test_directory_staging_preserves_full_input_and_rejects_extra_files():
    value = case("HWP：那个个🙂。\r\nC：好……。\n")
    host = Host({"account": "a"})
    source = "viking://resources/source/case.md"
    host.remote[source] = render(value)
    selected = [{"uri": source, "sha256": sha(render(value))}]
    directory = "viking://resources/staged/attempt-input"
    staged = await orchestration.stage_inputs(host, host.job, directory, selected)
    assert directory in host.directories
    assert parse_model(host.remote[staged[0]["uri"]]) == value
    assert await orchestration.stage_inputs(host, host.job, directory, selected) == staged
    host.remote[directory + "/unselected.md"] = "unexpected source"
    with pytest.raises(ValueError, match="directory_changed"):
        await orchestration.stage_inputs(host, host.job, directory, selected)


@pytest.mark.asyncio
async def test_permanent_preflight_rejection_stops_other_batches_and_resume_clears_error(monkeypatch):
    values = [case(number=i) for i in range(3)]
    job = {"job_id": "rejected", "account": "a", "current_round": 1, "status": "preparing",
           "dialogue_options": options(), "dataset_manifest_ref": "viking://resources/source/manifest.json",
           "dataset_manifest_sha256": sha("{}"), "runtime_timeout_seconds": 3600,
           "dialogue_output_root": "viking://resources/output/dialogue"}
    host = Host(job)
    host.remote[job["dataset_manifest_ref"]] = "{}"
    host.reject_compile = (400, "Compile source must be a directory; api_key=private-test-secret")
    journals, projections = {0: MemoryJournal()}, {}
    state = {"stage": "extract", "cursor": 1, "extract_count": 3, "merge_count": 0, "case_count": 3,
             "processed": 0, "pending": 0, "usable": 0, "isolated": 0, "warnings": 0, "dimensions": 0,
             "blocked_units": 0, "merge_planning_cursor": 1, "merge_buffer": None, "page_count": 0}
    await journals[0].save(summary=state)
    for number, value in enumerate(values, 1):
        uri = f"viking://resources/source/{number}.md"
        host.remote[uri] = render(value)
        await journals[0].save(**{f"extract/{number}": {
            "number": number, "stage": "extract", "files": [{"uri": uri, "sha256": sha(render(value)), "case": value}],
            "attempt": 0, "status": "pending", "accepted": {}}})
    def journal(_, current):
        return journals.setdefault(current.get("dialogue_recovery_epoch", 0), MemoryJournal())
    async def provision(*_):
        return "viking://agent/skills/saved"
    async def artifact(_, current, path, text):
        projections[current["dialogue_output_root"] + "/" + path] = text
    monkeypatch.setattr(orchestration, "Journal", journal)
    monkeypatch.setattr(dialogue_recovery, "Journal", journal)
    monkeypatch.setattr(orchestration, "provision", provision)
    monkeypatch.setattr(orchestration, "artifact", artifact)
    for _ in range(30):
        await orchestration.tick(host, host.job)
        if host.job["status"] == "interrupted":
            break
    assert host.job["delivery_status"] == "partial"
    assert host.job["processed_case_count"] == 0 and host.job["pending_case_count"] == 3
    assert len(host.requests) == 1 and not host.posts and not host.admission.live
    assert "OV HTTP 400: Compile source must be a directory" in host.job["error"]
    assert "private-test-secret" not in json.dumps(projections)
    assert "private-test-secret" not in host.job["error"]
    assert "compile_request_rejected" in projections[job["dialogue_output_root"] + "/issues/extract-000001.jsonl"]
    host.reject_compile = None
    await dialogue_recovery.resume(host, host.job, {"request_id": "resume-rejection"})
    assert "submission_rejection" not in await journals[1].get("summary")
    assert "definitive_failure" not in await journals[1].get("extract/1")
    for _ in range(50):
        await orchestration.tick(host, host.job)
        if host.job["status"] in {"succeeded", "interrupted"}:
            break
    assert host.job["status"] == "succeeded" and host.job["processed_case_count"] == 3
    assert host.job["error"] == ""
    before = len(host.requests)
    await dialogue_recovery.resume(host, host.job, {"request_id": "resume-rejection"})
    assert len(host.requests) == before and host.job["dialogue_recovery_epoch"] == 1


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
