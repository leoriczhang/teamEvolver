from __future__ import annotations

from teamEvolver.config_store import ConfigStore
from teamEvolver.evolve.kernel.settings import EvolveServerConfig


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


def test_process_policy_config_reaches_embedded_evolver(tmp_path) -> None:
    store = ConfigStore(tmp_path / "config.yaml")
    data = store.load()
    data["evolve"].update(
        {
            "use_session_judge": False,
            "publish_mode": "direct",
            "validation_max_rejections": 4,
            "human_review_enabled": False,
            "human_review_timeout_seconds": 123,
            "interval_seconds": 77,
        }
    )
    store.save(data)

    config = store.to_config()
    embedded = EvolveServerConfig.from_teamEvolver_config(config)

    assert embedded.use_session_judge is False
    assert embedded.publish_mode == "direct"
    assert embedded.validation_max_rejections == 4
    assert embedded.human_review_enabled is False
    assert embedded.human_review_pending_timeout_seconds == 123
    assert embedded.interval_seconds == 77
