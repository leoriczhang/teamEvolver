"""Keep real service test instances away from the operator's mining tasks."""

from __future__ import annotations

import pytest


@pytest.fixture(autouse=True)
def isolated_proxy_mining_repository(tmp_path, monkeypatch):
    """Give default ProxyServer repositories a per-test control-plane workspace.

    ProxyServer's lifespan starts an OV task monitor even in tests covering
    unrelated routes. Its production factory uses the checkout's mining_jobs;
    allowing that default here could mutate a running local service's tasks.
    Explicitly supplied repositories and subclass overrides remain authoritative.
    """
    from team_miner.ov_compile_mining import OvSkillMiningRepository
    from teamEvolver.proxy import ProxyServer

    def isolated_repository(owner):
        repository = getattr(owner, "_ov_skill_mining_repo", None)
        if repository is None:
            repository = OvSkillMiningRepository(
                tmp_path / "proxy-mining" / "state",
                tmp_path / "proxy-mining" / "project",
            )
            owner._ov_skill_mining_repo = repository
        return repository

    monkeypatch.setattr(ProxyServer, "_ov_skill_mining_repository", isolated_repository)
