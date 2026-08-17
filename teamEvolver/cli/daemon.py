"""
Daemon lifecycle commands: start / stop / status.

Handles foreground and background (detached) service startup, health probing,
PID bookkeeping, and graceful shutdown.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

import click

from .. import runtime_state
from ..config_store import ConfigStore


def _default_daemon_log_path() -> Path:
    return Path.home() / ".teamEvolver" / "teamEvolver.log"


def _daemon_child_environment(
    base_env: dict[str, str],
    *,
    home: Path | None = None,
) -> dict[str, str]:
    from dotenv import dotenv_values

    env = dict(base_env)
    env_file = (home or Path.home()) / ".teamEvolver" / "agent-protocol.env"
    if not env_file.is_file():
        return env
    for key, value in dotenv_values(env_file).items():
        if key and value is not None:
            env.setdefault(str(key), str(value))
    return env


def _effective_service_port(config_store: ConfigStore, override_port: int | None) -> int:
    if override_port:
        return override_port
    return int(config_store.get("service.port") or config_store.get("proxy.port") or 30000)


def _is_process_alive(pid: int) -> bool:
    return runtime_state.process_alive(pid)


def _read_pid() -> int | None:
    return runtime_state.read_pid()


def _clear_pid():
    runtime_state.clear_pid()


def _clear_pid_if_matches(pid: int):
    runtime_state.clear_pid_if_matches(pid)


def _healthz_ready(port: int, timeout: float = 0.5) -> bool:
    import urllib.request

    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/healthz", timeout=timeout) as resp:
            if resp.status != 200:
                return False
            payload = json.loads(resp.read().decode("utf-8"))
            return payload.get("ok") is True
    except Exception:
        return False


def _ensure_daemon_not_running():
    pid = _read_pid()
    if pid is None:
        return

    if not _is_process_alive(pid):
        _clear_pid()
        return

    raise click.ClickException(
        f"teamEvolver is already running (PID={pid}). "
        "Use 'teamEvolver status' to inspect it or 'teamEvolver stop' before starting a new daemon."
    )


def _wait_for_daemon_ready(proc, port: int, log_path: Path, timeout_s: float = 15.0):
    import time

    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        returncode = proc.poll()
        if returncode is not None:
            raise click.ClickException(f"teamEvolver daemon exited with code {returncode}. Check logs: {log_path}")
        if _healthz_ready(port):
            return
        time.sleep(0.2)

    raise click.ClickException(f"teamEvolver daemon did not become healthy in time. Check logs: {log_path}")


def _daemon_ready_timeout_seconds(default: float = 15.0) -> float:
    raw = str(os.environ.get("TEAMEVOLVER_DAEMON_READY_TIMEOUT_S", "")).strip()
    if not raw:
        return default
    try:
        value = float(raw)
    except ValueError:
        return default
    return value if value > 0 else default


def _spawn_daemon_process(
    port: int | None,
    log_file: str | None,
    effective_port: int,
) -> tuple[int, Path]:
    import os
    import signal
    import subprocess

    log_path = Path(log_file).expanduser() if log_file else _default_daemon_log_path()
    log_path.parent.mkdir(parents=True, exist_ok=True)

    try:
        with runtime_state.daemon_start_lock():
            _ensure_daemon_not_running()

            cmd = [sys.executable, "-m", "teamEvolver", "start"]
            if port:
                cmd.extend(["--port", str(port)])

            with log_path.open("ab") as log_handle:
                child_env = _daemon_child_environment(dict(os.environ))
                child_env["TEAMEVOLVER_RUNTIME_KIND"] = "daemon"
                child_env["TEAMEVOLVER_RUNTIME_LOG_PATH"] = str(log_path)
                popen_kwargs = {
                    "stdin": subprocess.DEVNULL,
                    "stdout": log_handle,
                    "stderr": subprocess.STDOUT,
                    "close_fds": True,
                    "env": child_env,
                }
                if os.name == "nt":
                    creationflags = getattr(subprocess, "DETACHED_PROCESS", 0) | getattr(
                        subprocess, "CREATE_NEW_PROCESS_GROUP", 0
                    )
                    if creationflags:
                        popen_kwargs["creationflags"] = creationflags
                else:
                    popen_kwargs["start_new_session"] = True
                proc = subprocess.Popen(cmd, **popen_kwargs)

            try:
                _wait_for_daemon_ready(
                    proc,
                    effective_port,
                    log_path,
                    timeout_s=_daemon_ready_timeout_seconds(),
                )
            except Exception:
                try:
                    if proc.poll() is None:
                        if os.name == "nt":
                            proc.terminate()
                        else:
                            os.killpg(proc.pid, signal.SIGTERM)
                        try:
                            proc.wait(timeout=5)
                        except subprocess.TimeoutExpired:
                            if os.name == "nt":
                                proc.kill()
                            else:
                                os.killpg(proc.pid, signal.SIGKILL)
                            proc.wait(timeout=5)
                except Exception:
                    pass

                _clear_pid_if_matches(proc.pid)
                raise

            return proc.pid, log_path
    except RuntimeError as exc:
        owner_pid = exc.args[0] if exc.args else "?"
        raise click.ClickException(
            f"Another 'teamEvolver start --daemon' is already in progress (PID={owner_pid}). "
            "Wait for it to finish or stop that process before retrying."
        ) from None


@click.command()
@click.option(
    "--port",
    type=int,
    default=None,
    help="Override service port for this session.",
)
@click.option(
    "--daemon",
    "-d",
    is_flag=True,
    default=False,
    help="Run teamEvolver in the background.",
)
@click.option(
    "--log-file",
    type=click.Path(dir_okay=False, path_type=str),
    default=None,
    help="Log file used with --daemon (default: ~/.teamEvolver/teamEvolver.log).",
)
def start(port: int | None, daemon: bool, log_file: str | None):
    """Start the teamEvolver service (console + skill sync + optional validation)."""
    import asyncio

    from ..log_color import setup_logging

    setup_logging()

    cs = ConfigStore()
    if not cs.exists():
        click.echo(
            "No config found. Run 'teamEvolver config' first.",
            err=True,
        )
        sys.exit(1)

    if daemon:
        configured_port = _effective_service_port(cs, None)
        # Do not create a temporary config snapshot when the requested port is
        # already the durable value. Internal Agent registration and admin
        # updates must keep writing the real config file in daemon mode.
        daemon_port = port if port and port != configured_port else None
        pid, log_path = _spawn_daemon_process(
            daemon_port,
            log_file,
            effective_port=_effective_service_port(cs, port),
        )
        click.echo(
            f"teamEvolver started in background (PID={pid}). Logs: {log_path}. "
            "Use 'teamEvolver status' to check health and 'teamEvolver stop' to stop it."
        )
        return

    if port:
        import tempfile

        import yaml

        from ..config_store import ConfigStore as _CS

        data = cs.load()
        data.setdefault("service", {})["port"] = port
        tmp = tempfile.NamedTemporaryFile(mode="w", suffix=".yaml", delete=False, encoding="utf-8")
        try:
            yaml.dump(data, tmp)
        finally:
            tmp.close()
        tmp_path = Path(tmp.name)
        cs = _CS(config_file=tmp_path)
    else:
        tmp_path = None

    from ..launcher import Launcher

    launcher = Launcher(cs)
    try:
        asyncio.run(launcher.start())
    except KeyboardInterrupt:
        click.echo("\nInterrupted — stopping teamEvolver.")
        launcher.stop()
    finally:
        if tmp_path is not None:
            tmp_path.unlink(missing_ok=True)


@click.command()
def stop():
    """Stop a running teamEvolver instance."""
    import os
    import signal
    from pathlib import Path

    pid_file = Path.home() / ".teamEvolver" / "teamEvolver.pid"
    if not pid_file.exists():
        click.echo("teamEvolver is not running (no PID file found).")
        return
    try:
        pid = int(pid_file.read_text().strip())
        os.kill(pid, signal.SIGTERM)
        pid_file.unlink(missing_ok=True)
        click.echo(f"Sent SIGTERM to PID {pid}.")
    except ProcessLookupError:
        click.echo("Process not found — cleaning up stale PID file.")
        pid_file.unlink(missing_ok=True)
    except Exception as e:
        click.echo(f"Error stopping teamEvolver: {e}", err=True)


@click.command()
def status():
    """Check whether teamEvolver is running."""
    pid = _read_pid()
    if pid is None:
        click.echo("teamEvolver: not running")
        return

    if not _is_process_alive(pid):
        click.echo("teamEvolver: not running (stale PID file)")
        _clear_pid()
        return

    cs = ConfigStore()
    port = int(cs.get("service.port") or cs.get("proxy.port") or 30000)

    healthy = _healthz_ready(port, timeout=2.0)
    if healthy:
        click.echo(f"teamEvolver: running  (PID={pid}, service=:{port})")
    else:
        click.echo(f"teamEvolver: starting (PID={pid}, service=:{port})")
