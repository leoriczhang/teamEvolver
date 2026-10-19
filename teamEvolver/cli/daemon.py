"""Start only the application composition root with an explicit runtime role."""
from __future__ import annotations

import os
import signal
import subprocess
import sys
from pathlib import Path

import click

from .. import runtime_state
from ..bootstrap import load_bootstrap


@click.command()
@click.option("--role", type=click.Choice(["api", "worker", "all"]), default=None)
@click.option("--port", type=click.IntRange(1, 65535), default=None)
@click.option("--daemon", "-d", is_flag=True)
@click.option("--log-file", type=click.Path(dir_okay=False), default=None)
def start(role, port, daemon, log_file):
    """Run API or Worker; all is for local development."""
    from ..logging_runtime import configure, register_secrets, shutdown
    from ..runtime.entrypoint import run_role

    config = load_bootstrap()
    role = role or os.environ.get("TEAMEVOLVER_ROLE", "api")
    if role not in {"api", "worker", "all"}:
        raise click.ClickException("TEAMEVOLVER_ROLE must be api, worker or all")
    if role == "all" and os.environ.get("TEAMEVOLVER_ENV", "local") != "local":
        raise click.ClickException("all runtime is restricted to local development")
    if not config.storage_pg_dsn:
        raise click.ClickException("runtime requires PostgreSQL; configure TEAMEVOLVER_PG_DSN")
    if daemon:
        with runtime_state.daemon_start_lock():
            pid = runtime_state.read_pid()
            if pid and runtime_state.process_alive(pid):
                raise click.ClickException(f"teamEvolver is already running (PID={pid})")
            log_path = Path(log_file or "/tmp/te_runtime.log")
            command = [sys.executable, "-m", "teamEvolver", "start", "--role", role]
            if port:
                command += ["--port", str(port)]
            if log_file:
                command += ["--log-file", str(log_file)]
            with log_path.open("ab") as output:
                proc = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=output,
                                        stderr=output, start_new_session=True)
            state = runtime_state.pid_file_path()
            state.parent.mkdir(parents=True, exist_ok=True)
            state.write_text(str(proc.pid))
            click.echo(f"Started {role} (PID={proc.pid}); logs: {log_path}")
        return
    from dataclasses import asdict
    register_secrets(asdict(config))
    configure(config.logging, log_file=log_file)
    try:
        run_role(role=role, config=config,
                 api_port=port if role != "worker" else None,
                 worker_health_port=port if role == "worker" else None)
    finally:
        runtime_state.clear_pid_if_matches(os.getpid())
        shutdown()


@click.command()
def stop():
    """Signal the recorded runtime PID."""
    pid = runtime_state.read_pid()
    if not pid:
        click.echo("teamEvolver is not running")
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    runtime_state.clear_pid_if_matches(pid)
    click.echo(f"Stopped PID {pid}")


@click.command()
def status():
    """Inspect the recorded runtime process."""
    pid = runtime_state.read_pid()
    click.echo(f"teamEvolver: running (PID={pid})" if pid else "teamEvolver: not running")
