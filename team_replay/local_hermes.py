"""Isolated local Hermes sessions. Only execution inputs enter the worker."""
from __future__ import annotations

import base64
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import tempfile
import time
import uuid
from pathlib import Path
from typing import Any, Optional

from ._util import normalize_artifact_rel_path, stable_hash
from .artifacts import (
    materialize_skill_treatment,
    skill_treatment_content,
    skill_treatment_members,
)
from .hooks import AgentObservation, ReplayContext, ReplayUnsupported, RUNTIME_METRICS
from .model_broker import ReplayModelSidecar, replay_model_broker

_REPO_ROOT = Path(__file__).resolve().parents[1]
_SYSTEMD_RUN = "/usr/bin/systemd-run"
_SYSTEMCTL = "/usr/bin/systemctl"
_ENV_BINARY = "/usr/bin/env"

def resolve_hermes_origin() -> Optional[str]:
    """Locate the open-source Hermes agent runtime (nousresearch/hermes-agent).

    True replay imports ``run_agent.AIAgent`` from Hermes. Resolution order:

    1. ``HERMES_ORIGIN`` env var — an explicit local checkout path (for
       developers hacking on Hermes itself). Wins if it holds ``run_agent.py``.
    2. A sibling ``../hermes_origin`` checkout next to this repo, if present.
    3. ``None`` — meaning "rely on an installed ``hermes-agent`` package"
       (``pip install 'teamEvolver[truereplay]'``); the worker imports
       ``run_agent`` straight off ``sys.path`` with no path injection.

    Returning a path means "inject this dir onto sys.path before importing";
    returning ``None`` means "import the installed package as-is"."""
    env = os.environ.get("HERMES_ORIGIN", "").strip()
    if env and (Path(env) / "run_agent.py").exists():
        return env
    sibling = _REPO_ROOT.parent / "hermes_origin"
    if (sibling / "run_agent.py").exists():
        return str(sibling)
    return None


_REPLAY_PYTHON_CACHE: Optional[str] = None


def resolve_replay_python() -> str:
    """Select a Python that can import Hermes without trusting PATH order."""
    global _REPLAY_PYTHON_CACHE
    explicit = str(
        os.environ.get("TEAMEVOLVER_REPLAY_PYTHON") or ""
    ).strip()
    if explicit:
        candidate = Path(explicit).expanduser()
        if not candidate.is_file():
            raise RuntimeError(
                f"TEAMEVOLVER_REPLAY_PYTHON does not exist: {candidate}"
            )
        return str(candidate)
    if _REPLAY_PYTHON_CACHE:
        return _REPLAY_PYTHON_CACHE
    candidates = [
        Path(sys.executable),
        _REPO_ROOT / ".venv" / "bin" / "python",
        Path.home() / "miniconda3" / "bin" / "python3.13",
        Path.home() / "miniconda3" / "bin" / "python",
    ]
    seen: set[str] = set()
    for candidate in candidates:
        resolved = str(candidate.expanduser().resolve())
        if resolved in seen or not candidate.is_file():
            continue
        seen.add(resolved)
        try:
            probe = subprocess.run(
                [
                    resolved,
                    "-c",
                    (
                        "import importlib.util,sys;"
                        "sys.exit(0 if importlib.util.find_spec('run_agent') else 1)"
                    ),
                ],
                capture_output=True,
                timeout=10,
                check=False,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            _REPLAY_PYTHON_CACHE = resolved
            return resolved
    origin = resolve_hermes_origin()
    if origin:
        # A checkout can be injected by the worker, so any functioning Python
        # with the teamEvolver dependencies is sufficient.
        _REPLAY_PYTHON_CACHE = sys.executable
        return sys.executable
    raise RuntimeError(
        "Hermes replay runtime is unavailable; set "
        "TEAMEVOLVER_REPLAY_PYTHON to a Python that can import run_agent"
    )


def read_hermes_harness() -> dict[str, str]:
    """Mirror the user's real Hermes model harness (the replayed agent must be
    consistent with what the client runs). Reads ~/.hermes/config.yaml."""
    cfg_path = Path(os.path.expanduser("~/.hermes/config.yaml"))
    model: dict[str, Any] = {}
    if cfg_path.exists():
        try:
            import yaml

            model = (yaml.safe_load(cfg_path.read_text("utf-8")) or {}).get("model", {}) or {}
        except Exception:
            model = {}
    return {
        "base_url": str(model.get("base_url") or os.getenv("OPENAI_BASE_URL", "https://ark.cn-beijing.volces.com/api/v3")),
        "api_key": str(model.get("api_key") or os.getenv("OPENAI_API_KEY", "")),
        "model": str(model.get("default") or os.getenv("TEAMEVOLVER_REPLAY_MODEL", "doubao-seed-evolving")),
        "api_mode": str(model.get("api_mode") or ""),
        "max_tokens": int(model.get("max_tokens") or 100000),
    }



def _write_sandbox_harness_config(
    hermes_home: Path,
    harness: dict[str, Any],
) -> None:
    config = {
        "model": {
            "provider": "custom",
            "base_url": harness["base_url"],
            "default": harness["model"],
            "api_key": harness["api_key"],
            "max_tokens": harness["max_tokens"],
            "api_mode": harness["api_mode"],
        }
    }
    config_path = hermes_home / "config.yaml"
    try:
        import yaml

        config_path.write_text(
            yaml.safe_dump(config, sort_keys=False),
            "utf-8",
        )
    except Exception:
        config_path.write_text(json.dumps(config), "utf-8")
    os.chmod(config_path, 0o600)


def build_sandbox(
    base: Path,
    branch: str,
    harness: dict[str, str],
    skill: Optional[dict[str, Any]],
    materials: Optional[list[dict[str, Any]]] = None,
) -> dict[str, str]:
    """Create an isolated HOME and install that branch's peer Skill set."""
    home = base / branch
    hermes_home = home / ".hermes"
    workspace = home / "workspace"
    for d in (hermes_home / "skills", hermes_home / "sessions", hermes_home / "logs", workspace):
        d.mkdir(parents=True, exist_ok=True)
    os.chmod(home, 0o700)
    os.chmod(hermes_home, 0o700)
    os.chmod(workspace, 0o700)

    _write_sandbox_harness_config(hermes_home, harness)

    installed_trees: dict[str, str] = {}
    for member in skill_treatment_members(skill):
        name = str(member.get("name") or "")
        sk_dir = hermes_home / "skills" / name
        installed_trees[name] = materialize_skill_treatment(member, sk_dir)
    installed_tree = (
        next(iter(installed_trees.values()))
        if len(installed_trees) == 1
        else stable_hash(installed_trees)
        if installed_trees
        else ""
    )

    for item in materials or []:
        rel_path = normalize_artifact_rel_path(str(item.get("path") or ""))
        try:
            data = base64.b64decode(
                str(item.get("content_b64") or ""),
                validate=True,
            )
        except (ValueError, TypeError) as exc:
            raise ValueError(f"invalid replay material: {rel_path}") from exc
        target = workspace / Path(rel_path)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)

    return {
        "home": str(home),
        "hermes_home": str(hermes_home),
        "workspace": str(workspace),
        "skill_tree_sha256": installed_tree,
    }


# ---------------------------------------------------------------------------
# Worker: run ONE branch in an isolated subprocess and dump its trajectory.
# ---------------------------------------------------------------------------


def _workspace_evidence(workspace: str, *, max_files: int = 40) -> list[dict[str, Any]]:
    root = Path(workspace)
    evidence: list[dict[str, Any]] = []
    if not root.is_dir():
        return evidence
    for path in sorted(root.rglob("*")):
        if path.is_symlink() or not path.is_file() or len(evidence) >= max_files:
            continue
        try:
            with path.open("rb") as stream:
                data = stream.read(32_000)
        except OSError:
            continue
        item: dict[str, Any] = {
            "path": path.relative_to(root).as_posix(),
            "size": path.stat().st_size,
        }
        try:
            item["text_preview"] = data[:32_000].decode("utf-8")
        except UnicodeDecodeError:
            item["binary"] = True
        evidence.append(item)
    return evidence



def _path_is_within(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def _sandbox_read_paths(
    worker_python: str,
    case: Optional[dict[str, Any]],
) -> list[Path]:
    """Return explicit host-home paths that the hidden-home worker may read."""
    home = Path.home().resolve()
    candidates = [
        Path(worker_python).resolve().parents[1],
    ]
    origin = resolve_hermes_origin()
    if origin:
        candidates.append(Path(origin).resolve())
    for item in (case or {}).get("referenced_paths") or []:
        if not isinstance(item, dict):
            continue
        resolved = str(item.get("resolved") or "").strip()
        if not resolved or resolved.startswith("uploaded://"):
            continue
        path = Path(resolved).resolve()
        if path.exists():
            candidates.append(path)
    unique: list[Path] = []
    for path in candidates:
        if path.exists() and _path_is_within(path, home) and path not in unique:
            unique.append(path)
    return unique


def _worker_environment(
    worker_python: str,
    sandbox: dict[str, str],
    *,
    broker_socket_path: Optional[Path] = None,
    broker_sidecar_port: int = 0,
    timeout: int = 0,
) -> dict[str, str]:
    tmp_dir = Path(sandbox["home"]) / "tmp"
    tmp_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(tmp_dir, 0o700)
    python_bin = str(Path(worker_python).resolve().parent)
    path = ":".join(
        dict.fromkeys(
            [
                python_bin,
                "/usr/local/sbin",
                "/usr/local/bin",
                "/usr/sbin",
                "/usr/bin",
                "/sbin",
                "/bin",
            ]
        )
    )
    env = {
        "HOME": sandbox["home"],
        "HERMES_HOME": sandbox["hermes_home"],
        "LANG": str(os.environ.get("LANG") or "C.UTF-8"),
        "LC_ALL": str(os.environ.get("LC_ALL") or ""),
        "PATH": path,
        "PYTHONDONTWRITEBYTECODE": "1",
        "PYTHONPATH": str(Path(sandbox["home"]) / ".runtime"),
        "TEAMEVOLVER_REPLAY_HOST_NETNS_INODE": str(
            os.stat("/proc/self/ns/net").st_ino
        ),
        "TMPDIR": str(tmp_dir),
    }
    if broker_socket_path is not None:
        env.update(
            {
                "TEAMEVOLVER_REPLAY_MODEL_SOCKET": str(
                    broker_socket_path
                ),
                "TEAMEVOLVER_REPLAY_MODEL_SIDECAR_PORT": str(
                    broker_sidecar_port
                ),
                "TEAMEVOLVER_REPLAY_MODEL_TIMEOUT": str(timeout),
            }
        )
    for key in ("SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE"):
        value = str(os.environ.get(key) or "").strip()
        if value and Path(value).exists():
            env[key] = value
    return {key: value for key, value in env.items() if value}


def _require_private_network_namespace() -> None:
    """Fail closed when systemd ignored PrivateNetwork on this host."""
    try:
        host_inode = int(
            os.environ["TEAMEVOLVER_REPLAY_HOST_NETNS_INODE"]
        )
        worker_inode = int(os.stat("/proc/self/ns/net").st_ino)
    except (KeyError, OSError, TypeError, ValueError) as exc:
        raise RuntimeError(
            "cannot verify replay network namespace"
        ) from exc
    if worker_inode == host_inode:
        raise RuntimeError(
            "PrivateNetwork isolation is unavailable on this host"
        )


def _systemd_sandbox_command(
    *,
    worker_python: str,
    spec_path: Path,
    sandbox: dict[str, str],
    harness: dict[str, str],
    timeout: int,
    case: Optional[dict[str, Any]],
    unit_name: str,
    broker_socket_path: Optional[Path] = None,
    broker_sidecar_port: int = 0,
) -> list[str]:
    del harness
    for executable in (_SYSTEMD_RUN, _SYSTEMCTL, _ENV_BINARY):
        if not Path(executable).is_file():
            raise RuntimeError(f"required sandbox executable is missing: {executable}")
    sandbox_home = Path(sandbox["home"]).resolve()
    actual_home = Path.home().resolve()
    command = [
        _SYSTEMD_RUN,
        "--user",
        "--wait",
        "--pipe",
        "--quiet",
        "--collect",
        f"--unit={unit_name}",
        f"--working-directory={sandbox_home}",
        "-p",
        "Type=exec",
        "-p",
        "NoNewPrivileges=yes",
        "-p",
        "ProtectSystem=strict",
        "-p",
        "ProtectHome=yes",
        "-p",
        "PrivateNetwork=yes",
        "-p",
        "PrivateTmp=yes",
        "-p",
        "ProtectProc=invisible",
        "-p",
        "ProcSubset=pid",
        "-p",
        f"InaccessiblePaths={actual_home}",
        "-p",
        f"ReadWritePaths={sandbox_home}",
        "-p",
        "RestrictSUIDSGID=yes",
        "-p",
        "LockPersonality=yes",
        "-p",
        "RestrictNamespaces=yes",
        "-p",
        "RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6",
        "-p",
        "SystemCallArchitectures=native",
        "-p",
        "UMask=0077",
        "-p",
        f"RuntimeMaxSec={max(1, int(timeout))}s",
        "-p",
        "TimeoutStopSec=5s",
        "-p",
        "KillMode=mixed",
    ]
    for path in _sandbox_read_paths(worker_python, case):
        command.extend(("-p", f"BindReadOnlyPaths={path}"))
    command.append(_ENV_BINARY)
    command.append("-i")
    command.extend(
        f"{key}={value}"
        for key, value in _worker_environment(
            worker_python,
            sandbox,
            broker_socket_path=broker_socket_path,
            broker_sidecar_port=broker_sidecar_port,
            timeout=timeout,
        ).items()
    )
    command.extend(
        (
            worker_python,
            "-m",
            "team_replay.local_hermes",
            "--worker",
            "--spec",
            str(spec_path),
        )
    )
    return command


def _systemd_launcher_environment() -> dict[str, str]:
    env = {"PATH": "/usr/bin:/bin"}
    for key in ("DBUS_SESSION_BUS_ADDRESS", "XDG_RUNTIME_DIR"):
        value = str(os.environ.get(key) or "").strip()
        if value:
            env[key] = value
    return env



def _write_json(path: Path, payload: Any) -> None:
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(payload, ensure_ascii=False), "utf-8")
    os.chmod(temporary, 0o600)
    os.replace(temporary, path)


class LocalHermesFactory:
    def __init__(self, harness: dict[str, Any]):
        self.harness = dict(harness)
        self.python = resolve_replay_python()
        self.origin = resolve_hermes_origin() or ""

    def open(self, context: ReplayContext):
        return LocalHermesSession(context, self.harness, self.python, self.origin)


class LocalHermesSession:
    def __init__(self, context, harness, worker_python, origin):
        self.context = context
        self.base = Path(tempfile.mkdtemp(prefix="replay-hermes-"))
        self.process = None
        self.broker_scope = None
        self.broker_started = False
        self.closed = False
        self.turn = 0
        self.deadline = time.monotonic() + context.timeout_seconds
        self.unit = "teamevolver-replay-" + uuid.uuid4().hex
        try:
            self.sandbox = build_sandbox(self.base, context.treatment.branch, harness,
                                         context.treatment.skill, list(context.materials))
            self.home = Path(self.sandbox["home"])
            # Do not expose host jobs, judge prompts, configuration or source datasets to tools.
            runtime = self.home / ".runtime" / "team_replay"
            runtime.mkdir(parents=True)
            (runtime / "__init__.py").write_text("")
            for name in ("local_hermes.py", "model_broker.py", "hooks.py", "artifacts.py", "_util.py"):
                shutil.copyfile(Path(__file__).parent / name, runtime / name)
            broker_token = secrets.token_urlsafe(32)
            socket = self.home / ".model-broker.sock"
            self.broker_scope = replay_model_broker(
                base_url=harness["base_url"], api_key=harness["api_key"], socket_path=socket,
                token=broker_token, timeout_seconds=context.timeout_seconds,
            )
            broker = self.broker_scope.__enter__()
            self.broker_started = True
            worker_harness = {**harness, "base_url": broker.worker_base_url(0), "api_key": broker_token}
            spec = {**self.sandbox, "harness": worker_harness, "hermes_origin": origin,
                    "skill_content": skill_treatment_content(context.treatment.skill),
                    "context_snapshot": context.context_snapshot, "timeout": context.timeout_seconds}
            spec_path = self.home / ".replay_spec.json"
            _write_json(spec_path, spec)
            command = _systemd_sandbox_command(
                worker_python=worker_python, spec_path=spec_path, sandbox=self.sandbox,
                harness=worker_harness, timeout=context.timeout_seconds, case=None,
                unit_name=self.unit, broker_socket_path=socket,
            )
            self.error_log = (self.home / ".worker.log").open("w")
            self.process = subprocess.Popen(command, cwd=str(self.home), env=_systemd_launcher_environment(),
                                            stdout=subprocess.DEVNULL, stderr=self.error_log)
        except Exception:
            self.close()
            raise

    def send(self, user_message):
        if self.closed:
            raise RuntimeError("Session is closed")
        self.turn += 1
        _write_json(self.home / f".request-{self.turn}.json", {"message": user_message})
        output = self.home / f".response-{self.turn}.json"
        while not output.exists():
            if self.process.poll() is not None:
                raise ReplayUnsupported("isolated Hermes worker unavailable; check systemd and installed runtime")
            if time.monotonic() >= self.deadline:
                raise TimeoutError("Hermes branch timed out")
            time.sleep(0.05)
        result = json.loads(output.read_text("utf-8"))
        if result.get("error"):
            raise ReplayUnsupported(result["error"])
        return AgentObservation(result["response"], tuple(result.get("messages") or []),
                                tuple(result.get("artifacts") or []), result.get("metrics") or {})

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.process is not None:
            try:
                subprocess.run([_SYSTEMCTL, "--user", "stop", self.unit + ".service"],
                               env=_systemd_launcher_environment(), stdout=subprocess.DEVNULL,
                               stderr=subprocess.DEVNULL, timeout=10, check=False)
            finally:
                if self.process.poll() is None:
                    self.process.kill()
                self.process.wait(timeout=5)
        if getattr(self, "error_log", None):
            self.error_log.close()
        if self.broker_started:
            self.broker_scope.__exit__(None, None, None)
        shutil.rmtree(self.base, ignore_errors=True)


def _run_worker(spec_path):
    """A continuous customer session. Judge state is never serialized here."""
    import inspect

    spec = json.loads(Path(spec_path).read_text("utf-8"))
    _require_private_network_namespace()
    os.environ.update(HOME=spec["home"], HERMES_HOME=spec["hermes_home"], TERMINAL_ENV="local", HERMES_YOLO_MODE="1")
    os.chdir(spec["workspace"])
    home = Path(spec["home"])
    sidecar = ReplayModelSidecar(socket_path=Path(os.environ["TEAMEVOLVER_REPLAY_MODEL_SOCKET"]),
                                port=0, timeout_seconds=spec["timeout"])
    turn = 1
    try:
        sidecar.start()
        harness = spec["harness"]
        harness["base_url"] = re.sub(r"^http://127\.0\.0\.1:0/", f"http://127.0.0.1:{sidecar.port}/", harness["base_url"])
        _write_sandbox_harness_config(Path(spec["hermes_home"]), harness)
        if spec["hermes_origin"]:
            sys.path.insert(0, spec["hermes_origin"])
        from run_agent import AIAgent
        prompt = ""
        if spec["skill_content"]:
            prompt += "Installed skill guidance:\n" + spec["skill_content"]
        if spec["context_snapshot"]:
            prompt += "\nFixed context:\n" + json.dumps(spec["context_snapshot"], ensure_ascii=False)
        agent = AIAgent(base_url=harness["base_url"], api_key=harness["api_key"], model=harness["model"],
                        max_iterations=25, enabled_toolsets=["terminal", "file"], skip_context_files=True,
                        skip_memory=True, save_trajectories=False, quiet_mode=True, ephemeral_system_prompt=prompt)
        parameters = inspect.signature(agent.run_conversation).parameters
        if "conversation_history" not in parameters:
            raise ReplayUnsupported("Installed Hermes must support conversation_history for continuous Replay")
        history = []
        deadline = time.monotonic() + spec["timeout"]
        while time.monotonic() < deadline:
            request = home / f".request-{turn}.json"
            if not request.exists():
                time.sleep(0.05)
                continue
            message = json.loads(request.read_text())["message"]
            result = agent.run_conversation(message, conversation_history=history)
            messages = result.get("messages") or []
            previous = len(history) if messages[:len(history)] == history else 0
            round_messages = messages[previous:]
            history = messages if previous else history + messages
            _write_json(home / f".response-{turn}.json", {
                "response": result.get("final_response", ""), "messages": round_messages,
                "artifacts": _workspace_evidence(spec["workspace"]),
                "metrics": {key: result[key] for key in RUNTIME_METRICS if isinstance(result.get(key), (int, float))},
            })
            turn += 1
    except Exception as exc:
        _write_json(home / f".response-{turn}.json", {"error": str(exc)})
    finally:
        sidecar.close()


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser()
    parser.add_argument("--worker", action="store_true")
    parser.add_argument("--spec", required=True)
    _run_worker(parser.parse_args().spec)
