#!/usr/bin/env python3
"""Isolated real-HTTP smoke with temporary state and no external services."""
from __future__ import annotations
import json
import os
from pathlib import Path
import socket
import sys
import tempfile

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


def main():
    with tempfile.TemporaryDirectory(prefix="te-deregister-smoke-") as directory:
        root = Path(directory)
        for key in list(os.environ):
            if key.startswith(("TEAMEVOLVER_", "EVOLVE_", "LANGFUSE_", "OPENAI_")):
                os.environ.pop(key)
        os.environ.update(TEAMEVOLVER_CONFIG_DIR=str(root), TEAMEVOLVER_EMBEDDED_EVOLVE_ENABLED="0",
                          TEAMEVOLVER_SKILLMINER_ENABLED="0", TEAMEVOLVER_ROOT_API_KEY="smoke-root")
        from teamEvolver.config import TeamEvolverConfig
        from teamEvolver.proxy import ProxyServer
        from teamEvolver.session_store import SessionStore
        import httpx
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]
        config = TeamEvolverConfig(proxy_host="127.0.0.1", proxy_port=port,
            _config_file=str(root / "config.yaml"), users_registry_path=str(root / "users.json"),
            skills_dir=str(root / "skills"), sharing_local_root=str(root / "store"),
            sharing_enabled=False, sharing_skill_mirror_enabled=False,
            sharing_viking_endpoint="", sharing_viking_account="smoke-account",
            tenant_machine_token="tevt_smoke", aggregation_enabled=False,
            validation_enabled=False, llm_api_key="", session_split_enabled=False)
        if hasattr(config, "agent_protocol_identity_mode"):
            config.agent_protocol_identity_mode = "tenant_user"
            config.skills_delivery_mode = "pull"
        (root / "config.yaml").write_text("sharing:\n  enabled: false\n")
        skill = root / "skills" / "smoke"
        skill.mkdir(parents=True)
        (skill / "SKILL.md").write_text("---\nname: smoke\ndescription: Smoke\n---\nHello")
        server = ProxyServer(config)
        pid_file = Path(f"/tmp/te_deregister_smoke_{os.getpid()}.pid")
        pid_file.write_text(str(os.getpid()))
        report = {"port": port, "state": "temporary", "external_services": False}
        server.start()
        try:
            assert server.wait_until_ready(20), "HTTP startup failed"
            with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=15, trust_env=False) as client:
                auth = {"Authorization": "Bearer tevt_smoke"}
                assert client.get("/healthz").status_code == 200
                assert client.get("/").status_code == 200
                described = client.get("/internal/agents/context/describe?user_id=alice", headers=auth)
                assert described.status_code == 200, described.text
                assert described.json()["subject"] == {"tenant_id": "default", "user_id": "alice"}
                pull = client.get("/sync/skills?user_id=alice", headers=auth)
                assert pull.status_code == 200 and len(pull.json()["skills"]) == 1
                assert client.get("/sync/skills?user_id=alice", headers={**auth, "If-None-Match": pull.headers["ETag"]}).status_code == 304
                assert client.get("/sync/skills?user_id=alice").status_code == 401
                assert client.get("/sync/skills?user_id=alice", headers={**auth, "X-Tenant-Id": "other"}).status_code == 403
                body = {"schema_version": "teamevolver.agent-session.v2", "protocol_version": "2.0",
                        "session_id": "smoke-session", "runtime": {"type": "unregistered"},
                        "runtime_context": {"user_id": "alice"},
                        "turns": [{"prompt_text": "Record a reusable workflow", "response_text": "Use the saved workflow"}]}
                response = client.post("/ingest_session", json=body, headers=auth)
                assert response.status_code == 200, response.text
                result = response.json()
                archive = SessionStore.from_config(config).load_archived(result["session_id"])
                assert archive and archive["meta"]["user_id"] == "alice", result
                assert archive["runtime_context"]["source_session_id"] == "smoke-session"
                legacy = {**body, "schema_version": "teamevolver.agent-session.v1", "protocol_version": "1.0"}
                assert client.post("/ingest_session", json=legacy, headers=auth).status_code == 400
                report.update(health=True, console=True, context_v2=True, skill_pull=True, etag_304=True,
                              tenant_override_rejected=True, session_archive=True, legacy_rejected=True)
                if not hasattr(config, "agent_protocol_identity_mode"):
                    assert client.post("/internal/agents/register", json={}, headers={"Authorization": "Bearer smoke-root"}).status_code == 404
                    report["registration_removed"] = True
        finally:
            server.stop()
            pid_file.unlink(missing_ok=True)
        with socket.socket() as probe:
            assert probe.connect_ex(("127.0.0.1", port)) != 0, "smoke port not released"
        report["port_released"] = True
        print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
