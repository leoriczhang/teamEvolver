"""Customer HTTP, migration and DEAP transport acceptance."""

import hashlib
import json
import os
import uuid
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient

from teamEvolver.config import TeamEvolverConfig
from teamEvolver.integrations.deap_replay import DeapReplayAdapter
from teamEvolver.proxy.server import ProxyServer
from teamEvolver.session_store import SessionStore
from teamEvolver.skills.bundle import encode_bundle_payload
from teamEvolver.storage.pg_pool import close_pg_runtimes
from teamEvolver.storage.pg_store import PgObjectStore


def test_deap_two_branches_never_touch_default_workspace():
    calls = []

    def handle(request):
        payload = json.loads(request.content)
        calls.append((request.url.path, payload, request.headers))
        if "message" in request.url.path:
            return httpx.Response(200, json={"answer": "ok"})
        return httpx.Response(200, json={"success": True})

    with httpx.Client(transport=httpx.MockTransport(handle)) as client:
        branches = [DeapReplayAdapter("http://deap.test", employee_no="test-user", client=client) for _ in range(2)]
        try:
            for branch in branches:
                for turn in (1, 2):
                    result = branch.call_turn(
                        {
                            "request_id": "request",
                            "turn_num": turn,
                            "branch": "candidate",
                            "prompt": "task",
                            "skill": {
                                "name": "demo",
                                "bundle": encode_bundle_payload(
                                    {
                                        "SKILL.md": "# demo",
                                        "scripts/run.py": "print('test')",
                                    }
                                ),
                            }
                            if turn == 1
                            else None,
                        }
                    )
                    assert result["status"] == "succeeded"
                    assert result["metrics_incomplete"]
        finally:
            for branch in branches:
                branch.close()
    assert branches[0].workspace != branches[1].workspace
    assert all(payload.get("workspace") in {b.workspace for b in branches} for _, payload, _ in calls)
    messages = [call for call in calls if "message" in call[0]]
    assert messages[0][1]["conversationId"] == messages[1][1]["conversationId"]
    assert messages[0][1]["conversationId"] != messages[2][1]["conversationId"]
    assert all(call[2]["x-sf-employeeNo"] == "test-user" for call in messages)
    assert sum(path == "/skillopt/delete" and set(body) == {"workspace"} for path, body, _ in calls) == 2


@pytest.mark.skipif(not os.environ.get("TE_PG_TEST_DSN"), reason="TE_PG_TEST_DSN not set")
def test_customer_http_auth_ingest_and_restart(monkeypatch, tmp_path):
    monkeypatch.setenv("TEAMEVOLVER_ROOT_API_KEY", "test-root-" + "x" * 40)
    monkeypatch.setenv("TEAMEVOLVER_SKILLMINER_ENABLED", "0")
    monkeypatch.setenv("TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED", "0")
    config = TeamEvolverConfig(
        storage_pg_enabled=True,
        storage_pg_dsn=os.environ["TE_PG_TEST_DSN"],
        storage_pg_schema="http_" + uuid.uuid4().hex[:12],
        sharing_session_backend="postgres",
        sharing_skill_backend="postgres",
        sharing_enabled=False,
        sharing_skill_mirror_enabled=False,
        llm_api_key="",
        users_registry_path=str(tmp_path / "users.json"),
    )
    root = {"Authorization": "Bearer " + os.environ["TEAMEVOLVER_ROOT_API_KEY"]}
    server = ProxyServer(config)
    try:
        with TestClient(server.app) as client:
            assert client.get("/readyz").status_code == 200
            assert client.get("/conversations").status_code == 401
            created = client.post("/api/tenants", headers=root, json={"display_name": "A", "account_id": "account-a"})
            assert created.status_code == 200, created.text
            tenant_token = created.json()["agent_token"]
            auth = {"Authorization": "Bearer " + tenant_token}
            assert client.get("/conversations", headers={"Authorization": "Bearer tevt_invalid"}).status_code == 401
            assert client.get("/conversations", headers={**auth, "X-Tenant-Id": "account-b"}).status_code == 403
            assert client.get("/api/tenants", headers=auth).status_code == 403
            response = client.post(
                "/ingest_session",
                headers=auth,
                json={
                    "session_id": "acceptance-session",
                    "user_alias": "test",
                    "turns": [{"prompt_text": "task", "response_text": "response"}],
                    "defer_evolution_trigger": True,
                },
            )
            assert response.status_code == 200, response.text
            assert response.json()["status"] in {"queued", "skipped"}
            bucket = PgObjectStore(dsn=config.storage_pg_dsn, schema=config.storage_pg_schema, tenant_id="account-a")
            assert SessionStore(bucket).load_archived("acceptance-session") is not None
        with TestClient(ProxyServer(config).app) as restarted:
            assert restarted.get("/conversations", headers=auth).status_code == 200
    finally:
        close_pg_runtimes()


@pytest.mark.skipif(not os.environ.get("TE_PG_TEST_DSN"), reason="TE_PG_TEST_DSN not set")
def test_consumption_keeps_new_revision(tmp_path):
    bucket = PgObjectStore(
        dsn=os.environ["TE_PG_TEST_DSN"],
        schema="revision_" + uuid.uuid4().hex[:12],
        tenant_id="account-a",
    )
    store = SessionStore(bucket)
    old = {"session_id": "same", "turns": [{"prompt_text": "old"}]}
    store.save_queued(old)
    old_hash = hashlib.sha256(bucket.get_object(store.queue_key("same")).read()).hexdigest()
    store.save_queued({**old, "turns": [{"prompt_text": "new"}]})
    assert not bucket.consume_session(store.queue_key("same"), old_hash, store.archive_key("same"), old)
    assert store.load_archived("same")["turns"][0]["prompt_text"] == "new"
    new_hash = hashlib.sha256(bucket.get_object(store.queue_key("same")).read()).hexdigest()
    assert bucket.consume_session(
        store.queue_key("same"),
        new_hash,
        store.archive_key("same"),
        store.load_archived("same"),
    )
    with pytest.raises(FileNotFoundError):
        bucket.get_object(store.queue_key("same"))
    close_pg_runtimes()


@pytest.mark.skipif(not os.environ.get("TE_PG_TEST_DSN"), reason="TE_PG_TEST_DSN not set")
def test_real_process_restart_preserves_login_and_accounts(tmp_path):
    import socket
    import subprocess
    import sys
    import time

    import yaml

    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
    config = tmp_path / "config.yaml"
    config.write_text(
        yaml.safe_dump(
            {
                "storage_pg": {
                    "enabled": True,
                    "dsn": os.environ["TE_PG_TEST_DSN"],
                    "schema": "process_" + uuid.uuid4().hex[:12],
                },
                "sharing": {"enabled": False, "skill_mirror_enabled": False},
                "skills": {"enabled": False},
            }
        )
    )
    env = {
        **os.environ,
        "HOME": str(tmp_path),
        "TEAMEVOLVER_CONFIG_FILE": str(config),
        "TEAMEVOLVER_ROOT_API_KEY": "test-" + uuid.uuid4().hex,
        "TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED": "0",
        "TEAMEVOLVER_SKILLMINER_ENABLED": "0",
    }
    env.pop("TEAMEVOLVER_PG_DSN", None)
    auth = {"Authorization": "Bearer " + env["TEAMEVOLVER_ROOT_API_KEY"]}
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=5) as client:
        for iteration in range(2):
            with (tmp_path / f"server-{iteration}.log").open("w") as log:
                process = subprocess.Popen(
                    [
                        sys.executable,
                        "-m",
                        "uvicorn",
                        "teamEvolver.customer:create_app",
                        "--factory",
                        "--host",
                        "127.0.0.1",
                        "--port",
                        str(port),
                    ],
                    env=env,
                    stdout=log,
                    stderr=log,
                )
                (tmp_path / "te_customer_smoke.pid").write_text(str(process.pid))
                try:
                    for _ in range(100):
                        assert process.poll() is None, (tmp_path / f"server-{iteration}.log").read_text()
                        try:
                            if client.get("/readyz").status_code == 200:
                                break
                        except httpx.TransportError:
                            pass
                        time.sleep(0.1)
                    else:
                        raise AssertionError("server never became ready")
                    if iteration == 0:
                        response = client.post(
                            "/api/auth/bootstrap",
                            headers=auth,
                            json={
                                "username": "test-admin",
                                "password": "test-password-with-enough-length",
                            },
                        )
                        assert response.status_code == 200, response.text
                        assert (
                            client.post("/api/tenants", headers=auth, json={"account_id": "persisted"}).status_code
                            == 200
                        )
                    assert client.get("/api/auth/status").json()["authenticated"]
                    assert "persisted" in {
                        t["tenant_id"] for t in client.get("/api/tenants", headers=auth).json()["tenants"]
                    }
                finally:
                    process.terminate()
                    try:
                        process.wait(timeout=10)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait(timeout=5)


@pytest.mark.skipif(not os.environ.get("TE_PG_TEST_DSN"), reason="TE_PG_TEST_DSN not set")
def test_import_legacy_project_is_idempotent(monkeypatch, tmp_path):
    import importlib.util

    import yaml

    from teamEvolver.config_store import ConfigStore
    from teamEvolver.integrations.agent_registry import list_agents
    from teamEvolver.skills.hub import SkillHub
    from teamEvolver.tenants.registry import TenantRegistry, effective_config, reset_current_tenant, set_current_tenant

    module_path = Path(__file__).resolve().parents[1] / "scripts" / "import_skillopt.py"
    spec = importlib.util.spec_from_file_location("import_skillopt", module_path)
    importer = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(importer)
    legacy = tmp_path / "legacy"
    (legacy / "converters").mkdir(parents=True)
    (legacy / "converters" / "project.py").write_text(
        "from core.langfuse_client import langfuse_to_template\ndef convert(raw): return langfuse_to_template(raw)\n")
    (legacy / "config").mkdir(parents=True)
    (legacy / "config" / "project.yaml").write_text(
        yaml.safe_dump(
            {
                "llm": {"url": "http://model.test/v1/chat/completions", "token": "Bearer test", "model": "test"},
                "langfuse": {"host": "http://lf.test", "public_key": "pk", "secret_key": "sk"},
                "experiment": {"agent_host": "http://deap.test", "emp_id": "test-user"},
            }
        )
    )
    skill = legacy / "claw_workspaces/project/workspace/skills/demo"
    skill.mkdir(parents=True)
    (skill / "SKILL.md").write_text("---\nname: demo\ndescription: test\n---\n# Demo\n")
    config_file = tmp_path / "config.yaml"
    config_file.write_text(
        yaml.safe_dump(
            {
                "storage_pg": {
                    "enabled": True,
                    "dsn": os.environ["TE_PG_TEST_DSN"],
                    "schema": "migration_" + uuid.uuid4().hex[:12],
                },
                "sharing": {"enabled": True, "skill_mirror_enabled": False},
            }
        )
    )
    monkeypatch.setenv("TEAMEVOLVER_CONFIG_FILE", str(config_file))
    preview = importer.import_projects(legacy, project="project", account_id="account-a")
    assert preview[0]["skills"] == 1
    assert "Bearer test" not in json.dumps(preview)
    for _ in range(2):
        importer.import_projects(legacy, apply=True, project="project", account_id="account-a")
    config = ConfigStore().to_config()
    registry = TenantRegistry(config)
    ctx = registry.get("account-a")
    token = set_current_tenant(ctx)
    try:
        scoped = effective_config(registry, ctx, config)
        assert SkillHub.team_from_config(scoped, tenant_id="account-a").list_remote()[0]["version"] == 1
        assert list_agents(scoped)[0]["runtime_type"] == "deap"
    finally:
        reset_current_tenant(token)
        close_pg_runtimes()
