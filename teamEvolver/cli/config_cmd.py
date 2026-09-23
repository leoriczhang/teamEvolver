"""Config command: get / set / show configuration values."""

from __future__ import annotations

import click

from ..config_store import CONFIG_FILE, ConfigStore


@click.command(name="config")
@click.argument("key_or_action")
@click.argument("value", required=False)
def config_cmd(key_or_action: str, value: str | None):
    """Get or set a config value.

    Examples:\n
      teamEvolver config show\n
      teamEvolver config service.port 30001
    """
    cs = ConfigStore()
    if key_or_action == "show":
        if not cs.exists():
            click.echo("No config file found. Run 'teamEvolver config' first.")
            return
        click.echo(f"Config file: {CONFIG_FILE}\n")
        click.echo(cs.describe())
        return

    if value is None:
        result = cs.get(key_or_action)
        if result is None:
            click.echo(f"{key_or_action}: (not set)")
        else:
            click.echo(f"{key_or_action}: {result}")
        return

    cs.set(key_or_action, value)
    click.echo(f"Set {key_or_action} = {cs.get(key_or_action)}")
