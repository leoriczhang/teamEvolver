import asyncio
import logging
import os
import time
from datetime import datetime
from pathlib import Path

import httpx
import pytest
from fastapi import FastAPI, HTTPException, Request

from teamEvolver.logging_http import RequestLogMiddleware, install_logging_status
from teamEvolver.logging_runtime import (
    LogRuntime,
    RollingFile,
    SafeFormatter,
    event,
    log_context,
    redact,
    register_secrets,
    resolve_settings,
)


def settings(tmp_path, **kwargs):
    return resolve_settings({"directory": str(tmp_path), "console_enabled": False, **kwargs}, environ={})


def record(message, level=logging.INFO, exc_info=None):
    return logging.LogRecord("test.logging", level, __file__, 1, message, (), exc_info)


def test_configuration_precedence_and_instance_isolation(tmp_path, monkeypatch):
    cfg = resolve_settings(
        {"directory": "/yaml", "level": "WARNING"},
        environ={"TEAMEVOLVER_LOG_DIR": "/env", "TEAMEVOLVER_LOG_LEVEL": "DEBUG"},
    )
    assert cfg["path"] == "/env/teamEvolver.log"
    assert cfg["level"] == "DEBUG"
    assert cfg["sources"]["level"] == "process_environment"
    cfg = resolve_settings(
        {"file_enabled": False}, log_file=str(tmp_path / "override.log"), environ={"TEAMEVOLVER_LOG_DIR": "/env"}
    )
    assert cfg["file_enabled"] is True and cfg["path"].endswith("override.log")
    env = {"TEAMEVOLVER_MULTI_REPLICA": "1", "TEAMEVOLVER_INSTANCE_ID": "pod/one"}
    first = resolve_settings({}, environ=env)["path"]
    monkeypatch.setattr(os, "getpid", lambda: 98765)
    second = resolve_settings({}, environ=env)["path"]
    assert first == second and "pod_one" in second  # stable identity survives restart
    env.pop("TEAMEVOLVER_INSTANCE_ID")
    second = resolve_settings({}, environ=env)["path"]
    assert "98765" in second


@pytest.mark.parametrize("key,value", [("max_file_mb", 0), ("level", "INVALID"), ("file_enabled", "maybe")])
def test_invalid_settings(key, value):
    with pytest.raises(ValueError, match="logging option"):
        resolve_settings({key: value}, environ={})


def test_day_size_restart_and_cleanup(tmp_path):
    now = [datetime(2026, 9, 20, 12).timestamp()]
    cfg = settings(tmp_path)
    cfg["max_file_mb"] = 0.00004
    sink = RollingFile(cfg, clock=lambda: now[0])
    sink.open()
    sink.write("first-message-abcdefghij")
    sink.write("second-message-abcdefghij")
    assert len(sink.archives()) == 1
    now[0] += 86400
    sink.write("third-message")
    assert len(sink.archives()) == 2
    sink.close()
    # Simulate mtime in the test clock's timezone, as on a real restart.
    os.utime(sink.path, (now[0], now[0]))
    restart = RollingFile(cfg, clock=lambda: now[0])
    restart.open()
    restart.write("fourth-message-abcdefghij")
    assert len(restart.archives()) == 2  # exact size still fits
    restart.write("fifth-message-abcdefghij")
    assert len(restart.archives()) == 3
    other = tmp_path / "candidate.json"
    other.write_text("do not delete")
    alien = tmp_path / "teamEvolver.log.2020-01-01.bad"
    alien.write_text("keep")
    now[0] += 16 * 86400
    restart.cleanup()
    assert not restart.archives()
    assert other.exists() and alien.exists()
    restart.close()


def test_capacity_cleanup_only_archives(tmp_path):
    cfg = settings(tmp_path)
    cfg["max_total_mb"] = 0.00001
    sink = RollingFile(cfg)
    sink.open()
    older = tmp_path / f"teamEvolver.log.{datetime.now():%Y-%m-%d}.000001"
    newer = tmp_path / f"teamEvolver.log.{datetime.now():%Y-%m-%d}.000002"
    older.write_text("0123456789")
    newer.write_text("0123456789")
    sink.cleanup()
    assert not older.exists() and newer.exists() and sink.path.exists()
    sink.close()


def test_redaction_real_trace_nested_and_body():
    register_secrets({"api_key": "configured-unlabelled-secret"})
    text = """Authorization: Bearer abc-123\n{'password': 'nested-secret'}\nhttps://user:pass@example.com/a?key=query-secret\npostgresql://dbuser:dbpass@db/x\nconfigured-unlabelled-secret"""
    cleaned = redact(text)
    assert all(
        secret not in cleaned
        for secret in ["abc-123", "nested-secret", "query-secret", "dbpass", "configured-unlabelled-secret"]
    )
    try:
        raise ValueError("business document body and password=hidden")
    except ValueError:
        import sys

        rendered = SafeFormatter().format(record("build.failed", exc_info=sys.exc_info()))
    assert "Traceback" in rendered and "ValueError" in rendered and "line " in rendered
    assert "business document" not in rendered and "hidden" not in rendered
    assert "\x1b" not in SafeFormatter().format(record("\x1b[31mred\x1b[0m"))


def test_queue_saturation_error_fallback_and_drain(tmp_path, capsys):
    runtime = LogRuntime(settings(tmp_path), capacity=1)
    runtime.handler.emit(record("one"))
    runtime.handler.emit(record("two"))
    runtime.handler.emit(record("emergency", logging.ERROR))
    assert runtime.dropped == 2 and "emergency" in capsys.readouterr().err
    runtime.start()
    runtime.close()
    assert "one" in Path(runtime.cfg["path"]).read_text()
    assert not runtime.thread.is_alive()


def test_unwritable_directory_recovers_and_falls_back(tmp_path, capsys):
    obstruction = tmp_path / "blocked"
    obstruction.write_text("file")
    now = [time.time()]
    runtime = LogRuntime(settings(obstruction), clock=lambda: now[0])
    runtime.start()
    assert runtime.file_state == "degraded"
    runtime.handler.emit(record("survives"))
    runtime.queue.join()
    assert runtime.file_missed == 1 and "survives" in capsys.readouterr().err
    obstruction.unlink()
    now[0] += 61
    runtime.handler.emit(record("trigger maintenance"))
    deadline = time.monotonic() + 2
    while runtime.file_state != "active" and time.monotonic() < deadline:
        time.sleep(0.02)
    runtime.handler.emit(record("recovered"))
    runtime.close()
    assert "recovered" in Path(runtime.cfg["path"]).read_text()
    assert "file_recovered" in capsys.readouterr().err


def test_runtime_write_failure(tmp_path, monkeypatch, capsys):
    runtime = LogRuntime(settings(tmp_path))
    runtime.start()

    def failed(_):
        raise OSError("disk full")

    monkeypatch.setattr(runtime.file, "write", failed)
    runtime.handler.emit(record("still visible"))
    runtime.queue.join()
    runtime.close()
    assert runtime.file_state == "degraded" and runtime.file_missed == 1
    assert "still visible" in capsys.readouterr().err


@pytest.mark.asyncio
async def test_request_ids_parallel_context_and_admin_status(caplog):
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    def guard(user):
        if not user or user.get("role") != "admin":
            raise HTTPException(403, "ADMIN_REQUIRED")

    install_logging_status(app, guard)

    @app.get("/work/{tenant}")
    async def work(tenant: str, request: Request):
        request.state.tenant_id = tenant
        with log_context(tenant=tenant):
            await asyncio.to_thread(event, logging.getLogger("test.context"), "thread.completed", tenant=tenant)
        return {}

    caplog.set_level(logging.DEBUG)
    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        responses = await asyncio.gather(*[client.get(f"/work/{t}", headers={"X-Request-ID": t}) for t in ["a", "b"]])
        assert [r.headers["x-request-id"] for r in responses] == ["a", "b"]
        missing = await client.get("/missing?secret=never_log", headers={"X-Request-ID": "bad value"})
        assert missing.status_code == 404 and missing.headers["x-request-id"] != "bad value"
        assert (await client.get("/api/logging/status")).status_code == 403
    assert any(getattr(r, "fields", {}).get("code") == "TE_ROUTE_NOT_REGISTERED" for r in caplog.records)
    assert "never_log" not in "\n".join(SafeFormatter().format(r) for r in caplog.records)
    threads = [r for r in caplog.records if r.name == "test.context"]
    assert {r.context["tenant"] for r in threads} == {"a", "b"}
    assert all(r.context["tenant"] == r.context["request_id"] for r in threads)


def test_tenant_cannot_override_logging():
    from teamEvolver.config import TeamEvolverConfig
    from teamEvolver.tenants.registry import apply_tenant_config_overrides

    cfg = TeamEvolverConfig(logging={"directory": "/service"})
    updated = apply_tenant_config_overrides(cfg, {"logging": {"directory": "/tenant"}})
    assert updated.logging == cfg.logging


def test_same_instance_exclusive_ownership_and_restart(tmp_path):
    first, second = RollingFile(settings(tmp_path)), RollingFile(settings(tmp_path))
    first.open()
    first.write("before restart")
    with pytest.raises(OSError):
        second.open()
    first.close()
    second.open()
    second.write("after restart")
    second.close()
    assert "before restart\nafter restart" in second.path.read_text()


def test_uvicorn_preformatted_trace_hides_business_body():
    raw = (
        'Traceback (most recent call last):\n  File "/app/x.py", line 12, in build\n'
        "    some_business_literal\nValueError: source body SECRET BUSINESS\n"
    )
    text = SafeFormatter().format(record(raw))
    assert "line 12" in text and "ValueError" in text
    assert "SECRET BUSINESS" not in text and "some_business_literal" not in text


@pytest.mark.asyncio
async def test_internal_error_has_request_id(caplog):
    app = FastAPI()
    app.add_middleware(RequestLogMiddleware)

    @app.get("/broken")
    async def broken():
        raise RuntimeError("a private business paragraph")

    async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url="http://test") as client:
        reply = await client.get("/broken", headers={"x-request-id": "error-request"})
    assert reply.status_code == 500 and reply.headers["x-request-id"] == "error-request"
    rendered = "\n".join(SafeFormatter().format(r) for r in caplog.records)
    assert "a private business paragraph" not in rendered


def test_child_output_bounded_and_never_leaks_payload(caplog):
    import subprocess
    import sys

    from teamEvolver.logging_subprocess import capture_subprocess

    caplog.set_level(logging.DEBUG)
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "print('x'*20000);print('Authorization: Bearer SECRET');"
            "print('ERROR private business');print('Application startup complete')",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
    )
    thread = capture_subprocess(child, {})
    child.wait(timeout=5)
    thread.join(timeout=2)
    assert not thread.is_alive()
    records = [r for r in caplog.records if r.name == "team_miner.subprocess"]
    assert any(r.fields["truncated"] for r in records)
    assert any(r.fields["kind"] == "ready" for r in records)
    assert all(
        "private business" not in SafeFormatter().format(r) and "SECRET" not in SafeFormatter().format(r)
        for r in records
    )


def test_config_env_file_sources_and_process_precedence(tmp_path, monkeypatch):
    from teamEvolver.config_store import ConfigStore

    config = tmp_path / "config.yaml"
    config.write_text("logging:\n  level: WARNING\n")
    env_file = tmp_path / ".env"
    env_file.write_text("TEAMEVOLVER_LOG_LEVEL=DEBUG\nTEAMEVOLVER_LOG_RETENTION_DAYS=9\n")
    monkeypatch.setenv("TEAMEVOLVER_ENV_FILE", str(env_file))
    monkeypatch.setenv("TEAMEVOLVER_LOG_LEVEL", "ERROR")
    monkeypatch.delenv("TEAMEVOLVER_LOG_RETENTION_DAYS", raising=False)
    cs = ConfigStore(config)
    try:
        cfg = resolve_settings(cs.get("logging"), sources=cs.env_sources)
        assert cfg["level"] == "ERROR" and cfg["sources"]["level"] == "process_environment"
        assert cfg["retention_days"] == 9 and cfg["sources"]["retention_days"].startswith("env_file:")
        assert cs.env_file == env_file
    finally:
        os.environ.pop("TEAMEVOLVER_LOG_RETENTION_DAYS", None)


def test_config_load_and_initialization_failures_written(tmp_path, monkeypatch):
    from click.testing import CliRunner

    from teamEvolver.cli.daemon import start
    from teamEvolver.launcher import Launcher
    from teamEvolver.logging_runtime import shutdown

    config = tmp_path / "config.yaml"
    config.write_text("broken: [not yaml")
    logfile = tmp_path / "error.log"
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_FILE", str(config))
    monkeypatch.setenv("TEAMEVOLVER_ENV_FILE", str(tmp_path / "nonexistent.env"))
    monkeypatch.setenv("TEAMEVOLVER_LOG_CONSOLE_ENABLED", "0")
    monkeypatch.setenv("TEAMEVOLVER_MULTI_REPLICA", "0")
    root = logging.getLogger()
    previous, level = list(root.handlers), root.level
    try:
        result = CliRunner().invoke(start, ["--log-file", str(logfile)])
        assert result.exit_code != 0
        assert "startup.configuration_failed" in logfile.read_text()
        config.write_text("ontology:\n  enabled: false\n")

        async def failed(self):
            raise RuntimeError("private initialization payload")

        monkeypatch.setattr(Launcher, "start", failed)
        result = CliRunner().invoke(start, ["--log-file", str(logfile)])
        assert result.exit_code != 0
        text = logfile.read_text()
        assert "startup.service_failed" in text and "RuntimeError" in text
        assert "private initialization payload" not in text
    finally:
        shutdown()
        root.handlers[:] = previous
        root.setLevel(level)


def test_shutdown_wait_is_bounded(tmp_path, monkeypatch):
    import threading

    entered, release = threading.Event(), threading.Event()
    runtime = LogRuntime(settings(tmp_path))
    original = runtime.file.write

    def stalled(line):
        entered.set()
        release.wait(timeout=3)
        original(line)

    monkeypatch.setattr(runtime.file, "write", stalled)
    runtime.start()
    runtime.handler.emit(record("stalled write"))
    assert entered.wait(timeout=1)
    runtime.handler.emit(record("pending"))
    started = time.monotonic()
    runtime.close(timeout=0.05)
    assert time.monotonic() - started < 0.6
    release.set()
    runtime.thread.join(timeout=1)
    assert runtime.dropped == 1
