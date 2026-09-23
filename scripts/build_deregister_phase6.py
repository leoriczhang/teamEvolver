#!/usr/bin/env python3
"""Build the separate, strict phase-6 source release without touching live data.

The compatibility tree is the input. Transformations fail on unexpected source
shapes. Output includes a complete source tree, a reviewable diff and hashes.
"""
from __future__ import annotations

import argparse
import ast
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import textwrap

ROOTS = (
    "teamEvolver", "team_skills", "team_replay", "team_memory", "team_miner",
    "session_ingestion", "tests", "scripts", "docs", "web-ui", "config", "docker",
    "cicd_scripts", "pyproject.toml", "requirements.txt", "README.md", "README.en.md",
    "AGENTS.md", "CONTEXT.md", "CUSTOMER_DEPLOYMENT.md", "LICENSE", "Dockerfile",
    "compose.yaml", "compose.customer.yaml", "run_local.sh", "agent-deregistration-plan.md",
    "agent-deregistration-acceptance.md",
)
IGNORED = {".git", ".venv", "node_modules", "__pycache__", ".pytest_cache",
           ".verify-deps", ".env", "data", "logs", "output", "outputs"}
REMOVED = (
    "teamEvolver/integrations/agent_registry.py",
    "teamEvolver/integrations/skill_sync_adapters.py",
    "teamEvolver/integrations/legacy_agent_identity.py",
    "teamEvolver/integrations/legacy_context_workspace.py",
    "session_ingestion/push/auth.py",
)


class Source:
    def __init__(self, root):
        self.root = root

    def read(self, path):
        return (self.root / path).read_text()

    def write(self, path, text):
        if path.endswith(".py"):
            ast.parse(text, filename=path)
        (self.root / path).write_text(text)

    def replace(self, path, old, new, count=1):
        text = self.read(path)
        if text.count(old) != count:
            raise ValueError(f"{path}: expected {count} occurrences of {old[:100]!r}, got {text.count(old)}")
        self.write(path, text.replace(old, new))

    def remove_functions(self, path, names):
        text = self.read(path)
        lines = text.splitlines(keepends=True)
        nodes = [n for n in ast.walk(ast.parse(text))
                 if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name in names]
        if set(n.name for n in nodes) != set(names):
            raise ValueError(f"{path}: missing functions {set(names) - set(n.name for n in nodes)}")
        for node in sorted(nodes, key=lambda n: n.lineno, reverse=True):
            start = min([node.lineno] + [d.lineno for d in node.decorator_list])
            del lines[start - 1:node.end_lineno]
        self.write(path, "".join(lines))

    def cut(self, path, start, end, replacement=""):
        text = self.read(path)
        matches = list(re.finditer("^" + re.escape(start), text, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError(f"{path}: ambiguous range")
        a = matches[0].start()
        end_match = re.search("^" + re.escape(end), text[a + len(start):], re.MULTILINE)
        if end_match is None:
            raise ValueError(f"{path}: missing range end")
        b = a + len(start) + end_match.start()
        self.write(path, text[:a] + replacement + text[b:])


def transform(s):
    p = "teamEvolver/proxy/routes.py"
    s.remove_functions(p, {
        "_check_ingest_api_key", "_check_v1_control_plane_key", "_register_agent_runtime",
        "register_agent_runtime", "sync_agentshub_openviking_config", "api_agent_integrations",
        "api_register_agent_integration", "api_retry_skill_sync", "api_discard_skill_sync",
    })
    s.cut(p, "from ..integrations.agent_protocol import is_v1_payload\n", "from ..session_store")
    s.replace(p, "    sync_agent_subject_mappings,\n", "")
    s.cut(p, '                    if getattr(request.state, "agent_legacy_identity", False):\n',
          "                    return response\n")
    s.replace(p, "                            # Keep this entry scoped to /context — never widen it to\n"
                 "                            # /internal/agents (that would expose /internal/agents/register).\n",
                 "                            # Authorize only the Context namespace.\n")

    p = "teamEvolver/proxy/agent_context.py"
    s.replace(p, "from ..integrations.agent_protocol import CONTEXT_RESULT_SCHEMA_V1\n", "")
    s.replace(p, "from ..integrations.agent_principal import AgentPrincipal\n"
                 "from ..integrations.legacy_agent_identity import resolve_request_principal\n",
                 "from ..integrations.agent_principal import AgentPrincipal, resolve_agent_principal\n")
    s.replace(p, "principal = resolve_request_principal(request, self._workspace_config(), body)",
                 'principal = resolve_agent_principal(request, self._workspace_config(), body.get("user_id"))')
    s.cut(p, '        legacy_user = getattr(request.state, "agent_legacy_user", None)\n', "        return principal, user\n")
    s.replace(p, '            legacy = getattr(request.state, "agent_legacy_record", {})\n'
                 '            return ContextStateStore(owner._workspace_config(), legacy_agent_id=legacy.get("agent_id", ""))',
                 "            return ContextStateStore(owner._workspace_config())")
    for line in ('            external_subject: str = Query(default=""),\n',
                 '            integration_id: str = Query(default=""),\n'):
        s.replace(p, line, "", count=2)
    s.replace(p, '            identity = {"external_subject": external_subject, "integration_id": integration_id}\n'
                 '            if user_id is not None:\n                identity["user_id"] = user_id\n',
                 '            identity = {"user_id": user_id}\n', count=2)
    s.replace(p, '            if getattr(request.state, "agent_legacy_identity", False):\n'
                 '                payload.update(protocol_version="1.0", integration_id=integration_id)\n', "")
    s.cut(p, '                    "schema_version": (\n', '                    "snapshot_id": snapshot_id,\n',
          '                    "schema_version": "teamevolver.context-result.v2",\n'
          '                    "subject": {"tenant_id": principal.tenant_id, "user_id": principal.user_id},\n')

    p = "teamEvolver/integrations/context_workspace.py"
    s.replace(p, 'tenant_id: str | None = None, legacy_agent_id: str = "",', "tenant_id: str | None = None,")
    s.replace(p, '        self._legacy_agent_id = legacy_agent_id if getattr(config, "agent_protocol_identity_mode", "dual") == "dual" else ""\n', "")
    s.replace(p, '            **({"agent_id": self._legacy_agent_id} if self._legacy_agent_id else {}),\n', "")
    s.cut(p, '        if tenant_id is None:\n', "        return tenant_id == principal.tenant_id\n")
    p = "teamEvolver/integrations/agent_principal.py"
    s.replace(p, '        mode = "legacy" if getattr(request.state, "agent_legacy_identity", False) else "tenant_user"\n'
                 '        increment("agent_identity_requests_total", mode=mode)',
                 '        increment("agent_identity_requests_total", mode="tenant_user")')

    p = "session_ingestion/push/routes.py"
    old = s.read(p)
    body = old[old.index("            principal = resolve_agent_principal("):old.index("        request.state.agent_legacy_identity = True")]
    body = textwrap.dedent(body)
    legacy_upgrade = ('    if not v2_payload:\n'
                      '        body = {**body, "schema_version": "teamevolver.agent-session.v2", "protocol_version": "2.0"}\n')
    if body.count(legacy_upgrade) != 1:
        raise ValueError("Session route legacy branch changed")
    body = body.replace(legacy_upgrade, "")
    s.write(p, '''"""Strict tenant/user Session push route."""
from __future__ import annotations

import asyncio
from typing import Any, Callable
from fastapi import HTTPException, Request
from teamEvolver.integrations.agent_principal import resolve_agent_principal
from teamEvolver.integrations.context_workspace import verify_context_usage
from teamEvolver.integrations.protocol_metrics import increment
from teamEvolver.tenants.registry import effective_config, get_current_tenant
from ..http import read_limited_json_body
from ..identifiers import InvalidSessionId, tenant_user_session_id
from ..service import SessionIngestionUnavailable, ingest
from .protocol import AgentProtocolError, is_session_v2_payload, normalize_session_envelope


def register_push_routes(owner: Any, app: Any, *, invalidate_cache: Callable[..., None] | None = None) -> None:
    @app.post("/ingest_session")
    async def ingest_session(request: Request):
        body = await read_limited_json_body(request)
        config = effective_config(None, get_current_tenant(), owner.config)
        if not is_session_v2_payload(body):
            increment("agent_identity_rejected_total", reason="PROTOCOL_VERSION_UNSUPPORTED")
            raise HTTPException(status_code=400, detail="PROTOCOL_VERSION_UNSUPPORTED: use agent-session.v2")
''' + textwrap.indent(body, "        "))
    p = "session_ingestion/push/protocol.py"
    s.remove_functions(p, {"_require_supported_version", "is_session_v1_payload", "normalize_session_envelope"})
    s.replace(p, 'AGENT_PROTOCOL_VERSION = "1.0"\nREGISTRATION_SCHEMA_V1 = "teamevolver.agent-registration.v1"\n'
                 'SESSION_SCHEMA_V1 = "teamevolver.agent-session.v1"\n', 'AGENT_PROTOCOL_VERSION = "2.0"\n')
    s.write(p, s.read(p) + '''

def normalize_session_envelope(payload: dict[str, Any]) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise AgentProtocolError("session payload must be an object")
    if not is_session_v2_payload(payload):
        raise AgentProtocolError("PROTOCOL_VERSION_UNSUPPORTED: use agent-session.v2")
    return _normalize_v2(payload)
''')
    p = "session_ingestion/push/__init__.py"
    s.replace(p, "SESSION_SCHEMA_V1", "SESSION_SCHEMA_V2", count=2)
    s.replace(p, "is_session_v1_payload", "is_session_v2_payload", count=2)
    # Replay transport v1 is independent of the retired registration protocol.
    s.write("teamEvolver/integrations/agent_protocol.py", '''"""Replay wire facade; registration and Session v1 have been retired."""
from team_replay.protocol import (
    ReplayProtocolError as AgentProtocolError,
    normalize_replay_request, normalize_replay_result,
    normalize_replay_turn_request, normalize_replay_turn_result, replay_request_id,
)
from session_ingestion.push.protocol import normalize_session_envelope
''')

    p = "teamEvolver/proxy/users_admin.py"
    s.remove_functions(p, {
        "_normalize_agent_identities", "_normalize_agent_subjects", "_validate_unique_agent_identities",
        "_validate_unique_agent_subjects", "resolve_registered_user_id", "resolve_agent_subject_user_id",
        "sync_agent_subject_mappings", "_default_agent_subjects",
    })
    s.cut(p, '        "agent_identities": dict(user.get("agent_identities") or {}),\n', '        "password_set":')
    s.cut(p, '    agent_subjects = body.get("agent_subjects")\n', "    user = {\n")
    s.cut(p, '        "agent_identities": _normalize_agent_identities(\n', '        "password_hash":')
    s.replace(p, "    _validate_unique_agent_identities(data, user)\n    _validate_unique_agent_subjects(data, user)\n", "")
    s.replace(p, '                "agent_identities": body.get(\n'
                 '                    "agent_identities", existing.get("agent_identities", {})\n'
                 '                ),\n                "agent_subjects": existing.get("agent_subjects", []),\n', "")
    s.replace("teamEvolver/proxy/tenant_routes.py", "# persisted (same policy as agent_registry).", "# persisted.")

    p = "team_skills/library/mutations.py"
    s.remove_functions(p, {"_due", "_event_key", "drain", "_cache_outbox", "_outbox_settled", "health", "retry", "discard",
                           "pull_enabled", "cancel_pending_for_pull"})
    s.replace(p, '"""Transactional team-Skill mutations with a durable sync outbox."""',
                 '"""Versioned team-Skill publication and tombstones for authenticated pull."""')
    s.replace(p, '"""Deep module owning commit records, tombstones, outbox and delivery."""',
                 '"""Own commit records and tombstones; Agents pull the published library."""')
    s.replace(p, "import asyncio\n", "")
    s.replace(p, "from datetime import datetime, timedelta, timezone", "from datetime import datetime, timezone")
    s.replace(p, "from typing import Any, Awaitable, Callable", "from typing import Any")
    s.replace(p, '        deliverer: Callable[[Any, dict[str, Any]], Awaitable[dict[str, Any]]] | None = None,\n', "", count=2)
    s.cut(p, "        self._deliverer = deliverer\n", "        self._reconciled_keys: set[str] = set()\n")
    s.replace(p, "return cls(hub=hub, config=config, deliverer=deliverer)", "return cls(hub=hub, config=config)")
    text = s.read(p)
    a = text.index("        if self.pull_enabled:\n")
    b = text.index("        expected_fingerprint =", a)
    c = text.index("    def reconcile(", b)
    body = textwrap.dedent(text[a + len("        if self.pull_enabled:\n"):b])
    s.write(p, text[:a] + textwrap.indent(body, "        ") + "\n" + text[c:])
    s.cut(p, '            event_id = str(commit.get("event_id") or "")\n', '        for item in self._bucket.iter_objects("skill_tombstones/"):\n')

    p = "teamEvolver/launcher.py"
    s.remove_functions(p, {"_run_skill_sync_outbox", "_cancel_skill_sync_outbox"})
    s.replace(p, "        self._skill_sync_task = None\n", "", count=2)
    s.cut(p, '        if getattr(cfg, "sharing_enabled", False):\n', "        try:\n            while not self._stop_event.is_set():\n")
    s.cut(p, "            if self._skill_sync_task is not None:\n", "    # ------------------------------------------------------------------ #\n    # PID / signals")

    p = "team_skills/candidates/worker.py"
    s.remove_functions(p, {"_save_agentshub_sync_status", "_sync_agentshub_skills"})
    s.replace(p, '        self._save_agentshub_sync_status(\n            job_id,\n            status="failed",\n'
                 '            detail="timed out waiting for committed published bundle",\n        )\n', "")
    s.replace(p, "_sync_registered_agents", "_record_skill_publication", count=2)
    s.cut(p, "        if service.pull_enabled:\n", "        decision = self._store.load_decision(job_id)\n",
          '        payload = {"status": "published", "delivery_mode": "pull"}\n')
    s.replace(p, 'decision["skill_delivery" if service.pull_enabled else "agent_sync"]', 'decision["skill_delivery"]')
    s.replace(p, '            if service.pull_enabled:\n                decision.pop("agent_sync", None)\n'
                 '                decision.pop("agentshub_sync", None)\n', "")

    p = "teamEvolver/config.py"
    s.replace(p, '    skills_delivery_mode: str = "push"\n    agent_protocol_identity_mode: str = "dual"\n', "")
    p = "teamEvolver/config_store/defaults.py"
    s.replace(p, '        "delivery_mode": "push",\n', "")
    s.replace(p, '    "agent_protocol": {"identity_mode": "dual"},\n', "")
    p = "teamEvolver/config_store/bridge.py"
    s.replace(p, '            ("TEAMEVOLVER_AGENT_IDENTITY_MODE", "agent_protocol", "identity_mode"),\n'
                 '            ("TEAMEVOLVER_SKILLS_DELIVERY_MODE", "skills", "delivery_mode"),\n', "")
    s.replace(p, '        agent_protocol = data.get("agent_protocol", {})\n', "")
    s.cut(p, '        identity_mode = str(agent_protocol.get(', '        orouter = data.get("openrouter", {})\n')
    s.replace(p, "            skills_delivery_mode=delivery_mode,\n            agent_protocol_identity_mode=identity_mode,\n", "")
    s.replace("teamEvolver/tenants/registry.py", '        "agent_protocol_identity_mode",\n        "skills_delivery_mode",\n', "")
    # The offline migration tool also runs from the final release for recovery.
    s.replace("scripts/migrate_deregister.py",
              'if config.agent_protocol_identity_mode != "tenant_user" or config.skills_delivery_mode != "pull":',
              'if getattr(config, "agent_protocol_identity_mode", "tenant_user") != "tenant_user" or getattr(config, "skills_delivery_mode", "pull") != "pull":')
    s.replace("teamEvolver/session_store.py", '            str(runtime.get("integration_id") or ""),\n',
              '            str((session.get("runtime_context") if isinstance(session.get("runtime_context"), dict) else {}).get("user_id") or ""),\n')
    for p in ("team_skills/evolution/runtime/evidence.py", "team_skills/evolution/runtime/orchestrator.py"):
        s.replace(p, '"external_subject",', '"user_id",')
    p = "team_replay/turn_server.py"
    s.cut(p, "To onboard YOUR agents,", "Per-turn contract",
          "Configure a tenant ReplayAdapterFactory using the turn-based HTTP preset.\n"
          "The preset selects this server's /turn/<runtime_type> endpoint and auth.\n\n")

    for p in REMOVED:
        (s.root / p).unlink()
    # Tests for removed surfaces are replaced, while the compatibility tree keeps
    # their original coverage. All v2, isolation, Replay and migration tests remain.
    (s.root / "tests/test_tenant_machine_credentials.py").unlink()
    s.remove_functions("team_memory/tests/test_dreamcycle_integration.py", {
        "test_agentshub_config_sync_merges_personal_sources",
        "test_generic_agent_registration_cannot_override_local_storage",
        "test_v1_registration_syncs_existing_subject_mappings",
    })
    s.replace("team_memory/tests/test_dreamcycle_integration.py", "    resolve_agent_subject_user_id,\n", "")
    p = "tests/test_agent_principal_v2.py"
    s.replace(p, '        agent_protocol_identity_mode="tenant_user", **kwargs,', "        **kwargs,")
    s.replace(p, '    monkeypatch.setattr("teamEvolver.integrations.agent_registry.resolve_active_agent", forbidden)\n',
              '    import importlib.util\n    assert importlib.util.find_spec("teamEvolver.integrations.agent_registry") is None\n')
    s.remove_functions(p, {"test_config_roundtrip_env_and_deployment_switches"})
    s.remove_functions("tests/test_skill_pull_v2.py", {"test_pull_publish_creates_no_outbox_and_cancels_pending"})
    p = "tests/test_deregister_migration.py"
    s.replace(p, ',\n                              agent_protocol_identity_mode="tenant_user", skills_delivery_mode="pull"', "")
    s.replace(p, ',\n                           agent_protocol_identity_mode="tenant_user", skills_delivery_mode="pull"', "")
    overlay = s.root / "scripts/phase6"
    for item in overlay.glob("test_*.py"):
        shutil.copy2(item, s.root / "tests" / item.name)
    s.replace("docs/zh/guides/11-agent-deregistration.md",
              "此仓库为兼容发布，默认 `dual/push`，不自动改动生产数据。",
              "此源码为阶段 6 独立清理版，固定 v2 + pull。下文保留完整迁移时序；先用兼容版完成阶段 5，稳定观察后再发布本版。")
    s.replace("docs/en/guides/11-agent-deregistration.md",
              "This tree is the compatibility release, defaulting to `dual/push`; it does not migrate production data automatically.",
              "This is the separate phase-6 release, fixed to v2 + pull. The sequence below covers the full rollout: complete phase 5 with the compatibility release and observe stability before deploying this tree.")
    # Historical prose stays available, but deleted-code hyperlinks point to
    # the migration guide. No historical Python implementation is shipped.
    for lang in ("zh", "en"):
        for path in (s.root / "docs" / lang).rglob("*.md"):
            text = path.read_text()
            for retired in ("agent_registry.py", "skill_sync_adapters.py"):
                text = re.sub(r"\[([^\]]*)\]\([^)]*/integrations/" + re.escape(retired) + r"\)",
                              lambda m: f"[{m[1]} (retired)]({os.path.relpath(s.root / 'docs' / lang / 'guides/11-agent-deregistration.md', path.parent)})", text)
                text = text.replace(f"`teamEvolver/integrations/{retired}`",
                                    f"{retired} (historical compatibility-release code; removed in phase 6)")
            path.write_text(text)


def build(source, destination):
    if destination.exists():
        raise ValueError("destination must not exist; use a new release directory")
    destination.mkdir(parents=True)
    target = destination / "source"
    target.mkdir()

    def ignore(folder, names):
        return [n for n in names if n in IGNORED or n.endswith((".pyc", ".log", ".db", ".sqlite", ".sqlite3"))
                or Path(folder, n).is_symlink()]

    for name in ROOTS:
        item = source / name
        if item.is_dir():
            shutil.copytree(item, target / name, ignore=ignore)
        elif item.is_file():
            shutil.copy2(item, target / name)
    transform(Source(target))
    for path in target.rglob("*.py"):
        text = path.read_text()
        original = source / path.relative_to(target)
        if original.is_file() and original.read_text() == text:
            continue
        path.write_text(re.sub(r"\n(?:[ \t]*\n){3,}", "\n\n\n", text))
    paths = {str(p.relative_to(target)) for p in target.rglob("*") if p.is_file()} | set(REMOVED) | {"tests/test_tenant_machine_credentials.py"}
    diff, changes = [], []
    for rel in sorted(paths):
        old = (source / rel).read_bytes() if (source / rel).is_file() else b""
        new = (target / rel).read_bytes() if (target / rel).is_file() else b""
        if old == new:
            continue
        changes.append({"path": rel, "before_sha256": hashlib.sha256(old).hexdigest(),
                        "after_sha256": hashlib.sha256(new).hexdigest(), "removed": not (target / rel).exists()})
        diff.extend(difflib.unified_diff(old.decode().splitlines(True), new.decode().splitlines(True),
                                       fromfile="a/" + rel, tofile="b/" + rel))
    (destination / "cleanup.patch").write_text("".join(diff))
    (destination / "manifest.json").write_text(json.dumps({
        "release": "agent-deregister-phase6", "requires": "phase5 applied and stable observation",
        "changes": changes,
    }, ensure_ascii=False, indent=2) + "\n")
    return {"source": str(target), "changed_files": len(changes)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(build(args.source.resolve(), args.output.resolve()), indent=2))
