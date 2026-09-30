from __future__ import annotations

from teamEvolver.cli.daemon import _daemon_ready_timeout_seconds


def test_daemon_ready_timeout_allows_openviking_fallback(monkeypatch):
    monkeypatch.delenv("TEAMEVOLVER_DAEMON_READY_TIMEOUT_S", raising=False)
    assert _daemon_ready_timeout_seconds() == 60.0


def test_daemon_ready_timeout_can_still_be_overridden(monkeypatch):
    monkeypatch.setenv("TEAMEVOLVER_DAEMON_READY_TIMEOUT_S", "12.5")
    assert _daemon_ready_timeout_seconds() == 12.5
