"""DreamCycle lifecycle and configuration bridge for teamEvolver."""

from __future__ import annotations

import base64
import json
import logging
import os
import shlex
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

_STATE_DIR = Path.home() / ".teamEvolver" / "dreamcycle"


def parse_openviking_key(value: str) -> tuple[str, str]:
    """Return the account and user encoded in an OpenViking API key."""
    parts = str(value or "").strip().split(".")
    if len(parts) < 3:
        return "", ""
    decoded: list[str] = []
    for part in parts[:2]:
        try:
            padding = "=" * (-len(part) % 4)
            decoded.append(base64.b64decode(part + padding).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return "", ""
    return decoded[0].strip(), decoded[1].strip()


def build_dreamcycle_env(config: Any) -> tuple[dict[str, str], list[str]]:
    """Build DreamCycle env for one authenticated user's maintained memory."""
    api_key = str(
        getattr(config, "sharing_viking_team_api_key", "")
        or getattr(config, "sharing_viking_api_key", "")
        or ""
    ).strip()
    key_account, key_user = parse_openviking_key(api_key)
    endpoint = str(
        getattr(config, "sharing_viking_endpoint", "")
        or ""
    ).strip()
    account = str(
        key_account or getattr(config, "sharing_viking_account", "") or ""
    ).strip()
    agent_id = str(
        key_user or getattr(config, "sharing_viking_user", "") or ""
    ).strip()
    source_api_keys = collect_personal_source_keys(config)
    source_users = collect_personal_source_users(config)
    llm_base_url = str(
        getattr(config, "dreamcycle_llm_base_url", "")
        or getattr(config, "llm_api_base", "")
        or ""
    ).strip()
    llm_api_key = str(
        getattr(config, "dreamcycle_llm_api_key", "")
        or getattr(config, "llm_api_key", "")
        or ""
    ).strip()
    llm_model = str(
        getattr(config, "dreamcycle_llm_model", "")
        or getattr(config, "llm_model_id", "")
        or getattr(config, "model_name", "")
        or ""
    ).strip()

    missing: list[str] = []
    for name, value in (
        ("sharing.viking_endpoint", endpoint),
        ("sharing.viking_team_api_key", api_key),
        ("sharing.viking_team_api_key.user", agent_id),
        ("dreamcycle.llm_api_key", llm_api_key),
        ("dreamcycle.llm_model", llm_model),
    ):
        if not value:
            missing.append(name)

    env = os.environ.copy()
    env.update(
        {
            "OPENVIKING_ENDPOINT": endpoint,
            "OPENVIKING_API_KEY": api_key,
            "OPENVIKING_ACCOUNT": account,
            "OPENVIKING_AGENT_ID": agent_id,
            "OPENVIKING_SOURCE_API_KEYS": json.dumps(source_api_keys),
            "OPENVIKING_SOURCE_USERS": json.dumps(source_users),
            "OPENVIKING_CUSTOMER_ID": str(
                getattr(config, "dreamcycle_customer_id", "") or ""
            ),
            "OPENVIKING_AGENT": str(
                getattr(config, "dreamcycle_viking_agent", "") or "dreamcycle"
            ),
            "DREAMCYCLE_LLM_BASE_URL": llm_base_url,
            "DREAMCYCLE_LLM_API_KEY": llm_api_key,
            "DREAMCYCLE_MODEL": llm_model,
            "DREAMCYCLE_TEAM_NAME": str(
                getattr(config, "team_display_name", "") or "Team"
            ).strip(),
            "DREAMCYCLE_EMBED_MODEL": str(
                getattr(config, "dreamcycle_embed_model", "") or ""
            ),
            "DREAMCYCLE_EMBED_BASE_URL": str(
                getattr(config, "dreamcycle_embed_base_url", "") or ""
            ),
            "DREAMCYCLE_EMBED_API_KEY": str(
                getattr(config, "dreamcycle_embed_api_key", "") or ""
            ),
            "DREAMCYCLE_DEDUP_MERGE_THRESHOLD": str(
                getattr(
                    config,
                    "dreamcycle_dedup_merge_threshold",
                    0.86,
                )
            ),
            "DREAMCYCLE_DEDUP_WARN_THRESHOLD": str(
                getattr(
                    config,
                    "dreamcycle_dedup_warn_threshold",
                    0.72,
                )
            ),
        }
    )
    return env, missing


def collect_personal_source_keys(config: Any) -> list[str]:
    """Collect configured personal keys without assuming users or machines."""
    candidates: list[str] = []
    configured = getattr(config, "sharing_viking_personal_api_keys", []) or []
    if isinstance(configured, (list, tuple, set)):
        candidates.extend(str(item or "") for item in configured)
    elif configured:
        candidates.extend(str(configured).replace("\n", ",").split(","))
    candidates.append(
        str(getattr(config, "sharing_viking_personal_api_key", "") or "")
    )

    registry_path = str(getattr(config, "users_registry_path", "") or "").strip()
    path = (
        Path(registry_path).expanduser()
        if registry_path
        else Path.home() / ".teamEvolver" / "users.json"
    )
    try:
        registry = json.loads(path.read_text("utf-8"))
        for user in registry.get("users") or []:
            if not isinstance(user, dict):
                continue
            personal = user.get("personal_space")
            if isinstance(personal, dict):
                candidates.append(str(personal.get("viking_api_key") or ""))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        pass

    team_key = str(
        getattr(config, "sharing_viking_team_api_key", "")
        or getattr(config, "sharing_viking_api_key", "")
        or ""
    ).strip()
    return list(
        dict.fromkeys(
            key
            for raw in candidates
            if (key := str(raw or "").strip()) and key != team_key
        )
    )


def collect_personal_source_users(config: Any) -> list[str]:
    """Collect explicit personal namespaces for local/root-key deployments."""
    candidates = [
        str(getattr(config, "sharing_viking_personal_user", "") or "")
    ]
    registry_path = str(getattr(config, "users_registry_path", "") or "").strip()
    path = (
        Path(registry_path).expanduser()
        if registry_path
        else Path.home() / ".teamEvolver" / "users.json"
    )
    try:
        registry = json.loads(path.read_text("utf-8"))
        for user in registry.get("users") or []:
            if not isinstance(user, dict):
                continue
            personal = user.get("personal_space")
            if isinstance(personal, dict):
                candidates.append(str(personal.get("viking_user") or ""))
    except (FileNotFoundError, OSError, TypeError, ValueError):
        pass
    team_user = str(getattr(config, "sharing_viking_user", "") or "").strip()
    return list(
        dict.fromkeys(
            user
            for raw in candidates
            if (user := str(raw or "").strip()) and user != team_user
        )
    )


def _resolve_command(command: str) -> list[str]:
    argv = shlex.split(str(command or ""))
    if not argv:
        return []
    resolved = shutil.which(argv[0])
    if resolved:
        argv[0] = resolved
    elif os.path.basename(argv[0]) == "dreamcycle":
        python = _resolve_dreamcycle_python()
        argv = [python, "-m", "dreamcycle", *argv[1:]]
    return argv


def _resolve_dreamcycle_python() -> str:
    candidates = [
        str(os.environ.get("DREAMCYCLE_PYTHON") or "").strip(),
        sys.executable,
        str(Path.home() / "miniconda3" / "bin" / "python"),
        str(Path.home() / ".venv" / "bin" / "python"),
    ]
    for candidate in dict.fromkeys(item for item in candidates if item):
        if not os.path.isfile(candidate) or not os.access(candidate, os.X_OK):
            continue
        try:
            probe = subprocess.run(
                [candidate, "-c", "import dreamcycle"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=5,
                check=False,
            )
        except (OSError, subprocess.TimeoutExpired):
            continue
        if probe.returncode == 0:
            return candidate
    return sys.executable


def _process_alive(pid: int | None) -> bool:
    if not pid:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _parent_pid(pid: int | None) -> int | None:
    if not pid:
        return None
    try:
        stat = Path(f"/proc/{pid}/stat").read_text("utf-8")
        return int(stat.rsplit(")", 1)[1].split()[1])
    except (FileNotFoundError, OSError, ValueError, IndexError):
        return None


def _read_pid(path: Path) -> int | None:
    try:
        return int(path.read_text("utf-8").strip() or "0") or None
    except (FileNotFoundError, OSError, ValueError):
        return None


def _unlink_stale_pid(path: Path) -> None:
    try:
        path.unlink(missing_ok=True)
    except OSError:
        logger.debug("[DreamCycle] could not remove stale pid file %s", path)


class DreamCycleSupervisor:
    """Own the optional daemon and one-shot maintenance processes."""

    def __init__(self, config: Any):
        self.config = config
        self._daemon_process: subprocess.Popen[bytes] | None = None
        self._trigger_process: subprocess.Popen[bytes] | None = None
        self._daemon_pid_file = _STATE_DIR / "daemon.pid"
        self._trigger_pid_file = _STATE_DIR / "trigger.pid"
        self._daemon_log = _STATE_DIR / "daemon.log"
        self._trigger_log = _STATE_DIR / "trigger.log"

    def _enabled(self) -> bool:
        return bool(getattr(self.config, "dreamcycle_enabled", True))

    def start(self) -> dict[str, Any]:
        if not self._enabled() or not bool(
            getattr(self.config, "dreamcycle_auto_start", True)
        ):
            return self.status()
        env, missing = build_dreamcycle_env(self.config)
        if missing:
            logger.warning(
                "[DreamCycle] auto-start skipped; missing config: %s",
                ", ".join(missing),
            )
            return self.status()
        existing = _read_pid(self._daemon_pid_file)
        if _process_alive(existing) and _parent_pid(existing) not in {
            None,
            1,
            os.getpid(),
        }:
            # During a teamEvolver restart the previous daemon may still be in
            # its graceful shutdown window. Wait for it instead of adopting a
            # process that is about to disappear.
            for _ in range(20):
                time.sleep(0.5)
                if not _process_alive(existing):
                    existing = None
                    break
        if _process_alive(existing):
            logger.info("[DreamCycle] using existing daemon pid=%s", existing)
            return self.status()
        command = str(
            getattr(self.config, "dreamcycle_daemon_command", "")
            or "dreamcycle --daemon"
        )
        self._daemon_process = self._spawn(
            command,
            env=env,
            pid_file=self._daemon_pid_file,
            log_path=self._daemon_log,
        )
        logger.info("[DreamCycle] daemon started pid=%s", self._daemon_process.pid)
        return self.status()

    def trigger(self) -> dict[str, Any]:
        if not self._enabled():
            return {"status": "disabled", **self.status()}
        env, missing = build_dreamcycle_env(self.config)
        if missing:
            return {"status": "not_configured", "missing": missing, **self.status()}
        existing = _read_pid(self._trigger_pid_file)
        if _process_alive(existing):
            return {"status": "already_running", **self.status()}
        command = str(
            getattr(self.config, "dreamcycle_trigger_command", "")
            or "dreamcycle --once"
        )
        self._trigger_process = self._spawn(
            command,
            env=env,
            pid_file=self._trigger_pid_file,
            log_path=self._trigger_log,
        )
        return {"status": "started", **self.status()}

    def status(self) -> dict[str, Any]:
        _, missing = build_dreamcycle_env(self.config)
        daemon_pid = _read_pid(self._daemon_pid_file)
        trigger_pid = _read_pid(self._trigger_pid_file)
        daemon_running = _process_alive(daemon_pid)
        trigger_running = _process_alive(trigger_pid)
        if daemon_pid and not daemon_running:
            _unlink_stale_pid(self._daemon_pid_file)
            daemon_pid = None
        if trigger_pid and not trigger_running:
            _unlink_stale_pid(self._trigger_pid_file)
            trigger_pid = None
        return {
            "enabled": self._enabled(),
            "configured": not missing,
            "missing": missing,
            # Keep compatibility with AgentsHub: running means an immediate
            # maintenance round is active, not that the overnight daemon exists.
            "running": trigger_running,
            "pid": trigger_pid,
            "daemon_running": daemon_running,
            "daemon_pid": daemon_pid,
            "log_file": str(self._trigger_log),
            "daemon_log_file": str(self._daemon_log),
        }

    def stop(self) -> None:
        process = self._daemon_process
        if process is None or process.poll() is not None:
            return
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        _unlink_stale_pid(self._daemon_pid_file)

    @staticmethod
    def _spawn(
        command: str,
        *,
        env: dict[str, str],
        pid_file: Path,
        log_path: Path,
    ) -> subprocess.Popen[bytes]:
        argv = _resolve_command(command)
        if not argv:
            raise ValueError("DreamCycle command is empty")
        _STATE_DIR.mkdir(parents=True, exist_ok=True)
        # DreamCycle otherwise auto-loads ~/.dreamcycle/.env with override=True,
        # which could silently replace the identity injected above.
        if "--env-file" not in argv:
            managed_env = _STATE_DIR / "managed.env"
            managed_env.touch(exist_ok=True)
            argv.extend(["--env-file", str(managed_env)])
        log_handle = open(log_path, "ab", buffering=0)
        try:
            process = subprocess.Popen(
                argv,
                env=env,
                stdout=log_handle,
                stderr=subprocess.STDOUT,
                start_new_session=True,
            )
        finally:
            log_handle.close()
        pid_file.write_text(f"{process.pid}\n", encoding="utf-8")
        return process
