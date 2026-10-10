"""Protect strict mining validation of the canonical Test Dataset contract."""

from __future__ import annotations

import copy
import json

import pytest

from team_miner import benchmark_format as bf
from team_miner.tests.dataset_fixtures import build_document
from team_replay.datasets.schema import normalize_document
from team_replay.datasets.schema import validate_document as validate_canonical_document


def _document():
    return build_document("demo", [{
        "case_id": "case-1",
        "name": "Verify supplied Evidence",
        "query": "Handle the supplied case",
        "requirements": [f"Evidence condition {index}" for index in range(12)],
        "trajectory_requirements": ["Read the supplied material before deciding"],
        "materials": [{"path": "facts.txt", "content": "verified facts"}],
        "source_session_ids": ["session-1"],
    }])


def test_canonical_mining_dataset_roundtrips_without_changing_checks_or_materials(tmp_path):
    document = _document()
    assert validate_canonical_document(document) == []
    assert normalize_document(document) == document
    assert bf.validate_document(document, expected_skill_name="demo") == []
    before = copy.deepcopy(document)
    path = tmp_path / "benchmark.json"
    bf.write_document(path, document)
    loaded, errors = bf.read_document(path)
    assert not errors and loaded == before and document == before
    case = loaded["cases"][0]
    assert set(case) == {
        "case_id", "query", "skill_ids", "checks", "materials", "provenance", "replay", "metadata",
    }
    assert case["metadata"]["name"] == "Verify supplied Evidence"
    assert case["materials"][0]["content"] == "verified facts"
    assert case["provenance"]["session_ids"] == ["session-1"]


@pytest.mark.parametrize("count", [11, 25])
def test_short_or_excessive_output_checklists_are_rejected_without_automatic_completion(tmp_path, count):
    document = _document()
    case = document["cases"][0]
    trajectory = [check for check in case["checks"] if check["kind"] == "trajectory"]
    case["checks"] = [
        {"id": f"R{index}", "kind": "output", "text": f"Original requirement {index}"}
        for index in range(count)
    ] + trajectory
    before = copy.deepcopy(document)
    assert validate_canonical_document(document) == []
    assert any("12-24" in error for error in bf.validate_document(document))
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded, errors = bf.read_document(path)
    assert loaded is None and errors
    with pytest.raises(ValueError, match="12-24"):
        bf.write_document(path, document)
    assert document == before and json.loads(path.read_text("utf-8")) == before


@pytest.mark.parametrize("invalid", [
    "duplicate_output", "missing_trajectory", "numeric_check", "wrong_split", "wrong_target",
    "wrong_disclosure", "missing_revision", "wrong_skill", "old_schema", "old_case_field",
])
def test_mining_quality_and_canonical_format_errors_are_not_repaired(tmp_path, invalid):
    document = _document()
    case = document["cases"][0]
    if invalid == "duplicate_output":
        case["checks"][1]["text"] = case["checks"][0]["text"]
    elif invalid == "missing_trajectory":
        case["checks"] = [check for check in case["checks"] if check["kind"] == "output"]
    elif invalid == "numeric_check":
        case["checks"][0]["text"] = 12000
    elif invalid == "wrong_split":
        case["metadata"]["split"] = "train"
    elif invalid == "wrong_target":
        case["metadata"]["minimum_requirement_target"] = 11
    elif invalid == "wrong_disclosure":
        case["replay"]["progressive_disclosure"]["batch_size"] = 2
    elif invalid == "missing_revision":
        document["metadata"].pop("candidate_revision")
    elif invalid == "wrong_skill":
        case["skill_ids"] = ["undeclared-skill"]
    elif invalid == "old_schema":
        document["schema_version"] = "team-replay.dataset.v2"
    else:
        case["input"] = case["query"]
    before = copy.deepcopy(document)
    assert bf.validate_document(document)
    path = tmp_path / "benchmark.json"
    path.write_text(json.dumps(document), encoding="utf-8")
    loaded, errors = bf.read_document(path)
    assert loaded is None and errors
    with pytest.raises(ValueError):
        bf.write_document(path, document)
    assert document == before


def test_mining_validation_requires_the_candidate_skill_reference():
    assert any("skills 必须包含 expected" in error for error in bf.validate_document(
        _document(), expected_skill_name="expected"
    ))


@pytest.mark.parametrize("content", [b"not JSON", b"\xff", b"[]"])
def test_reader_reports_invalid_utf8_json_without_raising(tmp_path, content):
    path = tmp_path / "benchmark.json"
    path.write_bytes(content)
    document, errors = bf.read_document(path)
    assert document is None and errors
