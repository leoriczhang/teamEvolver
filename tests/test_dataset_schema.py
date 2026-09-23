"""Canonical Test Dataset v2 normalization and legacy compatibility."""

import json

import pytest

from team_replay.datasets.schema import (
    DATASET_SCHEMA_V2,
    legacy_case_view,
    make_document,
    normalize_document,
    output_requirements,
    trajectory_requirements,
    validate_document,
)
from team_replay.datasets.synthesis import SynthesizedDatasetStore
from team_replay.datasets.store import (
    SkillDatasetStore,
    SkillDatasetStoreError,
    dataset_material_integrity,
)
from team_replay.lab.service import SkillLabStore
from team_replay.policy import normalize_case_checklist, progressive_config
from teamEvolver.storage.local import LocalObjectStore


def test_progressive_document_normalizes_to_v2_without_duplicate_checklists():
    document = normalize_document({
        "schema_version": 1,
        "dataset_format": "teamEvolver-progressive-test-v1",
        "skill_name": "demo",
        "generation_id": "generation-1",
        "candidate_revision": 2,
        "source_session_ids": ["s1"],
        "created_at": "2026-09-20T00:00:00Z",
        "datasets": [{
            "dataset_id": "case-1",
            "name": "场景",
            "query": "完成任务",
            "requirements": ["生成报告"],
            "trajectory_requirements": ["先核验输入"],
            "checklist": [
                {"id": "R01", "kind": "output", "text": "生成报告"},
                {"id": "T01", "kind": "trajectory", "text": "先核验输入"},
            ],
            "source_session_ids": ["s1"],
            "progressive_disclosure": {"batch_size": 2},
        }],
    })

    assert document["schema_version"] == DATASET_SCHEMA_V2
    assert document["dataset_id"] == "generation-1"
    assert document["skills"] == [{"skill_id": "demo"}]
    assert set(document["cases"][0]) == {
        "case_id", "name", "query", "skill_ids", "checks", "materials",
        "provenance", "replay", "metadata",
    }
    assert document["cases"][0]["skill_ids"] == ["demo"]
    assert document["cases"][0]["checks"] == [
        {"id": "R01", "kind": "output", "text": "生成报告"},
        {"id": "T01", "kind": "trajectory", "text": "先核验输入"},
    ]
    assert validate_document(document) == []


def test_session_collection_and_skill_dataset_share_the_same_document_shape():
    session_document = normalize_document({
        "schema_version": "team-replay.session-dataset.v1",
        "dataset_id": "ds-session",
        "name": "Session 集合",
        "description": "",
        "source": {"kind": "sessions"},
        "created_at": "2026-09-20T00:00:00Z",
        "updated_at": "2026-09-20T00:00:00Z",
        "items": [{
            "item_id": "item-1",
            "session_id": "s1",
            "trace_id": "t1",
            "title": "任务",
            "query": "生成报告",
            "requirements": ["包含结论"],
            "timestamp": "2026-09-19T00:00:00Z",
            "ingested_at": "2026-09-20T00:00:00Z",
            "session": {"session_id": "s1", "turns": []},
        }],
    })
    skill_document = normalize_document({
        "dataset_id": "skill-case",
        "dataset_format": "teamEvolver-skill-dataset-v1",
        "skill_name": "demo",
        "name": "Skill Case",
        "query": "执行技能",
        "requirements": "1. 生成结果\n2. 核验结果",
        "trajectory_requirements": "1. 读取材料",
    })

    assert set(session_document) == set(skill_document)
    assert session_document["schema_version"] == DATASET_SCHEMA_V2
    assert skill_document["schema_version"] == DATASET_SCHEMA_V2
    assert output_requirements(skill_document["cases"][0]) == ["生成结果", "核验结果"]
    assert trajectory_requirements(skill_document["cases"][0]) == ["读取材料"]
    assert session_document["cases"][0]["provenance"]["session_snapshot"]["session_id"] == "s1"


def test_legacy_case_view_is_only_a_boundary_projection():
    document = normalize_document({
        "dataset_id": "case-1",
        "skill_name": "demo",
        "query": "完成任务",
        "requirements": ["生成报告"],
        "trajectory_requirements": ["检查输入"],
    })

    view = legacy_case_view(document["cases"][0], document=document, text_requirements=True)

    assert view["dataset_format"] == DATASET_SCHEMA_V2
    assert view["skill_name"] == "demo"
    assert view["requirements"] == "生成报告"
    assert view["trajectory_requirements"] == "检查输入"


def test_skill_dataset_store_writes_v2_and_reads_legacy_entries(tmp_path):
    bucket = LocalObjectStore(tmp_path)
    store = SkillDatasetStore(bucket)

    saved = store.save_dataset({
        "dataset_id": "case-new",
        "skill_name": "demo",
        "name": "新用例",
        "query": "生成报告",
        "requirements": "1. 包含结论",
        "trajectory_requirements": "1. 核验输入",
    })
    raw = json.loads(bucket.get_object(store.dataset_key("demo", "case-new")).read())

    assert raw["schema_version"] == DATASET_SCHEMA_V2
    assert "cases" in raw and "requirements" not in raw
    assert saved["requirements"] == ["包含结论"]

    bucket.put_object(
        store.legacy_dataset_key("demo", "case-old"),
        json.dumps({
            "dataset_id": "case-old",
            "dataset_format": "teamEvolver-skill-dataset-v1",
            "skill_name": "demo",
            "name": "旧用例",
            "query": "处理旧任务",
            "requirements": "完成任务",
        }).encode(),
    )

    legacy = store.load_dataset(skill_name="demo", dataset_id="case-old")
    assert legacy is not None
    assert legacy["dataset_format"] == DATASET_SCHEMA_V2
    assert legacy["requirements"] == ["完成任务"]

    with pytest.raises(SkillDatasetStoreError, match="checks"):
        store.save_dataset({
            "dataset_id": "case-empty",
            "skill_name": "demo",
            "name": "空 Checklist",
            "query": "处理任务",
        })


def test_skill_dataset_store_supports_peer_skill_bindings(tmp_path):
    bucket = LocalObjectStore(tmp_path)
    store = SkillDatasetStore(bucket)

    saved = store.save_dataset({
        "dataset_id": "shared-case",
        "skill_ids": ["skill-a", "skill-b"],
        "name": "协作任务",
        "query": "完成跨技能任务",
        "requirements": ["生成最终结果"],
    })

    assert saved["skill_ids"] == ["skill-a", "skill-b"]
    assert saved["skill_name"] == ""
    assert store.load_dataset(skill_name="skill-a", dataset_id="shared-case")
    assert store.load_dataset(skill_name="skill-b", dataset_id="shared-case")
    assert store.load_dataset(skill_name="skill-c", dataset_id="shared-case") is None
    assert [item["dataset_id"] for item in store.list_datasets(skill_name="skill-a")] == [
        "shared-case"
    ]
    raw = json.loads(
        bucket.get_object(store.dataset_key("", "shared-case")).read()
    )
    assert raw["skills"] == [
        {"skill_id": "skill-a"},
        {"skill_id": "skill-b"},
    ]
    assert raw["cases"][0]["skill_ids"] == ["skill-a", "skill-b"]


def test_skill_lab_lists_one_dataset_through_each_peer_skill(tmp_path):
    store = SkillLabStore(LocalObjectStore(tmp_path))

    saved = store.save_dataset({
        "skill_name": "skill-a",
        "skill_ids": ["skill-a", "skill-b"],
        "name": "共享数据集",
        "query": "执行协作任务",
        "requirements": "完成任务",
    })

    assert saved["skill_ids"] == ["skill-a", "skill-b"]
    assert store.list_datasets(skill_name="skill-a")[0]["dataset_id"] == saved["dataset_id"]
    assert store.list_datasets(skill_name="skill-b")[0]["dataset_id"] == saved["dataset_id"]


def test_multi_task_dataset_can_use_different_skill_subsets(tmp_path):
    bucket = LocalObjectStore(tmp_path)
    store = SkillDatasetStore(bucket)
    store.save_dataset({
        "schema_version": DATASET_SCHEMA_V2,
        "dataset_id": "multi-task",
        "name": "多任务数据集",
        "skills": ["skill-a", "skill-b", "skill-c"],
        "source": {"kind": "manual"},
        "cases": [
            {
                "case_id": "task-1",
                "name": "任务一",
                "query": "执行任务一",
                "skill_ids": ["skill-a", "skill-b"],
                "checks": [{"id": "R01", "kind": "output", "text": "完成任务一"}],
            },
            {
                "case_id": "task-2",
                "name": "任务二",
                "query": "执行任务二",
                "skill_ids": ["skill-b", "skill-c"],
                "checks": [{"id": "R01", "kind": "output", "text": "完成任务二"}],
            },
        ],
    })

    document = store.load_document(skill_name="skill-c", dataset_id="multi-task")
    assert document is not None
    assert len(document["cases"]) == 2
    assert document["cases"][0]["skill_ids"] == ["skill-a", "skill-b"]
    assert document["cases"][1]["skill_ids"] == ["skill-b", "skill-c"]

    invalid = make_document(
        dataset_id="invalid",
        name="invalid",
        skills=["skill-a"],
        cases=[{
            "case_id": "task",
            "query": "run",
            "skill_ids": ["skill-b"],
            "checks": [{"id": "R01", "kind": "output", "text": "done"}],
        }],
    )
    assert any("未在顶层声明" in error for error in validate_document(invalid))


def test_evolution_generation_and_skill_copy_both_write_v2(tmp_path):
    bucket = LocalObjectStore(tmp_path)
    store = SynthesizedDatasetStore(bucket)
    case = normalize_document({
        "dataset_id": "case-1",
        "skill_name": "demo",
        "query": "生成报告",
        "requirements": ["包含结论"],
        "trajectory_requirements": ["核验输入"],
        "source_session_ids": ["s1"],
    })["cases"][0]

    generation = store.save_generation(
        skill_name="demo",
        generation_id="generation-1",
        datasets=[case],
        source_session_ids=["s1"],
        candidate_revision=3,
    )
    generation_raw = json.loads(
        bucket.get_object(store._key("demo", "generation-1")).read()
    )
    skill_raw = json.loads(
        bucket.get_object(
            SkillDatasetStore(bucket).dataset_key("demo", "case-1")
        ).read()
    )

    assert generation["schema_version"] == DATASET_SCHEMA_V2
    assert generation_raw["schema_version"] == DATASET_SCHEMA_V2
    assert skill_raw["schema_version"] == DATASET_SCHEMA_V2
    assert skill_raw["cases"][0]["metadata"]["read_only"] is True


def test_replay_policy_consumes_v2_case_directly():
    case = normalize_document({
        "dataset_id": "case-1",
        "query": "执行任务",
        "requirements": ["生成结果"],
        "trajectory_requirements": ["核验输入"],
        "progressive_disclosure": {"batch_size": 2},
    })["cases"][0]

    assert normalize_case_checklist(case) == case["checks"]
    assert progressive_config(case)["batch_size"] == 2


def test_material_integrity_reads_materials_from_v2_case():
    document = normalize_document({
        "dataset_id": "case-1",
        "query": "读取 input.csv 并生成报告",
        "requirements": ["生成报告"],
        "materials": [{"path": "input.csv", "size": 3, "sha256": "abc"}],
    })

    integrity = dataset_material_integrity(document)

    assert integrity["complete"] is True
    assert integrity["available_paths"] == ["input.csv"]
