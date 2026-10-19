"""`teamEvolver schema` — explicit, versioned database schema management.

Schema change is applied only here : never on import and
never on app start. ``apply`` runs pending ``teamEvolver/database/schema/*.sql``
in version order, idempotently, recording each in ``schema_versions``.
"""

from __future__ import annotations

import click

from ..bootstrap import load_bootstrap
from ..database import SchemaRunner, discover_migrations, get_database_pool
from ..database.pool import dsn_from_env


def _resolve_dsn_schema() -> tuple[str, str, str]:
    config = load_bootstrap()
    dsn = str(getattr(config, "storage_pg_dsn", "") or "").strip() or dsn_from_env()
    schema = str(getattr(config, "storage_pg_schema", "") or "teamevolver").strip()
    ssl = str(getattr(config, "storage_pg_ssl", "") or "prefer").strip()
    return dsn, schema, ssl


@click.group()
def schema() -> None:
    """Manage the TeamEvolver PostgreSQL schema."""


@schema.command("apply")
@click.option("--dry-run", is_flag=True, help="Report pending versions without applying.")
def schema_apply(dry_run: bool) -> None:
    """Apply pending schema versions (idempotent)."""
    dsn, schema_name, ssl = _resolve_dsn_schema()
    if not dsn:
        raise click.ClickException(
            "no PostgreSQL DSN configured (set storage_pg_dsn or TEAMEVOLVER_PG_* env)"
        )
    pool = get_database_pool(dsn=dsn, schema=schema_name, ssl=ssl)
    runner = SchemaRunner(schema=schema_name)

    async def _body():
        async with pool.admin_conn() as conn:
            return await runner.apply(conn, dry_run=dry_run)

    result = pool.run(_body())
    if dry_run:
        pending = result.get("pending", [])
        if pending:
            click.echo(f"pending versions: {', '.join(str(v) for v in pending)}")
        else:
            click.echo("schema up to date; no pending versions")
        return
    applied = result.get("applied", [])
    if applied:
        click.echo(f"applied versions: {', '.join(str(v) for v in applied)}")
    else:
        click.echo("schema already up to date")


@schema.command("status")
def schema_status() -> None:
    """List known schema versions on disk."""
    migrations = discover_migrations()
    for mig in migrations:
        click.echo(f"{mig.version:03d}  {mig.name}  {mig.checksum[:12]}")
    click.echo(f"total: {len(migrations)} version(s) on disk")
