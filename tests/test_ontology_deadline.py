"""Adaptive compile deadline: packing estimator, formula, config compatibility, prediction helpers."""

import time

import pytest

from team_ontology.compile_flow import (
    BATCH_CHAR_LIMIT,
    compile_deadline_seconds,
    creation_prediction,
    estimate_batches,
    predicted_block,
)
from team_ontology.config import CompileDeadlineConfig, OntologyConfig


class Host:
    ontology = {}


def test_from_host_tolerates_legacy_timeout_key():
    class LegacyHost:
        ontology = {"compile_timeout_seconds": 1800, "compile_deadline": {"wave_seconds": 5000}}

    config = OntologyConfig.from_host(LegacyHost())
    assert config.compile_deadline.wave_seconds == 5000
    assert config.compile_deadline.base_seconds == 1800


def test_from_host_constructs_nested_group_from_dict():
    class YamlHost:
        ontology = {
            "compile_deadline": {
                "base_seconds": 900,
                "wave_seconds": 4500,
                "merge_wave_seconds": 1800,
                "safety_factor": 2.0,
            }
        }

    config = OntologyConfig.from_host(YamlHost())
    assert config.compile_deadline == CompileDeadlineConfig(900, 4500, 1800, 2.0)


def test_from_host_defaults_when_absent():
    config = OntologyConfig.from_host(Host())
    assert config.compile_deadline == CompileDeadlineConfig()


@pytest.mark.parametrize(
    "characters,expected",
    [
        ([], 1),
        ([0], 1),
        ([80_000], 1),
        ([40_000, 40_000], 1),
        ([40_000, 50_000], 2),
        ([80_000, 1], 2),
        ([160_000], 2),
        ([80_000] * 6, 6),
        ([80_000] * 7, 7),
        ([10_000] * 8, 1),
        ([30_000] * 3, 2),
    ],
)
def test_estimate_batches(characters, expected):
    assert estimate_batches(characters) == expected


def test_batch_char_limit_matches_ov():
    assert BATCH_CHAR_LIMIT == 80_000


@pytest.mark.parametrize(
    "batches,expected",
    [(1, 12_150), (6, 12_150), (7, 18_900), (36, 54_000), (100, 144_450), (250, 353_700)],
)
def test_compile_deadline_seconds(batches, expected):
    assert compile_deadline_seconds(batches, CompileDeadlineConfig()) == expected


def test_compile_deadline_seconds_honors_configured_constants():
    config = CompileDeadlineConfig(base_seconds=900, wave_seconds=4500, merge_wave_seconds=1800, safety_factor=2.0)
    assert compile_deadline_seconds(36, config) == 70_200  # 2.0 * (900 + 6*4500 + 4*1800)


def test_predicted_block_formats_hours_and_minutes():
    hours = predicted_block(36, 2_900_000, 54_000, False)
    assert hours["note"] == "预计编译 ~15 小时"
    assert hours == {
        "batches": 36,
        "total_characters": 2_900_000,
        "duration_seconds": 54_000,
        "note": "预计编译 ~15 小时",
        "estimated": False,
    }
    minutes = predicted_block(1, 100, 600, True)
    assert minutes["note"] == "预计编译 ~10 分钟"
    assert minutes["estimated"] is True


def test_creation_prediction_exact_for_roots_collection():
    collection = {
        "request": {"references": []},
        "result": {"files": [{"characters": 126_000}, {"characters": 20}], "bytes": 300_000},
    }
    assert creation_prediction(collection, CompileDeadlineConfig()) == {
        "batches": 2,
        "total_characters": 126_020,
        "duration_seconds": 12_150,
        "note": "预计编译 ~3 小时",
        "estimated": False,
    }


def test_creation_prediction_exact_with_reference_characters():
    collection = {
        "request": {"references": [{"source_id": "s", "revision": "r", "digest": "d"}]},
        "result": {"files": [{"characters": 40_000}], "reference_characters": 50_000, "bytes": 200_000},
    }
    predicted = creation_prediction(collection, CompileDeadlineConfig())
    assert predicted["estimated"] is False
    assert predicted["batches"] == 2  # 90,000 chars -> two batches


def test_creation_prediction_legacy_references_estimated():
    collection = {
        "request": {"references": [{"source_id": "s", "revision": "r", "digest": "d"}]},
        "result": {"files": [{"characters": 100}], "bytes": 25_100},
    }
    predicted = creation_prediction(collection, CompileDeadlineConfig())
    assert predicted["estimated"] is True
    # Legacy fallback: 25,100 / 2.5 - 100 = 9,940 reference chars -> 10,040 total.
    assert predicted["total_characters"] == 10_040


def test_public_job_reports_elapsed_for_running_jobs():
    from team_ontology.api import public_job

    job = {"id": "j", "state": "running", "result": {"submitted_at": time.time() - 90, "stage": "poll"}}
    out = public_job(job)
    assert 80 <= out["result"]["elapsed_seconds"] <= 100
    done = {**job, "state": "failed"}
    assert "elapsed_seconds" not in public_job(done)["result"]
