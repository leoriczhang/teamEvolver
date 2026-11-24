"""Runtime changes are usable evidence with explicit historical provenance."""
import copy
import json
from datetime import datetime, timedelta, timezone

from team_replay.fingerprints import execution_fingerprint, execution_provenance, ineligibility_reason
from team_skills.evolution.repository import evidence_projection
from team_skills.evolution.session_processing import after_reset


def runtime():
    return {
        "type": "managed-agents", "connection_id": "ma",
        "agent_graph": {"agents": {"agent": {"id": "agent", "version": 3, "model": {
            "id": "old-model", "base_url": "https://user:password@models.example/v1?key=secret",
            "api_key": "secret",
        }, "skills": [{"skill_id": "skill", "type": "custom", "version": 2}]}}},
        "environment": {"id": "env-old", "name": "old", "config": {"setup_script": "secret setup"}},
    }


def test_runtime_drift_is_eligible_without_latest_execution_snapshot():
    source = runtime()
    current = copy.deepcopy(source)
    current["agent_graph"]["agents"]["agent"]["model"]["id"] = "new-model"
    current["environment"]["config"]["setup_script"] = "new"
    assert ineligibility_reason({"runtime": source}, {"ma": execution_fingerprint(current)}) == ""
    assert ineligibility_reason({"runtime": source}, {"ma": {}}) == ""
    assert ineligibility_reason({"runtime": source}, {}) == "Source connection is unavailable or disabled"
    source["snapshot_missing"] = True
    assert ineligibility_reason({"runtime": source}, {"ma": {}}) == "Historical execution snapshot is incomplete"
    source.pop("snapshot_missing")
    source.pop("environment")
    assert ineligibility_reason({"runtime": source}, {"ma": {}}) == "Historical execution fingerprint is incomplete"


def test_labels_do_not_change_execution_identity_but_model_and_config_do():
    source = runtime()
    changed = copy.deepcopy(source)
    changed["agent_graph"]["agents"]["agent"]["skills"][0]["display_name"] = "Renamed"
    assert execution_fingerprint(source) == execution_fingerprint(changed)
    changed["agent_graph"]["agents"]["agent"]["model"]["id"] = "new-model"
    assert execution_fingerprint(source) != execution_fingerprint(changed)


def test_public_provenance_is_pinned_and_survives_evidence_projection_without_secrets():
    session = {"session_id": "source", "content_hash": "hash", "runtime": runtime()}
    source = execution_provenance(session)
    assert source["agents"][0]["model"] == {"id": "old-model", "base_url": "https://models.example/v1"}
    assert source["environment"] == {"id": "env-old", "name": "old"}
    assert source["agents"][0]["skills"] == [{"skill_id": "skill", "type": "custom", "version": 2}]
    assert "secret" not in json.dumps(source) and "password" not in json.dumps(source)
    projected = evidence_projection(session)
    assert execution_provenance(projected) == source
    source["agents"][0]["model"]["id"] = "mutated"
    assert projected["execution_provenance"]["agents"][0]["model"]["id"] == "old-model"


def test_historical_evidence_keeps_business_time_for_the_skill_baseline_guard():
    boundary = datetime(2026, 11, 19, tzinfo=timezone.utc)
    fresh = evidence_projection({
        "session_id": "fresh", "content_hash": "hash", "business_time": boundary + timedelta(days=1),
    })
    assert after_reset(fresh, boundary)
    # Already-recorded provenance remains readable without inventing fresh
    # business time from a later ingestion timestamp.
    historical = {"execution_provenance": {"business_time": boundary - timedelta(days=1)},
                  "ingested_at": boundary + timedelta(days=1)}
    assert not after_reset(historical, boundary)
    historical["execution_provenance"]["business_time"] = boundary + timedelta(hours=1)
    assert after_reset(historical, boundary)
