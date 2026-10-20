"""Inspect process bootstrap; online configuration uses PostgreSQL."""
import json

import click

from ..bootstrap import load_bootstrap
from ..logging_runtime import redact, register_secrets


@click.command(name="config")
@click.argument("action", default="show")
def config_cmd(action):
    """Show read-only process bootstrap."""
    if action != "show":
        raise click.ClickException("dot-path configuration is removed; use tenant configuration API")
    from dataclasses import asdict
    data = asdict(load_bootstrap())
    register_secrets(data)
    click.echo(redact(json.dumps(data, ensure_ascii=False, indent=2)))
