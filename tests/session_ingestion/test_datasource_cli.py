from types import SimpleNamespace

from click.testing import CliRunner

from teamEvolver.cli import datasource_cmd as cli


def test_watch_follows_same_pull_until_all_members_settle(monkeypatch):
    monkeypatch.setattr(cli, "load_bootstrap", lambda: SimpleNamespace(proxy_port=52010))
    monkeypatch.setattr(cli.time, "sleep", lambda _: None)
    results = iter([("failed", False), ("failed", True)])
    urls = []

    def get(_ctx, method, suffix):
        urls.append((method, suffix))
        status, complete = next(results)
        return dict(status=status, complete=complete, tracking_available=True, counts={}, summary={}, job={})

    monkeypatch.setattr(cli, "_request_json", get)
    result = CliRunner().invoke(cli.datasource, ["watch", "pull:42"])
    assert result.exit_code == 1 and "failed" in result.output
    assert urls == [("GET", "/jobs/pull%3A42/progress?limit=1")] * 2


def test_named_session_preview_passes_agent_filter(monkeypatch):
    monkeypatch.setattr(cli, "load_bootstrap", lambda: SimpleNamespace(proxy_port=52010))
    requests = []
    monkeypatch.setattr(cli, "request", lambda ctx, *args: requests.append(args))
    result = CliRunner().invoke(cli.datasource, ["list", "--connection", "sales", "--agent-id", "judge"])
    assert result.exit_code == 0
    assert requests == [
        ("POST", "/connections/sales/sessions", {"filters": {"agent_id": "judge"}, "max_sessions": 100})
    ]
