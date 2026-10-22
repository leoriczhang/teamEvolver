"""Knowledge workspace commands cross API, PG receipts, Worker and content transport."""

import base64

import pytest

from team_miner.knowledge_miner.config import DEFAULTS
from team_miner.knowledge_miner.state import MinerState
from teamEvolver.config_runtime import ConfigRepository
from tests.support.app import H, _login, _pod
from tests.support.knowledge_miner import KnowledgeOV, bind, run_job
from tests.support.postgres import _run

pytestmark = pytest.mark.postgres
SOURCE = DEFAULTS["source_uri"]
WIKI = DEFAULTS["target_uri"]


@pytest.mark.parametrize("kind,root", [("source", SOURCE), ("wiki", WIKI)])
def test_content_save_read_delete_is_durable_and_tenant_isolated(pg_env, tmp_path, monkeypatch, kind, root):
    ov = KnowledgeOV()
    target = root + "/验收说明.md"
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        bind(client, ov)
        accepted = client.post("/api/knowledge-mining/content", headers=H,
                               json={"kind": kind, "uri": target, "content": "# 原始内容\n"})
        assert accepted.status_code == 202, accepted.text
        rid = accepted.json()["job_id"]
        assert not ov.documents
        assert client.get(f"/api/knowledge-mining/jobs/{rid}/result", headers=H).status_code == 404
        assert client.get(f"/api/jobs/{rid}", headers=H).json()["status"] == "queued"
        run_job(pg_env, ov)
        assert client.get(f"/api/jobs/{rid}", headers=H).json()["status"] == "succeeded"
        result = client.get(f"/api/knowledge-mining/jobs/{rid}/result", headers=H)
        assert result.json()["files"][0]["uri"] == target
        response = client.get("/api/knowledge-mining/content", headers=H,
                              params={"kind": kind, "uri": target})
        assert response.json()["uri"] == target and response.json()["content"] == "# 原始内容\n"
        tree = client.get("/api/knowledge-mining/tree", headers=H, params={"kind": kind}).json()
        assert tree["tree"][0]["uri"] == target
        assert client.get(f"/api/knowledge-mining/jobs/{rid}/result",
                          headers={"X-Tenant-Id": "alpha"}).status_code == 404
        assert client.get(f"/api/jobs/{rid}", headers={"X-Tenant-Id": "alpha"}).status_code == 404
        removed = client.delete("/api/knowledge-mining/entry", headers=H,
                                params={"kind": kind, "uri": target})
        assert removed.status_code == 202, removed.text
        assert target in ov.documents
        run_job(pg_env, ov)
        assert target not in ov.documents
        assert client.get(f"/api/jobs/{removed.json()['job_id']}", headers=H).json()["status"] == "succeeded"


def test_upload_retry_uses_persistent_file_checkpoints(pg_env, tmp_path, monkeypatch):
    ov = KnowledgeOV()
    first, second = SOURCE + "/one.md", SOURCE + "/two.md"
    ov.fail_writes.add(second)
    files = [{"path": path, "content_b64": base64.b64encode(text.encode()).decode()}
             for path, text in [("one.txt", "第一个"), ("two.md", "# 第二个")]]
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        accepted = client.post("/api/knowledge-mining/upload", headers=H, json={"files": files})
        assert accepted.status_code == 202, accepted.text
        rid = accepted.json()["job_id"]
    run_job(pg_env, ov)
    assert first in ov.documents and second not in ov.documents
    ov.fail_writes.clear()
    _run(pg_env, "default", lambda c: c.execute(
        f"UPDATE {pg_env.schema}.worker_tasks SET available_at=now() WHERE task_id=$1", rid))
    run_job(pg_env, ov)
    state = MinerState(pg_env.tx, "default")
    result = _run(pg_env, "default", lambda c: state.get(c, "result", rid))
    assert {row["uri"] for row in result["files"]} == {first, second}
    assert result["total_bytes"] == sum(len(ov.documents[path].encode()) for path in (first, second))
    first_writes = [call for call in ov.calls if call[1] == "/api/v1/content/write"
                    and call[2]["json_body"]["uri"] == first]
    assert len(first_writes) == 1


def test_graph_result_contains_links_and_is_not_a_compile(pg_env, tmp_path, monkeypatch):
    ov = KnowledgeOV()
    ov.documents = {WIKI + "/index.md": "# 导航\n[方法](methods/check.md)\n",
                    WIKI + "/methods/check.md": "# 检查方法\n"}
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        accepted = client.post("/api/knowledge-mining/graph", headers=H, json={})
        assert accepted.status_code == 202, accepted.text
        rid = accepted.json()["job_id"]
        run_job(pg_env, ov)
        result = client.get(f"/api/knowledge-mining/jobs/{rid}/result", headers=H)
        assert result.status_code == 200, result.text
        graph = result.json()
        assert len(graph["graph"]["nodes"]) == 2
        assert len(graph["graph"]["links"]) == 1
        assert "检查方法" in graph["html"]
        assert not ov.submissions


@pytest.mark.parametrize("operation", ["upload", "delete", "graph", "compile"])
def test_cancel_before_worker_has_no_external_effect(pg_env, tmp_path, monkeypatch, operation):
    ov = KnowledgeOV()
    target = SOURCE + "/retained.md"
    ov.documents[target] = "保留内容"
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        if operation == "upload":
            response = client.post("/api/knowledge-mining/content", headers=H,
                                   json={"uri": SOURCE + "/new.md", "content": "新内容"})
        elif operation == "delete":
            response = client.delete("/api/knowledge-mining/entry", headers=H, params={"uri": target})
        else:
            path = "tasks" if operation == "compile" else operation
            response = client.post("/api/knowledge-mining/" + path, headers=H, json={})
        assert response.status_code == 202, response.text
        rid = response.json()["job_id"]
        assert client.post(f"/api/jobs/{rid}/cancel", headers=H).status_code == 200
        run_job(pg_env, ov)
        assert client.get(f"/api/jobs/{rid}", headers=H).json()["status"] == "cancelled"
    assert ov.documents == {target: "保留内容"}
    assert not ov.calls


@pytest.mark.parametrize("method,path,params,body", [
    ("get", "/content", {"uri": SOURCE}, None),
    ("get", "/content", {"uri": WIKI + "/a.md", "kind": "source"}, None),
    ("delete", "/entry", {"uri": SOURCE}, None),
    ("post", "/content", None, {"uri": SOURCE + "/../escape.md", "content": "bad"}),
    ("post", "/upload", None, {"files": [{"path": "../escape.md", "content_b64": "YQ=="}]}),
    ("post", "/upload", None, {"files": [{"path": "a.md", "content_b64": "YQ=="},
                                         {"path": "a.txt", "content_b64": "Yg=="}]}),
    ("post", "/tasks", None, {"target_uri": WIKI + "/override"}),
    ("post", "/tasks", None, {"last_compile_time": "invalid"}),
])
def test_invalid_commands_are_rejected_before_acceptance(pg_env, tmp_path, monkeypatch, method, path, params, body):
    ov = KnowledgeOV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        bind(client, ov)
        kwargs = {"headers": H, "params": params}
        if body is not None:
            kwargs["json"] = body
        response = getattr(client, method)("/api/knowledge-mining" + path, **kwargs)
        assert response.status_code == 400, response.text
    assert not ov.calls
    assert _run(pg_env, "default", lambda c: c.fetchval(f"SELECT count(*) FROM {pg_env.schema}.worker_tasks")) == 0


def test_account_mismatch_missing_config_and_member_access_are_rejected(pg_env, tmp_path, monkeypatch):
    ov = KnowledgeOV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        bind(client, ov)
        assert client.get("/api/knowledge-mining/config?account=other", headers=H).status_code == 403
        _run(pg_env, "default", lambda c: c.execute(
            f"DELETE FROM {pg_env.schema}.tenant_configs WHERE module='miner'"))
        assert client.get("/api/knowledge-mining/config", headers=H).status_code == 503
        client.post("/api/auth/logout")
        registered = client.post("/api/auth/register", json={"username": "member", "password": "test-password"})
        assert registered.status_code == 200, registered.text
        client.app.state.services.extras["console_sessions"]._store().set_member(
            "default", "member", "member", "active", "root")
        assert client.get("/api/knowledge-mining/config", headers=H).status_code == 403
    assert not ov.calls


def test_accepted_operation_freezes_workspace_and_revision(pg_env, tmp_path, monkeypatch):
    ov = KnowledgeOV()
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        response = client.post("/api/knowledge-mining/content", headers=H,
                               json={"uri": SOURCE + "/frozen.md", "content": "旧配置下受理"})
        assert response.status_code == 202, response.text
        rid = response.json()["job_id"]
    repo = ConfigRepository(pg_env.tx)

    async def change(conn):
        current = await repo.read_tenant_module(conn, "miner")
        await repo.write_tenant_module(conn, module="miner", expected_revision=current.revision,
                                      config={"source_uri": "viking://resources/new-input"})

    _run(pg_env, "default", change)
    run_job(pg_env, ov)
    assert SOURCE + "/frozen.md" in ov.documents
    assert _run(pg_env, "default", lambda c: c.fetchval(
        f"SELECT config_revision FROM {pg_env.schema}.worker_tasks WHERE task_id=$1", rid)) == 1


def test_compile_success_links_job_upstream_index_and_next_watermark(pg_env, tmp_path, monkeypatch):
    ov = KnowledgeOV()
    ov.documents[WIKI + "/index.md"] = "# 完成页\n[方法](methods/check.md)\n"
    ov.documents[WIKI + "/methods/check.md"] = "# 检查方法\n"
    with _pod(pg_env, tmp_path, monkeypatch, pod="a") as client:
        _login(client, bootstrap=True)
        bind(client, ov)
        headers = {**H, "Idempotency-Key": "compile-workflow"}
        accepted = client.post("/api/knowledge-mining/tasks", headers=headers, json={"instruction": "整理文档"})
        rid = accepted.json()["job_id"]
        repeated = client.post("/api/knowledge-mining/tasks", headers=headers, json={"instruction": "整理文档"})
        assert repeated.json()["job_id"] == rid
        conflict = client.post("/api/knowledge-mining/tasks", headers=headers, json={"instruction": "更改要求"})
        assert conflict.status_code == 409
        run_job(pg_env, ov)
        detail = client.get(f"/api/knowledge-mining/tasks/{ov.task_id}", headers=H)
        assert detail.status_code == 200, detail.text
        assert detail.json()["task"]["status"] == "succeeded"
        assert detail.json()["job_id"] == rid
        listed = client.get("/api/knowledge-mining/tasks", headers=H).json()["tasks"]
        assert [task["task_id"] for task in listed] == [ov.task_id]
        alpha = {"X-Tenant-Id": "alpha"}
        assert client.get(f"/api/knowledge-mining/tasks/{ov.task_id}", headers=alpha).status_code == 404
        forbidden_cancel = client.post(f"/api/knowledge-mining/tasks/{ov.task_id}/cancel", headers=alpha, json={})
        assert forbidden_cancel.status_code == 404
        assert client.get("/api/knowledge-mining/tasks", headers=alpha).json()["tasks"] == []
        watermark = client.get("/api/knowledge-mining/capabilities", headers=H).json()["last_compile_time"]
        assert watermark
        run_job(pg_env, ov)
        state = MinerState(pg_env.tx, "default")
        receipt = _run(pg_env, "default", lambda c: state.get(c, "receipt", rid))
        index = _run(pg_env, "default", lambda c: state.get(c, "index", state.scope(receipt["settings"])))
        assert index["compile_task_id"] == ov.task_id and index["page_count"] == 2
        assert index["pages"][WIKI + "/index.md"]["links"][0]["target_uri"] == WIKI + "/methods/check.md"
        assert index["pages"][WIKI + "/methods/check.md"]["backlinks"][0]["source_uri"] == WIKI + "/index.md"
        next_job = client.post("/api/knowledge-mining/tasks", headers=H, json={})
        next_payload = _run(pg_env, "default", lambda c: state.get(c, "payload", next_job.json()["job_id"]))
        assert next_payload["compile"]["args"]["last_compile_time"] == watermark
        assert len(ov.submissions) == 1
