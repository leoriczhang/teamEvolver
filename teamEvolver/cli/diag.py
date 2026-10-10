"""Background validation commands."""

from __future__ import annotations

import click

from ..config_store import ConfigStore


@click.group()
def validation():
    """Background validation commands."""


@validation.command(name="status")
def validation_status():
    """Show background validation configuration and current availability."""
    from team_skills.candidates import ValidationWorker

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

    from team_skills.candidates import ValidationWorker

    cs = ConfigStore()
    cfg = cs.to_config()
    worker = ValidationWorker(cfg)
    result = asyncio.run(worker.run_once(force=force))
    for key, value in result.items():
        click.echo(f"{key}: {value}")
