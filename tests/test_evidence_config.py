from __future__ import annotations

from teamEvolver.config_store import ConfigStore


def test_evidence_defaults_are_exposed_to_embedded_evolver(tmp_path) -> None:
    config = ConfigStore(tmp_path / "missing.yaml").to_config()

    assert config.evolve_evidence_enabled is True
    assert config.evolve_evidence_max_entries == 400
    assert config.evolve_evidence_recent_limit == 20
    assert config.evolve_evidence_historical_limit == 20
    assert config.evolve_evidence_replay_cases_per_window == 1
    assert config.evolve_evidence_change_debt_threshold == 3
    assert config.evolve_candidate_coalesce_enabled is True


def test_evidence_config_values_are_normalized(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    store.save(
        {
            "evolve": {
                "evidence_enabled": True,
                "evidence_max_entries": 50,
                "evidence_recent_limit": 5,
                "evidence_historical_limit": 7,
                "evidence_replay_cases_per_window": 2,
                "evidence_change_debt_threshold": 4,
                "candidate_coalesce_enabled": False,
            }
        }
    )

    config = store.to_config()

    assert config.evolve_evidence_max_entries == 50
    assert config.evolve_evidence_recent_limit == 5
    assert config.evolve_evidence_historical_limit == 7
    assert config.evolve_evidence_replay_cases_per_window == 2
    assert config.evolve_evidence_change_debt_threshold == 4
    assert config.evolve_candidate_coalesce_enabled is False
