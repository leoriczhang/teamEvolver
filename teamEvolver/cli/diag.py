"""
Diagnostics & maintenance commands.

Groups:
- doctor      — inspect local integration state (Hermes).
- restore     — restore agent integration config from backups.
- validation  — background validation status / one-shot run.
"""

from __future__ import annotations

from pathlib import Path

import click

from ..config_store import ConfigStore


def _echo_report(report: dict[str, object]) -> None:
    ordered_keys = [
        "status",
        "integration_scope",
        "config_path",
        "config_exists",
        "expected_model",
        "configured_model",
        "expected_base_url",
        "configured_base_url",
        "configured_provider",
        "proxy_match",
        "expected_skills_dir",
        "skills_dir_exists",
        "skills_dir_mode",
        "legacy_skills_dir",
        "legacy_skills_present",
        "latest_backup",
        "session_boundary_mode",
    ]
    list_keys = {"issues", "notes", "next_steps"}
    emitted: set[str] = set()

    for key in ordered_keys:
        if key not in report:
            continue
        click.echo(f"{key}: {report[key]}")
        emitted.add(key)

    for key, value in report.items():
        if key in emitted or key in list_keys:
            continue
        click.echo(f"{key}: {value}")

    for key in ("issues", "notes", "next_steps"):
        value = report.get(key)
        if not isinstance(value, list):
            continue
        click.echo(f"{key}:")
        if not value:
            click.echo("  (none)")
            continue
        for item in value:
            click.echo(f"  - {item}")


@click.group()
def doctor():
    """Integration diagnostics."""


@doctor.command(name="hermes")
def doctor_hermes():
    """Inspect the local Hermes integration state."""
    from ..integrations import inspect_hermes_config

    cs = ConfigStore()
    if not cs.exists():
        raise click.ClickException("No config file found. Run 'teamEvolver config' first.")

    report = inspect_hermes_config(cs.to_config())
    _echo_report(report)


@click.group()
def restore():
    """Restore agent integration state from backups."""


@restore.command(name="hermes")
@click.option(
    "--backup",
    "backup_path",
    type=click.Path(exists=True, dir_okay=False, path_type=str),
    default=None,
    help="Restore from a specific backup file instead of the latest Hermes backup.",
)
def restore_hermes(backup_path: str | None):
    """Restore ~/.hermes/config.yaml from a saved backup."""
    from ..integrations import restore_hermes_config

    try:
        result = restore_hermes_config(Path(backup_path).expanduser() if backup_path else None)
    except FileNotFoundError as exc:
        raise click.ClickException(str(exc)) from None

    click.echo(f"Restored Hermes config: {result['target']} <- {result['source']}")


@click.group()
def validation():
    """Background validation commands."""


@validation.command(name="status")
def validation_status():
    """Show background validation configuration and current availability."""
    from ..validation import ValidationWorker

    cs = ConfigStore()
    cfg = cs.to_config()
    worker = ValidationWorker(cfg)
    snapshot = worker.status_snapshot()
    for key, value in snapshot.items():
        click.echo(f"{key}: {value}")


@validation.command(name="run-once")
@click.option("--force", is_flag=True, help="Run one validation poll even if the client is not idle.")
def validation_run_once(force: bool):
    """Run one background validation polling iteration."""
    import asyncio

    from ..validation import ValidationWorker

    cs = ConfigStore()
    cfg = cs.to_config()
    worker = ValidationWorker(cfg)
    result = asyncio.run(worker.run_once(force=force))
    for key, value in result.items():
        click.echo(f"{key}: {value}")
