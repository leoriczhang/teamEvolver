from __future__ import annotations

from teamEvolver.cli.daemon import _daemon_child_environment


def test_daemon_loads_agent_protocol_env_without_overriding_process_env(
    tmp_path,
) -> None:
    runtime_dir = tmp_path / ".teamEvolver"
    runtime_dir.mkdir()
    runtime_dir.joinpath("agent-protocol.env").write_text(
        'EVOLVE_INGEST_API_KEY="file-secret"\n'
        'AGENT_PROTOCOL_LABEL="Agents Hub"\n',
        encoding="utf-8",
    )

    loaded = _daemon_child_environment(
        {"PATH": "/usr/bin"},
        home=tmp_path,
    )
    overridden = _daemon_child_environment(
        {
            "PATH": "/usr/bin",
            "EVOLVE_INGEST_API_KEY": "process-secret",
        },
        home=tmp_path,
    )

    assert loaded["EVOLVE_INGEST_API_KEY"] == "file-secret"
    assert loaded["AGENT_PROTOCOL_LABEL"] == "Agents Hub"
    assert overridden["EVOLVE_INGEST_API_KEY"] == "process-secret"
