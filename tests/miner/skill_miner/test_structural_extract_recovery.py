"""Worksheet labels must not consume model repair budgets or hide missing business rows."""

import copy
import hashlib
import json

import pytest

from team_miner.skill_miner import bounded_compile_plan as planning
from team_miner.skill_miner import source_unit_extraction as extraction
from team_miner.skill_miner.extract_recovery import prepare_recovery
from team_miner.skill_miner.ov_staged_orchestration import _read, _save
from tests.miner.skill_miner.support.source_unit_extraction import ExactExtractOwner, assignment, output_for


def without_structure(packet):
    value = output_for(packet)
    value["facts"] = []
    refs = packet["derived_controls"]["allowed_source_refs"]
    value["facts"] = [{"text": "原表列名", "source_refs": [ref]}
                      for ref, unit in zip(refs, packet["evidence"], strict=True) if unit["kind"] == "table_header"]
    return value


def test_sheet_label_is_retained_without_fabricating_model_facts_or_global_rules(tmp_path):
    _, context, packet = assignment(tmp_path, "## Sheet1\n\n| 步骤 |\n| --- |\n| 先征得同意 |\n")
    value = without_structure(packet)
    raw = copy.deepcopy(value)
    accepted = extraction.bind_source_unit_index(tmp_path, value, packet, context, "child")
    assert value == raw and accepted["value"]["context_refs"] == []
    assert len(accepted["value"]["facts"]) == 1  # No generated fact for the label or blank line.
    assert extraction.retained_structural_ids(tmp_path, accepted) == {
        unit["id"] for unit in packet["evidence"] if unit["kind"] in {"heading", "separator"}
    }
    # The generic/legacy validator still requires every source; only exact frozen syntax is exempt.
    with pytest.raises(ValueError, match="Every assigned source"):
        planning.validate_index({**value, "input_ids": packet["input_ids"]}, input_ids=packet["input_ids"],
                                source_refs=context["source_refs"], routing=True)


@pytest.mark.parametrize("text", [
    "## Sheet1：须先征得同意\n| 步骤 |\n| --- |\n| 完整业务步骤 |\n",
    "## 仅适用于已同意的客户\n| 步骤 |\n| --- |\n| 完整业务步骤 |\n",
    "Sheet1\n必须检查资格\n",
])
def test_business_headings_and_paragraphs_cannot_be_silently_retained(tmp_path, text):
    _, context, packet = assignment(tmp_path, text)
    value = without_structure(packet)
    with pytest.raises(ValueError, match="Every assigned source|must contain"):
        extraction.bind_source_unit_index(tmp_path, value, packet, context, "child")


def test_missing_business_row_and_tampered_syntax_fail_closed(tmp_path):
    _, context, packet = assignment(tmp_path, "## Sheet1\n\n| 步骤 |\n| --- |\n| 先同意 |\n| 再确认 |\n")
    value = without_structure(packet)
    missing = value["methods"].pop()
    with pytest.raises(ValueError, match="missing="):
        extraction.bind_source_unit_index(tmp_path, value, packet, context, "child")
    value["methods"].append(missing)
    accepted = extraction.bind_source_unit_index(tmp_path, value, packet, context, "child")
    accepted["structural_coverage"]["units"][0]["sha256"] = "0" * 64
    with pytest.raises(ValueError, match="structural source coverage"):
        extraction.retained_structural_ids(tmp_path, accepted)


def test_structure_only_unit_keeps_complete_routing_without_methods_or_evidence_gaps(tmp_path):
    from team_miner.skill_miner.source_scope import _local_unit

    _, context, packet = assignment(tmp_path, "## Sheet1\n\n")
    value = without_structure(packet)
    accepted = extraction.bind_source_unit_index(tmp_path, value, packet, context, "child")
    assert not any(accepted["value"][group] for group in ("methods", "facts", "deferred"))
    assert accepted["ordinals"] == [0]
    _save(tmp_path / "units/0/00000000.json", accepted)
    chunk = _read(tmp_path / "chunks/00000000.json")
    _save(tmp_path / "chunk-metadata/00000000.json", {key: value for key, value in chunk.items() if key != "content"})
    routed = _local_unit(tmp_path, 0, 1)
    assert routed["classification_complete"] and routed["method_ordinals"] == []


class SheetOwner(ExactExtractOwner):
    def new(self, count=1):
        job, entries = super().new(count)
        for entry in entries:
            from pathlib import Path

            path = Path(entry["local_path"])
            text = path.read_text().replace("# 方法 0", "## Sheet1")
            path.write_text(text)
            entry.update(sha256=hashlib.sha256(text.encode()).hexdigest(), content_bytes=len(text.encode()))
        return job, entries


@pytest.mark.asyncio
async def test_fresh_extraction_accepts_structure_omission_on_first_response(tmp_path):
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
    from tests.miner.skill_miner.support.source_unit_extraction import POLICY

    owner = SheetOwner(tmp_path)
    job, entries = owner.new(1)
    job = await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, "")
    root, state = await owner._staged_load(job)
    current = state["current"]
    for row in _read(root / current["packet_file"]):
        packet = _read(root / row["local_path"])
        uri = current["output_uri"] + "/" + row["output_path"]
        owner.ov.contents[uri] = json.dumps(without_structure(packet), ensure_ascii=False)
    job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    assert job["status"] == "waiting_human", job.get("error")
    assert len(owner.ov.calls) == 1
    for path in (root / "extract-batches/outcomes").glob("*.json"):
        assert _read(path)["valid"]


async def old_failure(owner, monkeypatch):
    from team_miner.skill_miner.ov_compile_mining import SKILL_BODY
    from tests.miner.skill_miner.support.source_unit_extraction import POLICY

    job, entries = owner.new(1)
    job = await owner._prepare_staged_round(job, SKILL_BODY, entries, POLICY, "")
    for _ in range(3):
        root, state = await owner._staged_load(job)
        current = state["current"]
        row = next(row for row in _read(root / current["packet_file"]) if row["context"]["slot"] == 1)
        packet = _read(root / row["local_path"])
        uri = current["output_uri"] + "/" + row["output_path"]
        output = json.loads(owner.ov.contents[uri])
        neutral = {ref for ref, unit in zip(row["context"]["source_refs"], packet["evidence"], strict=True)
                   if extraction.neutral_structure(unit)}
        output["methods"][0]["source_refs"] = [ref for ref in row["context"]["source_refs"] if ref not in neutral]
        owner.ov.contents[uri] = json.dumps(output, ensure_ascii=False)
        with monkeypatch.context() as old:
            old.setattr(extraction, "neutral_structure", lambda _: False)
            job = await owner._advance_staged_round(job, owner.ov.tasks[job["ov_task_id"]])
    assert job["status"] == "failed", job.get("error")
    job = owner.repo.update_job(job["job_id"], ov_status="completed")
    root, state = await owner._staged_load(job)
    return job, root, state


@pytest.mark.asyncio
async def test_structural_failure_recovers_original_results_without_retry_or_sibling_changes(tmp_path, monkeypatch):
    owner = SheetOwner(tmp_path)
    job, root, state = await old_failure(owner, monkeypatch)
    files = {path: path.read_bytes() for path in root.rglob("*") if path.is_file() and (
        "children" in path.parts or "outcomes" in path.parts or "accepted" in path.parts or "units" in path.parts
    )}
    calls = len(owner.ov.calls)
    assert owner.repo.public_job(job)["can_resume"]
    journal = prepare_recovery(root, state, job, apply=True)
    assert len(journal["entries"]) == 1
    prepare_recovery(root, _read(root / "state.json"), job, apply=True)  # Process exit before job update.
    assert all(path.read_bytes() == data for path, data in files.items())
    job = owner.repo.resume_identity_failure(job["job_id"])
    root, state = await owner._staged_load(job)
    assert state["current"] is None
    ledger = _read(root / state["extract_batch_pending"])
    assert all(slot["status"] == "valid" for slot in ledger["slots"])
    assert ledger["slots"][1]["format_attempts"] == 3
    job = await owner._staged_next(job, root, state)
    assert job["status"] == "waiting_human", job.get("error")
    assert len(owner.ov.calls) == calls  # Revalidated the complete original output, no new Compile.
    again = {**job, "status": "failed", "error": journal["error"], "finished_at": "different-failure"}
    with pytest.raises(ValueError, match="本次失败"):
        prepare_recovery(root, _read(root / "state.json"), again, apply=True)


@pytest.mark.asyncio
async def test_structural_recovery_rejects_changed_raw_result(tmp_path, monkeypatch):
    job, root, state = await old_failure(SheetOwner(tmp_path), monkeypatch)
    current = state["current"]
    row = _read(root / current["packet_file"])[0]
    path = root / "children" / current["child"] / (row["output_path"] + ".raw")
    path.write_text(path.read_text().replace("退款复核", "已改业务"))
    with pytest.raises(ValueError, match="SHA"):
        prepare_recovery(root, state, job, apply=True)
    assert not (root / "recovery/extract-structural-coverage-v1.json").exists()
