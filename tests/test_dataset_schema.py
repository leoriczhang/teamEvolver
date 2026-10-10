"""Canonical Test Dataset contract and storage tests."""

import json

import pytest

from team_replay.datasets.schema import (
    DATASET_SCHEMA,
    DatasetSchemaError,
    make_document,
    normalize_document,
    output_requirements,
    trajectory_requirements,
    validate_document,
)
from team_replay.datasets.store import SkillDatasetStore, dataset_material_integrity
from team_replay.lab.service import SkillLabStore
from team_replay.policy import normalize_case_checklist, progressive_config
from teamEvolver.storage.local import LocalObjectStore


def case(
    case_id: str = "case-1",
    *,
    skill_ids: list[str] | None = None,
    query: str = "完成任务",
    checks: list[dict] | None = None,
    materials: list[dict] | None = None,
) -> dict:
    return {
        "case_id": case_id,
        "query": query,
        "skill_ids": skill_ids or [],
        "checks": checks or [{"id": "R01", "kind": "output", "text": "生成报告"}],
        "materials": materials or [],
        "provenance": {},
        "replay": {"progressive_disclosure": {"batch_size": 2}},
        "metadata": {},
    }


def document(dataset_id: str = "dataset-1", *, skills: list[str] | None = None, cases: list[dict] | None = None) -> dict:
    return make_document(
        dataset_id=dataset_id,
        name="测试数据集",
        skills=skills or [],
        source={"kind": "manual"},
        cases=cases or [case(skill_ids=skills)],
        metadata={},
    )


def test_current_document_has_one_unversioned_shape():
    value = normalize_document(document(skills=["demo"]))

    assert value["schema_version"] == DATASET_SCHEMA == "team-replay.dataset"
    assert set(value["cases"][0]) == {
        "case_id", "query", "skill_ids", "checks", "materials",
        "provenance", "replay", "metadata",
    }
    assert output_requirements(value["cases"][0]) == ["生成报告"]
    assert trajectory_requirements(value["cases"][0]) == []
    assert validate_document(value) == []


@pytest.mark.parametrize(
    "payload",
    [
        {"schema_version": "team-replay.dataset.v2", "dataset_id": "old", "name": "old", "cases": []},
        {"schema_version": DATASET_SCHEMA, "dataset_id": "old", "name": "old", "items": []},
        {"dataset_id": "old", "skill_name": "demo", "query": "old", "requirements": ["old"]},
    ],
)
def test_old_dataset_shapes_are_rejected(payload):
    with pytest.raises(DatasetSchemaError):
        normalize_document(payload, allow_empty=True)


def test_skill_dataset_store_writes_only_current_documents(tmp_path):
    bucket = LocalObjectStore(tmp_path)
    store = SkillDatasetStore(bucket)
    saved = store.save_dataset(document("case-new", skills=["demo"]))
    raw = json.loads(bucket.get_object(store.dataset_key("", "case-new")).read())

    assert saved["schema_version"] == DATASET_SCHEMA
    assert raw == saved
    assert store.load_dataset(skill_name="demo", dataset_id="case-new") == saved
    assert store.load_dataset(skill_name="other", dataset_id="case-new") is None


def test_skill_dataset_store_supports_peer_skill_bindings(tmp_path):
    bucket = LocalObjectStore(tmp_path)
    store = SkillDatasetStore(bucket)
    saved = store.save_dataset(
        document(
            "shared-case",
            skills=["skill-a", "skill-b"],
            cases=[case(skill_ids=["skill-a", "skill-b"])],
        )
    )

    assert [item["skill_id"] for item in saved["skills"]] == ["skill-a", "skill-b"]
    assert store.load_dataset(skill_name="skill-a", dataset_id="shared-case")
    assert store.load_dataset(skill_name="skill-b", dataset_id="shared-case")
    assert store.load_dataset(skill_name="skill-c", dataset_id="shared-case") is None


def test_skill_lab_lists_one_dataset_through_each_peer_skill(tmp_path):
    store = SkillLabStore(LocalObjectStore(tmp_path))
    saved = store.save_dataset(
        document(
            "shared-case",
            skills=["skill-a", "skill-b"],
            cases=[case(skill_ids=["skill-a", "skill-b"])],
        )
    )

    assert store.list_datasets(skill_name="skill-a")[0]["dataset_id"] == saved["dataset_id"]
    assert store.list_datasets(skill_name="skill-b")[0]["dataset_id"] == saved["dataset_id"]


def test_multi_case_dataset_can_use_different_skill_subsets():
    value = document(
        "multi-task",
        skills=["skill-a", "skill-b", "skill-c"],
        cases=[
            case("task-1", skill_ids=["skill-a", "skill-b"]),
            case("task-2", skill_ids=["skill-b", "skill-c"]),
        ],
    )
    assert validate_document(value) == []

    invalid = {**value, "cases": [case("task", skill_ids=["unknown"])]}
    assert any("未在顶层声明" in error for error in validate_document(invalid))


def test_replay_policy_consumes_current_case_directly():
    value = case(
        checks=[
            {"id": "R01", "kind": "output", "text": "生成结果"},
            {"id": "T01", "kind": "trajectory", "text": "核验输入"},
        ]
    )

    assert normalize_case_checklist(value) == value["checks"]
    assert progressive_config(value)["batch_size"] == 2


def test_material_integrity_reads_materials_from_current_case():
    value = document(
        cases=[
            case(
                query="读取 input.csv 并生成报告",
                materials=[{"path": "input.csv", "size": 3, "sha256": "abc"}],
            )
        ]
    )

    integrity = dataset_material_integrity(value)
    assert integrity["complete"] is True
    assert integrity["available_paths"] == ["input.csv"]
