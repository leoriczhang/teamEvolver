from __future__ import annotations

import asyncio
import threading
import time
from types import SimpleNamespace

import pytest

from teamEvolver.launcher import Launcher
from teamEvolver.proxy.server import ProxyServer


def test_wait_until_ready_returns_when_server_thread_stops() -> None:
    server = ProxyServer.__new__(ProxyServer)
    server._ready_event = threading.Event()
    server._server_stopped_event = threading.Event()
    server._server_stopped_event.set()

    started = time.monotonic()
    assert server.wait_until_ready(timeout_s=30.0) is False
    assert time.monotonic() - started < 0.5


def test_launcher_fails_when_proxy_does_not_become_ready(monkeypatch) -> None:
    servers: list[object] = []

    class FailedProxyServer:
        def __init__(self, **_kwargs):
            self.stopped = False
            servers.append(self)

        def start(self) -> None:
            return None

        def wait_until_ready(self, timeout_s: float) -> bool:
            assert timeout_s == 30.0
            return False

        def stop(self) -> None:
            self.stopped = True

    monkeypatch.setattr("teamEvolver.proxy.ProxyServer", FailedProxyServer)
    config = SimpleNamespace(
        proxy_host="127.0.0.1",
        proxy_port=52010,
        use_skills=False,
        sharing_enabled=False,
        validation_enabled=False,
        validation_agentshub_url="",
        validation_agentshub_api_key="",
        validation_required_results=3,
        validation_required_approvals=2,
    )

    launcher = Launcher(SimpleNamespace())
    with pytest.raises(RuntimeError, match="failed to become ready"):
        asyncio.run(launcher._run(config))

    assert servers and servers[0].stopped is True
