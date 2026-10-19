"""Single-file AgentsHub Replay Adapter, uploaded and bound through the Root API.

Config: replay.agentshub = {"backend_root": "/path/to/AgentsHub/backend",
"tenant_id": "...", "agent_id": "...", "user_id": "...", "python": "..."}.
The last three fields are optional defaults; recorded Session identity wins.

Each branch starts ONE private systemd worker, disposable SQLite database,
model broker and workspace. All send() calls resume that same Agent Session.
Only this file is uploaded; the embedded runtime uses the installed AgentsHub
backend. AgentsHub requires no new HTTP endpoint or source modifications.
The model broker retains credentials outside the isolated Agent worker.
The server owns Checklist judging and user feedback. No Checklist is sent here.
"""
from __future__ import annotations

import copy
import json
import os
import shutil
import socket
import subprocess
import tempfile
import time
from collections.abc import Mapping
from pathlib import Path

from team_replay.hooks import AgentObservation, ReplayUnsupported

REPLAY_ADAPTER = {"label": "AgentsHub — persistent isolated sessions", "enabled": True}


def build_replay_adapter(config):
    values = dict(config) if isinstance(config, Mapping) else vars(config)
    return AgentsHubFactory(dict(values.get("agentshub") or values))


class AgentsHubFactory:
    def __init__(self, config):
        self.config = copy.deepcopy(config)
        if not config.get("backend_root"):
            raise ValueError("replay.agentshub.backend_root is required")

    def open(self, context):
        if context.runtime_type != "agentshub":
            raise ReplayUnsupported(f"AgentsHub adapter cannot run {context.runtime_type!r}")
        return AgentsHubSession(context, self.config)


class AgentsHubSession:
    def __init__(self, context, config):
        self.config = copy.deepcopy(config)
        self.startup = {
            "config": self.config, "request_id": context.request_id,
            "branch": context.treatment.branch, "skill": copy.deepcopy(context.treatment.skill),
            "materials": copy.deepcopy(list(context.materials)),
            "context_snapshot": copy.deepcopy(dict(context.context_snapshot)),
            "source_session": copy.deepcopy(dict(context.source_session)),
            "source_turn_num": context.source_turn_num, "timeout_seconds": context.timeout_seconds,
        }
        self.timeout = context.timeout_seconds
        self.deadline = None
        self.root = None
        self.process = None
        self.connection = None
        self.stream = None
        self.listener = None
        self.closed = False
        self.turn_num = 0

    def remaining(self):
        value = self.deadline - time.monotonic()
        if value <= 0:
            raise TimeoutError("AgentsHub Replay branch deadline exceeded")
        return value

    def _start(self):
        self.deadline = time.monotonic() + self.timeout
        backend = Path(self.config["backend_root"]).resolve()
        python = str(self.config.get("python") or backend / ".venv/bin/python")
        if not backend.is_dir() or not Path(python).is_file():
            raise ReplayUnsupported("AgentsHub backend or Python environment is unavailable on this Worker")
        self.root = Path(tempfile.mkdtemp(prefix="te-agentshub-"))
        os.chmod(self.root, 0o700)
        runtime = self.root / "runtime.py"
        runtime.write_text(AGENTSHUB_RUNTIME, encoding="utf-8")
        startup = {**self.startup, "root": str(self.root), "runtime_path": str(runtime)}
        spec = self.root / "spec.json"
        spec.write_text(json.dumps(startup, ensure_ascii=False), encoding="utf-8")
        self.listener = socket.socket(socket.AF_UNIX)
        self.listener.bind(str(self.root / "control.sock"))
        self.listener.listen(1)
        self.listener.settimeout(self.remaining())
        with (self.root / "controller.log").open("wb") as log:
            self.process = subprocess.Popen(
                [python, str(runtime), "controller", str(spec)],
                cwd=backend, env={**os.environ, "PYTHONPATH": str(backend)},
                stdin=subprocess.DEVNULL, stdout=log, stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        # Bounded polling also detects setup failure without waiting the branch timeout.
        while True:
            self.listener.settimeout(min(1, self.remaining()))
            try:
                self.connection, _ = self.listener.accept()
                break
            except TimeoutError:
                if self.process.poll() is not None:
                    error_path = self.root / "startup-error.json"
                    detail = json.loads(error_path.read_text()) if error_path.exists() else {}
                    raise ReplayUnsupported(detail.get("error") or "AgentsHub isolated worker failed to start")
        self.connection.settimeout(self.remaining())
        self.stream = self.connection.makefile("rb")
        self._check(self._receive())

    def _receive(self):
        self.connection.settimeout(self.remaining())
        line = self.stream.readline(32 * 1024 * 1024 + 1)
        if not line or len(line) > 32 * 1024 * 1024:
            raise RuntimeError("AgentsHub worker closed the connection or exceeded the result limit")
        return json.loads(line)

    @staticmethod
    def _check(result):
        if result.get("status") == "unsupported":
            raise ReplayUnsupported(result.get("error") or "AgentsHub Replay is unsupported")
        if result.get("status") != "succeeded":
            raise RuntimeError(result.get("error") or "AgentsHub Replay execution failed")

    def send(self, user_message):
        if self.closed:
            raise RuntimeError("Replay Session is closed")
        if self.process is None:
            try:
                self._start()
            except Exception:
                self.close()
                raise
        self.turn_num += 1
        self.connection.settimeout(self.remaining())
        self.connection.sendall((json.dumps({
            "turn_num": self.turn_num, "prompt": user_message,
        }, ensure_ascii=False) + "\n").encode())
        result = self._receive()
        self._check(result)
        if result.get("turn_num") != self.turn_num:
            raise RuntimeError("AgentsHub Replay turn number mismatch")
        metrics = dict(result.get("metrics") or {})
        missing = sorted({"total_tokens", "tool_call_count"} - metrics.keys())
        return AgentObservation(
            response=result["response"], messages=tuple(result.get("messages") or []),
            artifacts=tuple(result.get("artifacts") or []), metrics=metrics,
            trace_id=result.get("trace_id", ""),
            metrics_incomplete_reason="missing observed metrics: " + ", ".join(missing) if missing else "",
        )

    def close(self):
        if self.closed:
            return
        self.closed = True
        for handle in (self.stream, self.connection, self.listener):
            if handle is not None:
                handle.close()
        if self.root is not None:
            unit_path = self.root / "unit.txt"
            if unit_path.exists():
                # Stop the exact sandbox cgroup, including its Agent/tool children.
                control = (["sudo", "-n", "/usr/bin/systemctl"] if self.config.get("systemd_scope") == "system"
                           else ["/usr/bin/systemctl", "--user"])
                subprocess.run(
                    [*control, "stop", unit_path.read_text().strip() + ".service"],
                    stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                    timeout=10, check=False,
                )
        if self.process is not None:
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
                    self.process.wait(timeout=5)
        if self.root is not None:
            shutil.rmtree(self.root, ignore_errors=True)


# This runtime is shipped inside the uploaded file. It executes in AgentsHub's
# Python environment, so the TeamEvolver process never imports AgentsHub app.
AGENTSHUB_RUNTIME = r'''
from __future__ import annotations

import base64
import copy
import json
import os
import secrets
import shlex
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path
from uuid import uuid4


def emit(connection, value):
    connection.sendall((json.dumps(value, ensure_ascii=False, default=str) + "\n").encode())


def identity(spec, source_path):
    source = spec.get("source_session") or {}
    recorded = dict(source.get("runtime_context") or {})
    session_id = recorded.get("source_session_id") or source.get("session_id")
    with sqlite3.connect(f"file:{source_path}?mode=ro", uri=True) as db:
        db.row_factory = sqlite3.Row
        row = db.execute(
            "SELECT tenant_id,user_id,agent_id,environment_id,harness_id,harness_config_json "
            "FROM sessions WHERE id=?", (session_id or "",),
        ).fetchone()
    result = {key: spec["config"].get(key) for key in (
        "tenant_id", "user_id", "agent_id", "environment_id", "model_config_id", "harness_id", "harness_config",
    )}
    result.update({k: v for k, v in recorded.items() if v not in (None, "")})
    if row is not None:
        # Database identity is authoritative for the recorded source Session.
        result.update({k: row[k] for k in ("tenant_id", "user_id", "agent_id", "environment_id", "harness_id")})
        result["harness_config"] = json.loads(row["harness_config_json"] or "{}")
    result["harness_id"] = result.get("harness_id") or "pi"
    if not result.get("tenant_id") or not result.get("agent_id"):
        raise ValueError("Replay requires a recorded or configured tenant_id and agent_id")
    return result


def controller(spec):
    from app.config import get_settings
    from app.db import engine
    from app.integrations import team_evolver_replay as native
    from app.integrations.replay_isolation import (
        build_minimal_replay_db,
        encrypt_with_secret,
        replay_context_projection,
        replay_model_broker,
        systemd_launcher_environment,
        systemd_replay_command,
    )

    root = Path(spec["root"])
    settings = get_settings()
    if not settings.team_evolver_replay_enabled:
        raise ValueError("AgentsHub Replay is disabled")
    if engine.url.get_backend_name() != "sqlite" or not engine.url.database or engine.url.database == ":memory:":
        raise ValueError("AgentsHub Replay requires a file-backed SQLite source database")
    source_path = Path(engine.url.database).resolve()
    spec["identity"] = identity(spec, source_path)
    routes = native._load_model_broker_routes(source_path, tenant_id=spec["identity"]["tenant_id"])
    requested_model = spec["identity"].get("model_config_id")
    if not routes or (requested_model and requested_model not in routes):
        raise ValueError("The recorded Agent model configuration is unavailable")
    token, secret = secrets.token_urlsafe(32), secrets.token_urlsafe(48)
    context_path = root / "context.json"
    context_path.write_text(json.dumps(replay_context_projection(spec["context_snapshot"])), encoding="utf-8")
    python = sys.executable
    backend = Path(spec["config"]["backend_root"]).resolve()
    pi_command = ""
    if spec["identity"]["harness_id"] == "pi":
        parts = shlex.split(spec["config"].get("pi_command") or settings.pi_agent_command or "pi")
        executable = shutil.which(parts[0]) if parts else None
        if not executable:
            raise ValueError("AgentsHub Pi executable is unavailable")
        pi_command = shlex.join([executable, *parts[1:]])
    unit = "te-agentshub-" + uuid4().hex
    (root / "unit.txt").write_text(unit)
    timeout = int(spec["timeout_seconds"])
    with replay_model_broker(
        routes, socket_path=root / "model-broker.sock", token=token, timeout_seconds=timeout,
    ) as broker:
        model_routes = {name: {
            "base_url": broker.model_base_url(name, 0),
            "api_key_encrypted": encrypt_with_secret(token, secret),
        } for name in routes}
        build_minimal_replay_db(
            source_path, root / "replay.db", tenant_id=spec["identity"]["tenant_id"],
            user_id=spec["identity"].get("user_id") or "", model_routes=model_routes,
        )
        env = native._worker_environment(
            root=root, replay_db=root / "replay.db", backend_root=backend, app_secret=secret,
            context_path=context_path, pi_command=pi_command, target_skill_name="",
            broker_socket_path=root / "model-broker.sock", broker_sidecar_port=0, timeout_seconds=timeout,
        )
        env["ULTRARAG_DOTENV"] = "/dev/null"
        # Agent tools use the already installed backend dependencies, including
        # python-pptx/Pillow, instead of attempting network package installs.
        env["PATH"] = str(backend / ".venv/bin") + ":" + env["PATH"]
        spec_path = root / "worker.json"
        spec_path.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
        command = systemd_replay_command(
            worker_python=python, backend_root=backend, replay_root=root,
            spec_path=spec_path, timeout_seconds=timeout, unit_name=unit,
            pi_command=pi_command, environment=env,
        )
        command = [
            "BindPaths=" + str(root) if value.startswith("ReadWritePaths=")
            else "TemporaryFileSystem=" + str(Path.home().resolve()) + ":ro"
            if value == "InaccessiblePaths=" + str(Path.home().resolve())
            else "ProtectHome=tmpfs" if value == "ProtectHome=yes"
            else value for value in command
        ]
        # Reuse native isolation properties, changing only the worker entry point.
        index = command.index(python)
        command[index:] = [python, spec["runtime_path"], "worker", str(spec_path)]
        # Keep the ephemeral app secret out of argv and systemd's unit description.
        env_path = root / "worker.env"
        env_path.write_text("".join(key + "=" + json.dumps(value) + "\n" for key, value in env.items()))
        os.chmod(env_path, 0o600)
        index = command.index("/usr/bin/env")
        command[index:] = [python, spec["runtime_path"], "worker", str(spec_path)]
        command[index:index] = ["-p", "EnvironmentFile=" + str(env_path)]
        command[index:index] = ["-p", "BindReadOnlyPaths=" + str(Path(sys.base_prefix).resolve())]
        # Some installations expose /home and /data00/home as separate mount aliases.
        # Venv and Pi launchers contain absolute links through the original HOME.
        actual_home = Path.home().resolve()
        for value in list(command):
            if value.startswith("BindReadOnlyPaths="):
                path = Path(value.split("=", 1)[1])
                if path.is_relative_to(actual_home) and Path.home() != actual_home:
                    alias = Path.home() / path.relative_to(actual_home)
                    command[index:index] = ["-p", "BindReadOnlyPaths=" + str(path) + ":" + str(alias)]
        # The backend code is readable; production databases and dotenv secrets are not.
        hidden = [source_path, Path(str(source_path) + "-wal"), Path(str(source_path) + "-shm"),
                  backend / ".env", backend.parent / ".env"]
        hidden += [Path.home() / path.relative_to(actual_home) for path in list(hidden)
                   if path.is_relative_to(actual_home) and Path.home() != actual_home]
        for path in hidden:
            if path.exists():
                command[index:index] = ["-p", "InaccessiblePaths=" + str(path)]
        system_scope = spec["config"].get("systemd_scope") == "system"
        control = ["/usr/bin/systemctl", "--user"]
        if system_scope:
            command.remove("--user")
            command[1:1] = ["-p", "User=" + str(os.getuid()), "-p", "Group=" + str(os.getgid())]
            command = ["sudo", "-n", *command]
            control = ["sudo", "-n", "/usr/bin/systemctl"]
        with (root / "sandbox.log").open("wb") as log:
            process = subprocess.Popen(command, env=systemd_launcher_environment(), stdout=log, stderr=log)
        try:
            process.wait(timeout=timeout + 10)
        except subprocess.TimeoutExpired:
            subprocess.run(
                [*control, "stop", unit + ".service"],
                env=systemd_launcher_environment(), capture_output=True, timeout=10, check=False,
            )
            process.wait(timeout=10)
        if process.returncode:
            detail = (root / "sandbox.log").read_text(errors="replace")[-2500:]
            raise RuntimeError("AgentsHub systemd sandbox exited with code " + str(process.returncode) + ": " + detail)


def observed_usage(events):
    """Missing usage is unavailable; never synthesize a zero token bill."""
    calls = [e for e in events if e.event_type == "llm_call_finished"]
    metrics = {"api_calls": len(calls)}
    if not calls:
        return metrics
    values = []
    for event in calls:
        raw = dict(event.payload_json or {})
        usage = raw.get("usage") if isinstance(raw.get("usage"), dict) else raw
        item = {k: v for k, v in usage.items() if type(v) is int and v >= 0}
        if "total_tokens" not in item and "input_tokens" in item and "output_tokens" in item:
            item["total_tokens"] = item["input_tokens"] + item["output_tokens"]
        if "cached_input_tokens" in item:
            item["cache_read_tokens"] = item["cached_input_tokens"]
        values.append(item)
    for key in ("total_tokens", "input_tokens", "output_tokens", "cache_read_tokens",
                "cache_write_tokens", "reasoning_tokens"):
        if all(key in item for item in values):
            metrics[key] = sum(item[key] for item in values)
    return metrics


def install_skills(db, spec):
    from app.agents.branching import (
        ensure_open_gallery_binding,
        ensure_private_resource_binding,
        is_overall_agent,
        mark_resource_open_gallery,
    )
    from app.db.models import AgentResourceBinding, GeneralSkill
    from app.integrations.team_evolver_replay import _install_branch_skill
    from sqlmodel import select

    tenant, agent = spec["identity"]["tenant_id"], spec["identity"]["agent_id"]
    # Install exactly the branch treatment in the disposable database.
    for row in db.exec(select(AgentResourceBinding)).all():
        if row.resource_type in {"skill", "general_skill"}:
            db.delete(row)
    for row in db.exec(select(GeneralSkill)).all():
        db.delete(row)
    db.commit()
    skill = spec.get("skill")
    members = skill.get("skills") if skill and isinstance(skill.get("skills"), list) else [skill] if skill else []
    installed = []
    for member in members:
        payload = copy.deepcopy(member)
        bundle = payload.get("bundle") or {}
        for item in bundle.get("files") or []:
            if item.get("encoding") == "base64":
                try:
                    item["content"] = base64.b64decode(item["content"], validate=True).decode("utf-8")
                except (ValueError, UnicodeError) as exc:
                    raise ValueError("AgentsHub cannot mount a binary Skill file: " + item["path"]) from exc
                item["encoding"] = "utf-8"
            if item["path"] == "SKILL.md":
                payload["content"] = item["content"]
        _install_branch_skill(
            db, tenant_id=tenant, agent_id=agent, target_name=payload["name"], skill=payload,
            GeneralSkill=GeneralSkill, AgentResourceBinding=AgentResourceBinding, select=select,
            mark_resource_open_gallery=mark_resource_open_gallery,
            ensure_open_gallery_binding=ensure_open_gallery_binding,
            ensure_private_resource_binding=ensure_private_resource_binding, is_overall_agent=is_overall_agent,
        )
        installed.append(payload["name"])
    ids = {row.id for row in db.exec(select(GeneralSkill)).all() if row.name in installed}
    for row in db.exec(select(AgentResourceBinding)).all():
        if row.resource_type == "general_skill" and row.resource_id in ids:
            row.status = "active"
            db.add(row)
    db.commit()


def worker(spec):
    from app.integrations import team_evolver_replay as native
    from app.integrations.replay_isolation import ReplayModelSidecar

    native._require_private_network_namespace()
    sidecar = ReplayModelSidecar(
        socket_path=Path(os.environ["AGENTSHUB_REPLAY_MODEL_SOCKET"]),
        port=0, timeout_seconds=spec["timeout_seconds"],
    )
    sidecar.start()
    try:
        native._bind_dynamic_model_sidecar_port(sidecar.port)
        from app.core.harness_runtime import HarnessDescriptor, HarnessRuntime, register_harness
        from app.db import engine, init_db
        from app.db.models import AgentEvent, ChatSession, Message
        from app.general_skills.sandbox import open_session_sandbox
        from app.integrations.team_evolver import _tool_evidence
        from app.observability.event_log import EventLog
        from app.observability.spans import bind_span_sink
        from app.session.session_schema import ChatAttachmentRead, ChatTurnRequest
        from sqlmodel import Session, select

        init_db()
        # Pi starts a Python RPC wrapper before Node. Its native env allowlist
        # omits the isolated DB/dotenv paths required by that Python wrapper.
        # Use the public Harness extension seam only inside this sandbox.
        from app.core.pi_agent import PiAgentRuntime
        class IsolatedPi(PiAgentRuntime):
            def _process_env(self, *args, **kwargs):
                result = super()._process_env(*args, **kwargs)
                for key in ("APP_SECRET", "DATABASE_URL", "PYTHONPATH", "ULTRARAG_DOTENV",
                            "ULTRARAG_DATA_DIR", "TEAM_EVOLVER_SESSION_INGEST_ENABLED",
                            "OPENVIKING_MEMORY_ENABLED", "OPENVIKING_SESSIONS_ENABLED",
                            "VIKING_KNOWLEDGE_ENABLED", "VIKING_MEMORY_ENABLED"):
                    if key in os.environ:
                        result[key] = os.environ[key]
                return result
        register_harness(
            HarnessDescriptor("pi", "Pi Replay", "Isolated Pi runtime", ()),
            IsolatedPi, replace=True,
        )
        who = spec["identity"]
        tenant, user, agent = who["tenant_id"], who.get("user_id"), who["agent_id"]
        session_id = "replay_" + spec["branch"] + "_" + uuid4().hex
        with Session(engine) as db, socket.socket(socket.AF_UNIX) as connection:
            install_skills(db, spec)
            db.add(ChatSession(
                id=session_id, tenant_id=tenant, user_id=user, agent_id=agent,
                environment_id=who.get("environment_id"), harness_id=who["harness_id"],
                harness_config_json=dict(who.get("harness_config") or {}),
            ))
            turns = (spec.get("source_session") or {}).get("turns") or []
            source_num = spec.get("source_turn_num") or 1
            for prior in turns:
                if int(prior.get("turn_num") or 0) >= source_num:
                    break
                for role, key in (("user", "prompt_text"), ("assistant", "response_text")):
                    if prior.get(key):
                        db.add(Message(tenant_id=tenant, session_id=session_id, role=role, content=prior[key]))
            db.commit()
            sandbox = open_session_sandbox(tenant, session_id)
            native._materialize_case_materials(spec["materials"], sandbox.fs_dir)
            source_turn = next((t for t in turns if t.get("turn_num") == source_num), {})
            # Attachments are fixed input content; host file refs are not forwarded.
            attachments = []
            for raw in source_turn.get("attachments") or []:
                if not isinstance(raw, dict):
                    continue
                item = dict(raw)
                if item.get("file_ref") and not (item.get("text") or item.get("data_url")):
                    raise ValueError("Recorded attachment content is unavailable for isolated Replay")
                item.pop("file_ref", None)
                attachments.append(ChatAttachmentRead.model_validate(item))
            loop, event_log = HarnessRuntime(db), EventLog(db)
            def record(event_type, payload):
                event_log.record(tenant, session_id, event_type, payload)
                db.commit()
            connection.connect(str(Path(spec["root"]) / "control.sock"))
            connection.settimeout(spec["timeout_seconds"])
            emit(connection, {"status": "succeeded", "session_id": session_id})
            with connection.makefile("rb") as stream:
                expected = 1
                while True:
                    line = stream.readline(1024 * 1024 + 1)
                    if not line:
                        break
                    try:
                        if len(line) > 1024 * 1024:
                            raise ValueError("Replay turn input exceeds the size limit")
                        request = json.loads(line)
                        if request.get("turn_num") != expected:
                            raise ValueError("Replay turns must execute in sequence")
                        before = {e.id for e in db.exec(select(AgentEvent)).all()}
                        started = time.monotonic()
                        with bind_span_sink(record):
                            response = loop.handle_turn(ChatTurnRequest(
                                tenant_id=tenant, user_id=user, agent_id=agent, session_id=session_id,
                                environment_id=who.get("environment_id"), harness_id=who["harness_id"],
                                harness_config=who.get("harness_config"), model_config_id=who.get("model_config_id"),
                                message=request["prompt"], attachments=attachments if expected == 1 else [],
                                channel="team_evolver_replay", debug=False,
                            ))
                        if native._reply_reports_execution_failure(response.reply):
                            raise RuntimeError("Agent reported execution failure: " + response.reply[:4000])
                        events = [e for e in db.exec(select(AgentEvent).order_by(
                            AgentEvent.created_at, AgentEvent.id,
                        )).all() if e.id not in before]
                        calls, results = _tool_evidence(events)
                        messages = [{"role": "user", "content": request["prompt"]}]
                        by_id = {r.get("tool_call_id"): r for r in results}
                        for call in calls:
                            messages.append({"role": "assistant", "content": "", "tool_calls": [call]})
                            result = by_id.get(call.get("id"))
                            if result:
                                messages.append({
                                    "role": "tool", "tool_call_id": result.get("tool_call_id"),
                                    "name": result.get("tool_name"), "content": result.get("content", ""),
                                })
                        messages.append({"role": "assistant", "content": response.reply})
                        emit(connection, {
                            "status": "succeeded", "turn_num": expected, "response": response.reply,
                            "messages": messages, "artifacts": native._collect_replay_artifacts(sandbox.fs_dir),
                            "metrics": {"tool_call_count": len(calls), **observed_usage(events)},
                            "trace_id": session_id + ":" + str(expected),
                            "elapsed_seconds": round(time.monotonic() - started, 3),
                        })
                        expected += 1
                    except Exception as exc:
                        emit(connection, {"status": "failed", "turn_num": expected, "error": str(exc)})
                        break
    finally:
        sidecar.close()


if __name__ == "__main__":
    spec = json.loads(Path(sys.argv[2]).read_text("utf-8"))
    try:
        (controller if sys.argv[1] == "controller" else worker)(spec)
    except Exception as exc:
        error_path = Path(spec["root"], "startup-error.json")
        if not error_path.exists():
            error_path.write_text(json.dumps({"error": str(exc)}))
        raise
'''
